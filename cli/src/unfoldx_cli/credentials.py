"""Token storage: env > OS keychain (optional) > 0600 credentials file.

A token is stored per API base, so a development token is never sent to another
site. Tokens are never written into the project directory and never echoed back.
"""

from __future__ import annotations

import json
import os
import stat
import sys

from .config import config_dir
from .errors import UnfoldxError

_KEYRING_SERVICE = "unfoldx"


def _keyring():
    if os.environ.get("UNFOLDX_NO_KEYRING"):  # CI/tests: force file storage
        return None
    try:
        import keyring  # optional extra: pip install unfoldx[keychain]

        return keyring
    except ImportError:
        return None


def _path():
    return config_dir() / "credentials.json"


def _read_file() -> dict:
    path = _path()
    if not path.is_file():
        return {}
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        print(f"warning: {path} permissions {oct(mode)} too open; fixing to 0600", file=sys.stderr)
        path.chmod(0o600)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise UnfoldxError("environment", f"Invalid credential file: {path}", code="invalid_credential_store") from None
    if not isinstance(data, dict):
        raise UnfoldxError("environment", f"Invalid credential file: {path}", code="invalid_credential_store")
    return data


def _write_file(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    path.chmod(0o600)


def load_token(api_base: str) -> str | None:
    env_token = os.environ.get("UNFOLDX_TOKEN")
    if env_token:
        return env_token.strip()
    keyring = _keyring()
    if keyring is not None:
        try:
            stored = keyring.get_password(_KEYRING_SERVICE, api_base)
            if stored:
                return stored
        except Exception as exc:  # noqa: BLE001 - degrade to file, but say so
            print(f"warning: keychain read failed ({exc}); trying credentials file", file=sys.stderr)
    token = (_read_file().get("tokens") or {}).get(api_base)
    return token if isinstance(token, str) and token else None


def save_token(api_base: str, token: str) -> str:
    """Persist the token; returns a human-readable description of the location."""
    keyring = _keyring()
    if keyring is not None:
        try:
            keyring.set_password(_KEYRING_SERVICE, api_base, token)
            return "OS keychain"
        except Exception as exc:  # noqa: BLE001 - degrade to file, but say so
            print(f"warning: keychain write failed ({exc}); using credentials file", file=sys.stderr)
    data = _read_file()
    data["tokens"] = {**(data.get("tokens") or {}), api_base: token}
    _write_file(data)
    return str(_path())


def forget_token(api_base: str) -> None:
    keyring = _keyring()
    if keyring is not None:
        try:
            keyring.delete_password(_KEYRING_SERVICE, api_base)
        except Exception:  # noqa: BLE001 - nothing stored there is fine
            pass
    data = _read_file()
    if api_base in (data.get("tokens") or {}):
        data["tokens"] = {base: token for base, token in data["tokens"].items() if base != api_base}
        _write_file(data)


def require_token(api_base: str) -> str:
    token = load_token(api_base)
    if not token:
        raise UnfoldxError(
            "auth_failed",
            f"Not logged in to {api_base}. Run `unfoldx login`, or set UNFOLDX_TOKEN.",
            code="not_logged_in",
        )
    return token
