"""A local stand-in for the UnfoldX `/api/v2` server over real HTTP.

Shared by the CLI and MCP tests.
"""

from __future__ import annotations

import json
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TOKEN = "ufx_" + "Ab-_" * 10 + "xyz"  # token_urlsafe alphabet, 43 characters
FILM = b"\x00\x00\x00 ftypisom fake film bytes"
RUN = "run_AAAAAAAAAAAAAAAAAAAAAA"


class FakePlatform:
    def __init__(self):
        self.truncate_download = False
        self.disconnects = 0
        self.polls_before_approval = 1
        self.angles = None  # the route card's explanation angles, when a test offers them
        self.token_hiccups = 0  # transient 503s the token endpoint answers first
        self.stalls = 0  # requests that hang past the client's timeout before answering
        self.over_budget = False  # route answers come back approval_required
        self.materials: dict[str, dict] = {}
        self.objects: dict[str, bytes] = {}  # what was posted to the fake OSS endpoint
        self.commands: dict[str, dict] = {}  # Idempotency-Key -> submitted body
        self.answers: list[dict] = []
        self.revoked = False
        self.run_states = ["running", "waiting_input"]
        self.open_question = True  # False: waiting on the web page with no card the API can answer
        self.requests: list[tuple[str, str, dict]] = []
        self.clients: list[str | None] = []  # each request's X-Unfoldx-Client, the fake OSS and files included
        self.base = ""

    def envelope(self, data=None, *, status=200, error=None):
        body = {"schema": "unfoldx.v2", "request_id": "req_test"}
        body.update({"ok": True, "data": data} if error is None else {"ok": False, "error": error})
        return status, body

    def fail(self, status, type_, code, message="", **extra):
        return self.envelope(status=status, error={
            "type": type_, "code": code, "message": message or code, "retryable": False,
            "retry_after_sec": None, "outcome": None, "details": None, **extra})

    def question(self):
        return {"question_id": "q-route", "contract": "unfoldx.route-choice.v2", "text": "选择制作路线", "options": [], "answerable": True,
                "answer_schema": {"type": "object"}, "answer_examples": [],
                "approval_url": "https://dev.unfoldx.ai/chat/abc123",
                "route_choice": {"recommended": "template",
                                 "routes": [{"route": "template", "available": True},
                                            {"route": "fine", "available": False,
                                             "unavailable_reason": "精细制作当前没有可用的制作环境。"}],
                                 "quotes": [{"route": "template", "low": 80, "high": 125, "eta_minutes": [3, 8], "hold": 125, "quote_ref": "qr-template"}],
                                 **({"angles": self.angles} if self.angles else {})}}

    def run(self, state="queued"):
        return {"run_id": RUN, "workspace_id": "ws_abc123", "web_url": "https://unfoldx.ai/chat/ws_abc123", "state": state, "question": None, "settlement_status": "pending"}

    def write(self, method, path, headers, body):
        if path == "/api/v2/materials/uploads":
            material_id = f"mat_{len(self.materials):022d}"
            key = f"files/1/{len(self.materials)}_{body['filename']}"
            self.materials[material_id] = {"material_id": material_id, "filename": body["filename"],
                                           "size_bytes": body["size_bytes"], "content_type": body["content_type"], "state": "pending", "key": key}
            view = {k: v for k, v in self.materials[material_id].items() if k != "key"}
            return self.envelope({**view, "upload": {
                "method": "POST", "url": self.base + "/oss", "file_field": "file",
                "fields": {"key": key, "policy": "cG9saWN5", "OSSAccessKeyId": "test-id", "signature": "sig",
                           "Content-Type": body["content_type"]}}}, status=201)
        if path.startswith("/api/v2/materials/") and path.endswith("/complete"):
            material = self.materials.get(path.split("/")[4])
            if material is None:
                return self.fail(404, "not_found", "not_found")
            if len(self.objects.get(material["key"], b"")) != material["size_bytes"]:
                return self.fail(409, "invalid_request", "upload_incomplete")
            material["state"] = "ready"
            return self.envelope({k: v for k, v in material.items() if k != "key"})
        if path == "/api/v2/runs":
            key = headers.get("Idempotency-Key")
            if not key:
                return self.fail(400, "invalid_request", "command_id_required")
            if key in self.commands and self.commands[key] != body:
                return self.fail(409, "invalid_request", "idempotency_conflict")
            replay = key in self.commands
            self.commands[key] = body
            return self.envelope({"run": self.run(), "idempotent_replay": replay}, status=202)
        if path == f"/api/v2/runs/{RUN}/answers":
            if self.over_budget:
                return self.fail(409, "needs_input", "approval_required",
                                 details={"approval_url": "https://dev.unfoldx.ai/chat/abc123", "hold": 125})
            self.answers.append(body)
            return self.envelope({"run": self.run(), "answer": {"question_id": body["question_id"]}})
        if path == f"/api/v2/runs/{RUN}/cancel":
            return self.envelope({"run": self.run("cancelled")})
        return None

    def handle(self, method, path, query, headers, body):
        self.requests.append((method, path, body))
        self.clients.append(headers.get("X-Unfoldx-Client"))
        if self.stalls > 0:
            self.stalls -= 1
            threading.Event().wait(1.0)  # not time.sleep, which tests stub out for the client
        if path == "/api/v2/auth/device" and method == "POST":
            return self.envelope({"device_code": "d" * 43, "user_code": "ABCD-EFGH",
                                  "verification_uri": "https://dev.unfoldx.ai/cli-authorize",
                                  "verification_uri_complete": "https://dev.unfoldx.ai/cli-authorize?code=ABCD-EFGH",
                                  "expires_in": 600, "interval": 0})
        if path == "/api/v2/auth/device/token":
            if self.token_hiccups > 0:
                self.token_hiccups -= 1
                return self.fail(503, "server_error", "internal_error", retryable=True)
            if self.polls_before_approval > 0:
                self.polls_before_approval -= 1
                return self.envelope({"status": "pending"})
            return self.envelope({"status": "authorized", "access_token": TOKEN, "token_type": "Bearer",
                                  "credential_id": "cred_0123456789abcdef", "client_name": body.get("client_name"),
                                  "scopes": ["capabilities:read", "runs:read", "artifacts:read", "usage:read"],
                                  "budget": {"route_credit_cap": 300, "daily_credit_cap": 1000},
                                  "expires_at": "2027-01-28T00:00:00Z"})
        if path.startswith("/files/"):
            return 200, FILM
        if path == "/oss" and method == "POST":
            fields = _multipart(headers, body)
            self.objects[fields["key"].decode()] = fields["file"]
            return 204, b""
        if headers.get("Authorization") != f"Bearer {TOKEN}":
            return self.fail(401, "auth_failed", "credential_invalid")
        if self.revoked:
            return self.fail(401, "auth_failed", "credential_revoked")
        if path == "/api/v2/me" and method == "DELETE":
            self.revoked = True
            return self.envelope({"credential_id": "cred_0123456789abcdef", "revoked": True})
        if path == "/api/v2/me":
            return self.envelope({"credential_id": "cred_0123456789abcdef", "client_name": "unfoldx-cli test",
                                  "scopes": ["capabilities:read", "runs:read"]})
        if method == "POST" and (written := self.write(method, path, headers, body)) is not None:
            return written
        if path.startswith("/api/v2/materials/"):
            material = self.materials.get(path.split("/")[4])
            if material is None:
                return self.fail(404, "not_found", "not_found")
            return self.envelope({k: v for k, v in material.items() if k != "key"})
        if path.startswith("/api/v2/commands/"):
            if path.split("/")[4] not in self.commands:
                return self.fail(404, "not_found", "not_found")
            return self.envelope(self.run())
        if path == f"/api/v2/runs/{RUN}/questions":
            return self.envelope({"questions": [self.question()]})
        if path == "/api/v2/capabilities":
            return self.envelope({"api": "unfoldx.v2", "min_client_version": "0.2.0", "scopes": [], "operations": []})
        if path == "/api/v2/workspaces":
            return self.fail(403, "auth_failed", "insufficient_scope", "该凭据没有 runs:read 权限")
        if path == f"/api/v2/runs/{RUN}/events":
            after = int(query.get("after_seq", ["0"])[0])
            state = self.run_states.pop(0) if len(self.run_states) > 1 else self.run_states[0]
            events = [] if after >= 7 else [{"seq": 7, "type": "progress", "text": "正在写脚本"}]
            return self.envelope({"events": events, "next_seq": max(after, 7) if events else after, "state": state})
        if path == f"/api/v2/runs/{RUN}":
            state = self.run_states[0]
            return self.envelope({**self.run(state),
                                  "question": self.question() if state == "waiting_input" and self.open_question
                                  else None})
        if path == f"/api/v2/runs/{RUN}/result":
            return self.envelope({"run_id": RUN, "workspace_id": "ws_abc123", "message": "视频已交付", "state": "completed", "ready": True, "title": "量子纠缠一分钟",
                                  "artifacts": [{"artifact_id": f"{RUN}.video", "role": "video",
                                                 "filename": "abc123.mp4"}]})
        if path == f"/api/v2/artifacts/{RUN}.video/access" and method == "POST":
            return self.envelope({"artifact_id": f"{RUN}.video", "url": self.base + "/files/final.mp4?sig=1",
                                  "filename": "abc123.mp4", "expires_at": "2026-11-04T12:00:00Z", "expires_in": 3600})
        return self.fail(404, "not_found", "not_found")


def _multipart(headers, raw):
    from email.parser import BytesParser
    from email.policy import HTTP
    message = BytesParser(policy=HTTP).parsebytes(
        b"Content-Type: " + headers["Content-Type"].encode() + b"\r\n\r\n" + raw)
    return {part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
            for part in message.iter_parts()}


@contextmanager
def serve():
    fake = FakePlatform()

    class Handler(BaseHTTPRequestHandler):
        def _serve(self):
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            is_json = (self.headers.get("Content-Type") or "").startswith("application/json")
            body = (json.loads(raw) if raw else {}) if is_json or not raw else raw
            status, payload = fake.handle(self.command, parsed.path, parse_qs(parsed.query), self.headers, body)
            if fake.disconnects and self.command == 'POST' and parsed.path == '/api/v2/runs':
                fake.disconnects -= 1
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/octet-stream" if isinstance(payload, bytes)
                             else "application/json")
            self.send_header("Content-Length", str(len(data) + (10 if fake.truncate_download and parsed.path.startswith("/files/") else 0)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_DELETE = do_PUT = do_PATCH = _serve

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    fake.base = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield fake
    finally:
        server.shutdown()
