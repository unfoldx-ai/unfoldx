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
        self.disconnects = 0
        self.download_breaks = 0  # film downloads that drop the connection halfway
        self.ranges = []  # Range headers the film downloads asked for
        self.signed_url_expired = False  # film URLs answer 403
        self.file_requests = 0
        self.polls_before_approval = 1
        self.angles = None  # the route card's explanation angles, when a test offers them
        self.token_hiccups = 0  # transient 503s the token endpoint answers first
        self.stalls = 0  # requests that hang past the client's timeout before answering
        self.over_budget = False  # route answers come back approval_required
        self.credits = {"wallet": {"available": 1840, "held": 300},
                        "credential": {"credential_id": "cred_0123456789abcdef", "route_credit_cap": 300,
                                       "daily_credit_cap": 1000, "approved_last_24h": 600, "daily_remaining": 400,
                                       "window": "rolling_24h", "next_release_at": "2026-11-20T08:00:00Z"},
                        "auto_approve_limit": 300}
        self.intents: dict[str, dict] = {}
        self.topics: list[dict] = []
        self.schedules: dict[int, dict] = {}
        self.next_schedule_id = 1
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

    def topics_view(self):
        return {"revision": len(self.topics), "topics": self.topics, "events": [], "sources": []}

    def schedule_body(self, body):
        return {"schedule_id": self.next_schedule_id, "title": body["title"], "frequency": body["frequency"],
                "scope": body.get("scope") or {}, "deliverable": body["deliverable"],
                "notify": body.get("notify"), "state": "active", "daily_credit_cap": body.get("daily_credit_cap", 100),
                "next_run_at": "2026-11-24T01:00:00Z", "last_run": None, "created_at": "2026-11-23T00:00:00Z",
                "scope_names": {"topics": [], "events": []},
                "lead_agent": "kai"}

    def write(self, method, path, headers, body):
        if path == "/api/v2/publish-intents":
            intent_id = f"pi_{len(self.intents):022d}"
            self.intents[intent_id] = {"intent_id": intent_id, "run_id": body["run_id"], "platform": body["platform"],
                                       "title": body["title"], "description": body.get("description", ""),
                                       "tags": body.get("tags") or [], "visibility": "public", "state": "created",
                                       "detail": {}, "work_url": None,
                                       "handoff_url": f"https://dev.unfoldx.ai/publish/{intent_id}?t=tok",
                                       "expires_at": "2026-11-23T13:30:00Z"}
            return self.envelope(self.intents[intent_id], status=201)
        if path.endswith("/cancel") and path.startswith("/api/v2/publish-intents/"):
            intent = self.intents.get(path.split("/")[4])
            if intent is None:
                return self.fail(404, "not_found", "not_found")
            intent["state"] = "abandoned"
            return self.envelope(intent)
        if path == "/api/v2/topics/subscriptions":
            self.topics.append({"key": "t-new", "topic": body["topic"], "description": body.get("description") or "",
                                "cards": 0, "starred": 0, "new": 0, "updated_at": None})
            return self.envelope(self.topics_view(), status=201)
        if path == "/api/v2/schedules":
            row = self.schedule_body(body)
            self.schedules[row["schedule_id"]] = row
            self.next_schedule_id += 1
            return self.envelope(row, status=201)
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
            self.file_requests += 1
            return (403, b"expired") if self.signed_url_expired else (200, FILM)
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
        if path.startswith("/api/v2/publish-intents/"):
            intent = self.intents.get(path.split("/")[4])
            if intent is None:
                return self.fail(404, "not_found", "not_found")
            return self.envelope(intent)
        if path == f"/api/v2/runs/{RUN}/publications":
            return self.envelope({"run_id": RUN, "publications": []})
        if path == "/api/v2/topics":
            return self.envelope(self.topics_view())
        if path.startswith("/api/v2/topics/subscriptions/") and method == "DELETE":
            key = path.rsplit("/", 1)[1]
            self.topics = [row for row in self.topics if row["key"] != key]
            return self.envelope(self.topics_view())
        if path == "/api/v2/schedules":
            return self.envelope({"schedules": list(self.schedules.values())})
        if path.startswith("/api/v2/schedules/"):
            parts = path.split("/")
            schedule_id = int(parts[4])
            row = self.schedules.get(schedule_id)
            if row is None:
                return self.fail(404, "not_found", "not_found")
            if len(parts) > 5 and parts[5] == "runs":
                return self.envelope({"schedule_id": schedule_id, "runs": {"runs": [], "has_more": False}})
            if method == "PATCH":
                row.update(body)
                return self.envelope(row)
            if method == "DELETE":
                self.schedules.pop(schedule_id)
                return self.envelope({"schedule_id": schedule_id, "deleted": True})
            return self.envelope(row)
        if path == "/api/v2/capabilities":
            # Shaped like dev's answer, including fields this client does not know yet.
            return self.envelope({"api": "unfoldx.v2", "min_client_version": "0.2.0", "scopes": ["usage:read"],
                                  "budget": {"route_credit_cap": 300, "daily_credit_cap": 1000},
                                  "operations": ["GET /runs/{id}/usage", "GET /credits"],
                                  "routes": [{"route": "template", "label": "模板制作", "label_en": "Template"}],
                                  "skills": [{"id": "explain", "roles": ["jiang"], "title": "讲解", "says": "讲清楚 X",
                                              "needs": [], "routes": ["template"], "paid": True, "examples": [],
                                              "added_later": {"anything": True}}],
                                  "added_later": 1})
        if path == "/api/v2/credits":
            return self.envelope(self.credits)
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
            if parsed.path.startswith("/files/") and status == 200:
                fake.ranges.append(self.headers.get("Range"))
                start = int((self.headers.get("Range") or "bytes=0-")[6:].split("-")[0])
                if start:
                    status = 206
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
                    data = data[start:]
                if fake.download_breaks:
                    fake.download_breaks -= 1
                    if status == 200:
                        self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data[:len(data) // 2])
                    self.wfile.flush()
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.close_connection = True
                    return
                if status == 206:
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
            self.send_response(status)
            self.send_header("Content-Type", "application/octet-stream" if isinstance(payload, bytes)
                             else "application/json")
            self.send_header("Content-Length", str(len(data)))
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
