"""Client command ids: the Idempotency-Key a write carries.

An id is recorded under <config>/commands/ before the request goes out, keyed by what the command
asks for (goal, sources, the files' bytes), so running the same command again after a crash or a
lost response replays the run the server already accepted instead of starting a second one. `--new`
starts a fresh command on purpose.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path

from .config import config_dir
from .errors import UnfoldxError


def _record_path(request: dict) -> Path:
    digest = hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return config_dir() / "commands" / f"{digest}.json"


def recall(request: dict) -> dict | None:
    try:
        return json.loads(_record_path(request).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except ValueError:
        raise UnfoldxError("environment", "Command receipt is corrupt; recover the existing command before submitting again.",
                           code="invalid_command_receipt") from None


def record(request: dict, entry: dict) -> None:
    path = _record_path(request)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    partial = path.with_suffix(".part")
    partial.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    os.replace(partial, path)


def new_command_id() -> str:
    return "cli_" + secrets.token_urlsafe(18)


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
