"""Command implementations. Each returns an exit code (0 on success)."""

from __future__ import annotations

import json
import mimetypes
import re
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, command_ids
from .api import ApiClient, download_signed, quote, upload_form
from .config import Settings
from .credentials import forget_token, load_token, save_token
from .errors import EXIT_ENV, EXIT_OK, EXIT_USAGE, UnfoldxError
from .output import Emitter

_STOP_STATES = {"completed", "failed", "cancelled", "waiting_input"}


# -- account ---------------------------------------------------------------------


def cmd_login(args, settings: Settings, emit: Emitter) -> int:
    client = ApiClient(settings.api_base, None, settings.timeout_sec, client="cli")
    name = args.client_name or f"unfoldx-cli {__version__} @ {socket.gethostname()}"[:80]
    grant = client.post("/v2/auth/device", {"client_name": name})
    emit.note(f"Open {grant['verification_uri_complete']} and approve code {grant['user_code']}.")
    interval = max(float(grant.get("interval", 5)), 0.0)
    deadline = time.monotonic() + float(grant.get("expires_in", 600))
    while True:
        if time.monotonic() > deadline:
            raise UnfoldxError("timeout", "The login code expired; run `unfoldx login` again.",
                               code="authorization_expired")
        time.sleep(interval)
        try:
            issued = client.post("/v2/auth/device/token", {"device_code": grant["device_code"]}, retries=0)
        except UnfoldxError as exc:
            if exc.code == "slow_down":
                interval += 5
                continue
            if exc.retryable:  # a dropped poll does not end the grant; the deadline does
                interval = max(interval, float(exc.retry_after_sec or 0))
                continue
            raise
        if issued.get("status") == "authorized":
            break
    token = issued.pop("access_token")
    issued.pop("token_type", None)
    issued.pop("status", None)
    emit.result({"logged_in": True, "api_base": settings.api_base,
                 "credential_store": save_token(settings.api_base, token), **issued})
    return EXIT_OK


def cmd_logout(args, settings: Settings, emit: Emitter) -> int:
    token = load_token(settings.api_base)
    revoked = False
    if token:
        try:
            revoked = bool(ApiClient(settings.api_base, token, settings.timeout_sec, client="cli").delete("/v2/me").get("revoked"))
        except UnfoldxError as exc:
            if exc.type != "auth_failed":  # an already revoked or expired token needs no revocation
                raise
    forget_token(settings.api_base)
    emit.result({"logged_out": True, "revoked_on_server": revoked})
    return EXIT_OK


def cmd_get(path: str):
    def run(args, client: ApiClient, emit: Emitter) -> int:
        data = client.get(path.format(**{key: quote(value) for key, value in vars(args).items()
                                        if isinstance(value, str)}))
        if path.endswith('/questions'):
            data = {"run_id": args.run_id, **data}
        emit.result(data)
        return EXIT_OK
    return run


# -- runs ------------------------------------------------------------------------


def cmd_runs_events(args, client: ApiClient, emit: Emitter) -> int:
    emit.result(client.get(f"/v2/runs/{quote(args.run_id)}/events", after_seq=args.after_seq, limit=args.limit))
    return EXIT_OK


def cmd_runs_wait(args, client: ApiClient, emit: Emitter, settings: Settings) -> int:
    """Follow a run until it finishes or needs an answer; never waits inside one request."""
    run = quote(args.run_id)
    interval = settings.poll_interval_sec if args.interval is None else max(args.interval, 0.0)
    deadline = time.monotonic() + (args.wait_timeout or settings.wait_timeout_sec)
    seq = args.after_seq
    while True:
        page = client.get(f"/v2/runs/{run}/events", after_seq=seq)
        for event in page["events"]:
            text = event.get("text")
            if text:
                emit.note(f"[{event['type']}] {text}")
        seq = page["next_seq"]
        if page["state"] in _STOP_STATES:
            break
        if time.monotonic() > deadline:
            raise UnfoldxError("timeout", "The run is still going; wait again with --after-seq to resume.",
                               code="wait_timeout", retryable=True,
                               details={"run_id": args.run_id, "state": page["state"], "next_seq": seq})
        time.sleep(interval)
    state = page["state"]
    if state == "completed":
        emit.result({"run_id": args.run_id, "state": state, "next_seq": seq,
                     "result": client.get(f"/v2/runs/{run}/result")})
        return EXIT_OK
    if state == "waiting_input":
        status = client.get(f"/v2/runs/{run}")
        if status.get("question") is None:
            raise UnfoldxError("needs_input",
                               f"The run is waiting on its page, with nothing to answer here; open {status.get('web_url')}.",
                               code="no_open_question",
                               details={"run_id": args.run_id, "next_seq": seq, "web_url": status.get("web_url")})
        raise UnfoldxError("needs_input", "The run is waiting for an answer; open approval_url or answer it.",
                           code="question_open",
                           details={"run_id": args.run_id, "next_seq": seq, "question": status.get("question")})
    status = client.get(f"/v2/runs/{run}") if state == "failed" else {}
    failure = status.get("error") or {}
    raise UnfoldxError("generation_failed", failure.get("message") or f"The run ended {state}.",
                       code=f"run_{state}", details={"run_id": args.run_id, "next_seq": seq,
                                                     **({"failure": failure} if failure else {})})


def _submit_request(args) -> dict:
    flags = {"goal": args.goal, "urls": args.url or None, "material_ids": args.material or None,
             "files": args.file or None, "workspace_id": args.workspace}
    if args.input_json is None:
        request = {key: value for key, value in flags.items() if value is not None}
    else:
        if any(value is not None for value in flags.values()):
            raise UnfoldxError("usage", "Use either --input-json or flags, not both.", code="conflicting_input")
        try:
            text = sys.stdin.read() if args.input_json == "-" else Path(args.input_json).read_text(encoding="utf-8")
            request = json.loads(text)
        except ValueError as exc:
            raise UnfoldxError("usage", f"Cannot read --input-json: {exc}", code="bad_input_json") from None
        if not isinstance(request, dict) or set(request) - set(flags):
            raise UnfoldxError("usage", f"--input-json takes an object with {', '.join(flags)}.",
                               code="bad_input_json")
    if not request.get("goal"):
        raise UnfoldxError("usage", "Say what the video should do with --goal.", code="missing_goal")
    return request


def upload_material(client: ApiClient, path: Path) -> dict:
    """Upload one local file as material and wait for the API to confirm it is stored."""
    if not path.is_file():
        raise UnfoldxError("usage", f"No such file: {path}", code="missing_file")
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    grant = client.post("/v2/materials/uploads", {"filename": path.name, "size_bytes": path.stat().st_size,
                                                  "content_type": content_type})
    upload_form(client, grant.pop("upload"), path, client.timeout_sec * 10)
    return client.post(f"/v2/materials/{quote(grant['material_id'])}/complete")


def cmd_materials_upload(args, client: ApiClient, emit: Emitter) -> int:
    emit.result(upload_material(client, Path(args.path)))
    return EXIT_OK


def cmd_runs_submit(args, client: ApiClient, emit: Emitter) -> int:
    request = _submit_request(args)
    files = [Path(item) for item in request.pop("files", None) or []]
    identity = {**request, "api_base": client.api_base, "command_id": args.command_id,
                "files": [command_ids.file_digest(path) for path in files if path.is_file()]}
    entry = None if args.new else command_ids.recall(identity)
    if entry is None:
        entry = {"command_id": args.command_id or command_ids.new_command_id()}
        command_ids.record(identity, entry)
    if files:
        uploaded = entry.setdefault("uploaded", [])
        for path in files[len(uploaded):]:
            uploaded.append(upload_material(client, path)["material_id"])
            command_ids.record(identity, entry)
    body = {**request, "material_ids": [*(request.get("material_ids") or []), *entry.get("uploaded", [])]}
    if not body["material_ids"]:
        body.pop("material_ids")
    accepted = client.post("/v2/runs", body, headers={"Idempotency-Key": entry["command_id"]})
    if accepted.get("idempotent_replay"):
        emit.note(f"This request already started {accepted['run']['run_id']}; showing that run. "
                  "Add --new to start another.")
    emit.result({"command_id": entry["command_id"], **accepted})
    return EXIT_OK


def _route_answer(args, client: ApiClient, emit: Emitter) -> tuple[str, dict]:
    questions = client.get(f"/v2/runs/{quote(args.run_id)}/questions")["questions"]
    cards = [card for card in questions if card.get("route_choice")
             and args.question in (None, card["question_id"])]
    if not cards:
        raise UnfoldxError("needs_input", "This run has no open route card to answer.", code="no_route_question")
    card = cards[-1]
    quotes = {row["route"]: row for row in card["route_choice"].get("quotes") or []}
    if args.route not in quotes:
        reason = next((row.get("unavailable_reason") for row in card["route_choice"].get("routes") or []
                       if row.get("route") == args.route and row.get("unavailable_reason")), None)
        if reason:
            raise UnfoldxError("usage", f"Route {args.route!r} is unavailable: {reason} "
                                        f"Choose from {', '.join(quotes)}.", code="route_unavailable")
        raise UnfoldxError("usage", f"Route {args.route!r} is not quoted; choose from {', '.join(quotes)}.",
                           code="unknown_route")
    answer = {"action": "choose_route", "route": args.route, "quote_ref": quotes[args.route]["quote_ref"],
              "execution_mode": args.mode}
    for key, value in (("style_id", args.style), ("note", args.note)):
        if value:
            answer[key] = value
    angles = card["route_choice"].get("angles") or []
    if args.angle and args.angle not in [angle["id"] for angle in angles]:
        offered = f"choose from {', '.join(angle['id'] for angle in angles)}" if angles else "this card offers none"
        raise UnfoldxError("usage", f"Angle {args.angle!r} is not on the card; {offered}.", code="unknown_angle")
    if angles:  # the card's first angle is the recommended one, as on the web
        answer["angle_id"] = args.angle or angles[0]["id"]
        if not args.angle:
            emit.note(f"Using the recommended angle {angles[0]['id']} 「{angles[0]['title']}」; "
                      "pass --angle to choose another.")
    return card["question_id"], answer


def cmd_runs_answer(args, client: ApiClient, emit: Emitter) -> int:
    if args.route:
        question_id, answer = _route_answer(args, client, emit)
    elif args.question and (args.answer is not None or args.answer_json is not None):
        question_id = args.question
        try:
            answer = json.loads(args.answer_json) if args.answer_json is not None else args.answer
        except ValueError as exc:
            raise UnfoldxError("usage", f"--answer-json is not JSON: {exc}", code="bad_answer_json") from None
    else:
        raise UnfoldxError("usage", "Answer with --route ROUTE, or --question ID with --answer/--answer-json.",
                           code="missing_answer")
    emit.result(client.post(f"/v2/runs/{quote(args.run_id)}/answers",
                            {"question_id": question_id, "answer": answer}))
    return EXIT_OK


def cmd_runs_cancel(args, client: ApiClient, emit: Emitter) -> int:
    emit.result(client.post(f"/v2/runs/{quote(args.run_id)}/cancel"))
    return EXIT_OK


# -- artifacts -------------------------------------------------------------------


def cmd_artifacts_download(args, client: ApiClient, emit: Emitter) -> int:
    access = client.post(f"/v2/artifacts/{quote(args.artifact_id)}/access")
    name = Path(str(access.get("filename") or "")).name
    if not name or name.startswith("."):
        raise UnfoldxError("server_error", "The API returned no usable file name.", code="bad_response")
    match = re.fullmatch(r"(run_[A-Za-z0-9_-]+)\.(video|cover)", args.artifact_id)
    if not match:
        raise UnfoldxError("usage", "Use the artifact_id returned by runs result.", code="invalid_artifact_id")
    out_dir = Path(args.out) / match[1]
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / name
    size, digest = download_signed(client, access["url"], dest, client.timeout_sec * 10, overwrite=args.overwrite)
    emit.note(f"downloaded {args.artifact_id} -> {dest} ({size} bytes)")
    emit.result({"artifact_id": args.artifact_id, "path": str(dest), "bytes": size, "sha256": digest})
    return EXIT_OK


# -- diagnostics -----------------------------------------------------------------


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in str(text).split(".") if part.isdigit())


def cmd_doctor(args, settings: Settings, emit: Emitter) -> int:
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    check("python", sys.version_info >= (3, 11), f"{sys.version_info.major}.{sys.version_info.minor}")
    token = load_token(settings.api_base)
    check("credentials", token is not None, "token present" if token else "not logged in; run `unfoldx login`")
    try:
        client = ApiClient(settings.api_base, token, settings.timeout_sec, client="cli")
        check("api_base", True, settings.api_base)
    except UnfoldxError as exc:
        client = None
        check("api_base", False, exc.message)
    if client is not None and token:
        try:
            capabilities = client.get("/v2/capabilities")
            minimum = str(capabilities.get("min_client_version", "0"))
            check("auth", True, f"scopes: {', '.join(capabilities.get('scopes') or [])}")
            check("version", _version(__version__) >= _version(minimum),
                  f"cli {__version__}, server requires >= {minimum}")
        except UnfoldxError as exc:
            check("auth", False, f"{exc.code}: {exc.message}")
    ok = all(item["ok"] for item in checks)
    emit.result({"ok_all": ok, "checks": checks, "checked_at": datetime.now(timezone.utc).isoformat()})
    return EXIT_OK if ok else EXIT_ENV
