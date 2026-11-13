"""Thin HTTPS client for the unfoldx.v2 public API (stdlib only).

Robustness rules:
- https required except for localhost (contract tests).
- Retries dropped connections and errors the server marks retryable, honoring
  retry_after_sec. Every call here is a read or carries its own idempotency; the
  device-code poll retries in its own loop until the grant expires.
- Every failure is normalized into the closed-set UnfoldxError taxonomy; the
  server's `code`, `outcome` and `details` pass through unchanged.
"""

from __future__ import annotations

import hashlib
import http.client
import math
import os
import tempfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import SCHEMA, __version__
from .errors import UnfoldxError

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_MAX_RETRIES = 2
_MAX_BACKOFF_SEC = 30.0


class _NoApiRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise UnfoldxError(
            "environment", "The API redirected this request. Configure the final --api-base explicitly.",
            code="api_redirect",
        )


def _require_safe_url(url: str, purpose: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or (parsed.scheme == "http" and parsed.hostname not in _LOCAL_HOSTS):
        raise UnfoldxError(
            "environment",
            f"{purpose} must use https (got {parsed.scheme or 'no scheme'}); "
            "plain http is only allowed for localhost testing.",
            code="insecure_url",
        )


def quote(value: str) -> str:
    return urllib.parse.quote(str(value), safe="")


CLIENTS = ("cli", "mcp")


class ApiClient:
    def __init__(self, api_base: str, token: str | None, timeout_sec: float = 30.0, *, client: str):
        _require_safe_url(api_base, "API base URL")
        if client not in CLIENTS:
            raise ValueError(f"client {client!r} is not one of {', '.join(CLIENTS)}")
        self.api_base = api_base.rstrip("/")
        self.token = token
        self.timeout_sec = timeout_sec
        self.identity = {"User-Agent": f"unfoldx-cli/{__version__}", "X-Unfoldx-Client": client}
        self._opener = urllib.request.build_opener(_NoApiRedirect())

    def get(self, path: str, **query) -> dict:
        params = {key: value for key, value in query.items() if value is not None}
        return self.request("GET", path + (f"?{urllib.parse.urlencode(params)}" if params else ""))

    def post(self, path: str, body: dict | None = None, *, retries: int = _MAX_RETRIES,
             headers: dict | None = None) -> dict:
        return self.request("POST", path, body=body or {}, retries=retries, headers=headers)

    def delete(self, path: str) -> dict:
        return self.request("DELETE", path)

    def request(self, method: str, path: str, *, body: dict | None = None, retries: int = _MAX_RETRIES,
                headers: dict | None = None) -> dict:
        """Send one operation within its time budget; preserve the write identity on failure."""
        try:
            return self._request(method, path, body=body, retries=retries, headers=headers)
        except UnfoldxError as exc:
            key = (headers or {}).get("Idempotency-Key")
            if key:
                exc.details = {**(exc.details or {}), "command_id": key,
                               "recovery_command": f"unfoldx commands show {key}"}
            raise

    def _request(self, method, path, *, body, retries, headers):
        deadline = time.monotonic() + self.timeout_sec
        url = self.api_base + path
        payload = None if body is None else json.dumps(body).encode("utf-8")
        attempt = 0
        while True:
            request = urllib.request.Request(url, data=payload, method=method, headers=self.identity)
            request.add_header("Accept", "application/json")
            if payload is not None:
                request.add_header("Content-Type", "application/json")
            if self.token:
                request.add_header("Authorization", f"Bearer {self.token}")
            for name, value in (headers or {}).items():
                request.add_header(name, value)
            try:
                with self._opener.open(request, timeout=max(0.001, deadline - time.monotonic())) as response:
                    envelope = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                error = self._parse_error(exc)
                if attempt < retries and error.retryable:
                    delay = (float(error.retry_after_sec) if error.retry_after_sec is not None
                             else min(2.0 ** (attempt + 1), _MAX_BACKOFF_SEC))
                    if time.monotonic() + delay < deadline:
                        attempt += 1
                        time.sleep(delay)
                        continue
                raise error from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as exc:
                reason = getattr(exc, "reason", exc)
                timed_out = isinstance(reason, (socket.timeout, TimeoutError))
                error = UnfoldxError(
                    "timeout" if timed_out else "environment",
                    "Request timed out." if timed_out else "The API connection was interrupted; retry with the same command id.",
                    code="request_timeout" if timed_out else "connection_failed", retryable=True,
                )
                delay = min(2.0 ** (attempt + 1), _MAX_BACKOFF_SEC)
                if attempt < retries and time.monotonic() + delay < deadline:
                    attempt += 1
                    time.sleep(delay)
                    continue
                raise error from None
            except ValueError:
                raise UnfoldxError("server_error", "The API returned a non-JSON response.", code="bad_response") from None
            if not isinstance(envelope, dict) or envelope.get("schema") != SCHEMA or envelope.get("ok") is not True:
                raise UnfoldxError(
                    "server_error", f"The API did not answer with a {SCHEMA} envelope.", code="bad_response"
                )
            return envelope.get("data") or {}

    @staticmethod
    def _parse_error(exc: urllib.error.HTTPError) -> UnfoldxError:
        retry_after = None
        header = exc.headers.get("Retry-After") if exc.headers else None
        if header:
            try:
                retry_after = (max(0, int(header)) if header.isdigit() else
                               max(0, math.ceil((parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds())))
            except (ValueError, TypeError, OverflowError):
                pass
        try:
            error = json.loads(exc.read().decode("utf-8"))["error"]
            return UnfoldxError(
                str(error["type"]),
                str(error.get("message") or ""),
                code=error.get("code"),
                retryable=bool(error.get("retryable", False)),
                retry_after_sec=max([v for v in (error.get("retry_after_sec"), retry_after)
                                     if isinstance(v, (int, float)) and v >= 0], default=None),
                outcome=error.get("outcome"),
                details=error.get("details"),
            )
        except (ValueError, KeyError, TypeError):
            pass
        if exc.code == 401:
            return UnfoldxError("auth_failed", "Authentication failed.", code="http_401")
        if exc.code == 404:
            return UnfoldxError("not_found", "Resource not found.", code="http_404")
        if exc.code == 429:
            return UnfoldxError("rate_limited", "Rate limited.", code="http_429", retryable=True,
                                retry_after_sec=retry_after)
        if exc.code >= 500:
            return UnfoldxError("server_error", f"Server error (HTTP {exc.code}).", code=f"http_{exc.code}",
                                retryable=True)
        return UnfoldxError("invalid_request", f"HTTP {exc.code}.", code=f"http_{exc.code}")


def download_signed(client: ApiClient, url: str, dest: Path, timeout_sec: float, *,
                    overwrite: bool = False) -> tuple[int, str]:
    """Download completely before publishing a file; default publication never replaces another file."""
    _require_safe_url(url, "Download URL")
    if not overwrite and dest.exists():
        raise UnfoldxError("usage", f"File already exists: {dest}. Use --overwrite to replace it.", code="file_exists")
    total = 0
    digest = hashlib.sha256()
    fd, name = tempfile.mkstemp(prefix=".unfoldx-", suffix=".part", dir=dest.parent)
    partial = Path(name)
    try:
        with os.fdopen(fd, "wb") as fh:
            with urllib.request.urlopen(urllib.request.Request(url, headers=client.identity),
                                        timeout=timeout_sec) as response:
                while chunk := response.read(1024 * 256):
                    fh.write(chunk)
                    digest.update(chunk)
                    total += len(chunk)
                expected = response.headers.get('Content-Length')
                if expected is not None and total != int(expected):
                    raise http.client.IncompleteRead(b'', int(expected) - total)
        if overwrite:
            partial.replace(dest)
        else:
            try:
                os.link(partial, dest)  # atomic no-clobber, including a concurrent download
            except FileExistsError:
                raise UnfoldxError("usage", f"File already exists: {dest}. Use --overwrite to replace it.",
                                   code="file_exists") from None
    except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException):
        raise UnfoldxError("environment", "Download interrupted; the existing file was preserved.",
                           code="download_failed", retryable=True) from None
    finally:
        partial.unlink(missing_ok=True)
    return total, digest.hexdigest()


def upload_form(client: ApiClient, upload: dict, path: Path, timeout_sec: float) -> None:
    """POST a file to the signed object-storage form the API issued, streaming it from disk."""
    _require_safe_url(upload["url"], "Upload URL")
    boundary = "unfoldx-" + hashlib.sha256(str(time.time_ns()).encode()).hexdigest()[:24]
    head = b"".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        for name, value in upload["fields"].items())
    head += (f'--{boundary}\r\nContent-Disposition: form-data; name="{upload.get("file_field", "file")}"; '
             f'filename="{path.name}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    size = path.stat().st_size
    with open(path, "rb") as fh:
        def chunks():
            yield head
            while chunk := fh.read(1024 * 1024):
                yield chunk
            yield tail
        request = urllib.request.Request(upload["url"], data=chunks(), method="POST", headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(head) + size + len(tail)), **client.identity})
        try:
            with urllib.request.urlopen(request, timeout=timeout_sec):
                pass
        except urllib.error.HTTPError as exc:
            raise UnfoldxError("invalid_request", f"Object storage refused the upload (HTTP {exc.code}).",
                               code="upload_rejected", retryable=exc.code >= 500) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as exc:
            raise UnfoldxError("environment", f"Upload failed: {getattr(exc, 'reason', exc)}",
                               code="upload_failed", retryable=True) from None
