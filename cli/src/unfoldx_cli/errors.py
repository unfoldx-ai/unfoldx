"""Closed-set error taxonomy and stable exit codes (unfoldx.v2 contract).

The exit-code table is part of the public contract; agents branch on it.
Changing a mapping is a breaking change and requires a schema version bump.
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_QUOTA = 4
EXIT_INPUT = 5
EXIT_ENV = 6
EXIT_FAILED = 7
EXIT_TIMEOUT = 8
EXIT_POLICY = 9
EXIT_INTERRUPT = 130

# Server `error.type` -> CLI exit code. Unknown types map to EXIT_FAILED.
ERROR_TYPE_EXIT_CODES: dict[str, int] = {
    "invalid_request": EXIT_INPUT,
    "auth_failed": EXIT_AUTH,
    "quota_exceeded": EXIT_QUOTA,
    "rate_limited": EXIT_QUOTA,
    "needs_input": EXIT_INPUT,
    "policy_blocked": EXIT_POLICY,
    "generation_failed": EXIT_FAILED,
    "not_found": EXIT_INPUT,
    "server_error": EXIT_FAILED,
    "timeout": EXIT_TIMEOUT,
    # Client-side types (never sent by the server):
    "usage": EXIT_USAGE,
    "environment": EXIT_ENV,
}


class UnfoldxError(Exception):
    """A contract-level error carrying the public error envelope."""

    def __init__(
        self,
        error_type: str,
        message: str,
        *,
        code: str | None = None,
        retryable: bool = False,
        retry_after_sec: int | None = None,
        outcome: str | None = None,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.type = error_type
        self.code = code or error_type
        self.message = message
        self.retryable = bool(retryable)
        self.retry_after_sec = retry_after_sec
        self.outcome = outcome
        self.details = details

    @property
    def exit_code(self) -> int:
        return ERROR_TYPE_EXIT_CODES.get(self.type, EXIT_FAILED)

    def to_payload(self) -> dict:
        return {
            "type": self.type,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "retry_after_sec": self.retry_after_sec,
            "outcome": self.outcome,
            "details": self.details,
        }
