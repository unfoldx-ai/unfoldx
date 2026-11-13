"""Local exports: owned manifest, bounded signed downloads and atomic no-clobber output."""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

from .api import download_signed, quote
from .errors import UnfoldxError


def _save(source, target, overwrite):
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        if overwrite:
            os.replace(source, target)
        else:
            os.link(source, target)
    except FileExistsError:
        raise UnfoldxError(
            "usage", f"File exists: {target}; use --overwrite", code="file_exists"
        ) from None
    return {
        "path": str(target.resolve()),
        "bytes": target.stat().st_size,
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
    }


def export_work(client, run_id, format, out, overwrite=False):
    if format not in ("video", "cover", "subtitles", "storyboard", "transcript", "audio", "zip"):
        raise UnfoldxError("usage", "Unknown export format", code="invalid_arguments")
    manifest = client.get("/v2/works/" + quote(run_id) + "/exports")
    available = {x["role"]: x for x in manifest["files"]}
    if format != "zip" and format not in available:
        raise UnfoldxError(
            "not_found",
            f"{format} was not saved for this work. Available: " + ", ".join(available),
            code="export_unavailable",
        )
    directory = Path(out).expanduser().resolve() / run_id
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".export-", dir=directory) as staging:
        staging = Path(staging)
        downloaded = {}

        def fetch(role):
            if role in downloaded:
                return downloaded[role]
            item = available[role]
            filename = Path(item["filename"]).name
            if filename in ("", ".", ".."):
                raise UnfoldxError(
                    "server_error", "Unsafe export filename", code="invalid_response"
                )
            target = staging / filename
            if "content" in item:
                target.write_text(item["content"], encoding="utf-8")
            elif item.get("derived_from") == "video":
                ffmpeg = shutil.which("ffmpeg")
                if not ffmpeg:
                    raise UnfoldxError(
                        "environment",
                        "Install ffmpeg to extract the mixed audio track",
                        code="ffmpeg_missing",
                    )
                source = fetch("video")
                try:
                    process = subprocess.run(
                        [
                            ffmpeg,
                            "-nostdin",
                            "-v",
                            "error",
                            "-i",
                            str(source),
                            "-map",
                            "0:a:0",
                            "-vn",
                            "-c:a",
                            "aac",
                            "-y",
                            str(target),
                        ],
                        capture_output=True,
                        timeout=120,
                    )
                except subprocess.TimeoutExpired:
                    raise UnfoldxError(
                        "timeout", "Audio extraction exceeded 120 seconds", code="export_timeout"
                    ) from None
                if process.returncode:
                    raise UnfoldxError(
                        "invalid_request",
                        "The video has no extractable audio track",
                        code="audio_unavailable",
                    )
            else:
                download_signed(client, item["url"], target, client.timeout_sec)
            downloaded[role] = target
            return target

        if format == "zip":
            target = directory / (run_id + ".zip")
            if target.exists() and not overwrite:
                raise UnfoldxError("usage", f"File exists: {target}", code="file_exists")
            for role in available:
                fetch(role)
            archive = staging / (run_id + ".zip")
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
                for role, path in downloaded.items():
                    z.write(path, path.name)
                z.writestr(
                    "manifest.json",
                    json.dumps(
                        {
                            **manifest,
                            "files": [
                                {k: v for k, v in x.items() if k not in ("url", "content")}
                                for x in manifest["files"]
                            ],
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
            files = [{"role": "zip", **_save(archive, target, overwrite)}]
        else:
            item = available[format]
            target = directory / Path(item["filename"]).name
            if target.exists() and not overwrite:
                raise UnfoldxError("usage", f"File exists: {target}", code="file_exists")
            files = [{"role": format, **_save(fetch(format), target, overwrite)}]
    return {"run_id": run_id, "files": files, "unavailable": manifest["unavailable"]}


def superfast_wait(client, job_id, timeout=600, interval=2):
    deadline = time.monotonic() + timeout
    while True:
        data = client.get("/v2/superfast/jobs/" + quote(job_id))
        if data["status"] == "completed":
            return data
        if data["status"] in ("failed", "cancelled"):
            raise UnfoldxError(
                "generation_failed",
                data.get("error") or "Superfast job ended without delivery",
                code="superfast_failed",
                details=data,
            )
        if time.monotonic() >= deadline:
            raise UnfoldxError(
                "timeout",
                "Wait timed out; the superfast job continues",
                code="wait_timeout",
                details={"job_id": job_id},
            )
        time.sleep(min(interval, max(0, deadline - time.monotonic())))


def superfast_download(client, job_id, out, overwrite=False):
    data = client.post("/v2/superfast/jobs/" + quote(job_id) + "/access")
    target = Path(out).expanduser().resolve() / job_id / "film.mp4"
    target.parent.mkdir(parents=True, exist_ok=True)
    size, digest = download_signed(client, data["url"], target, client.timeout_sec, overwrite=overwrite)
    return {
        "job_id": job_id,
        "path": str(target),
        "bytes": size,
        "sha256": digest,
        "billing": data["billing"],
    }
