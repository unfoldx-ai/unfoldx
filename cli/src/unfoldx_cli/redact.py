"""Credential redaction applied to every output path (stdout, stderr, errors)."""

from __future__ import annotations

import re

_MASK = "***redacted***"

_PATTERNS = [
    re.compile(r"ufx_[A-Za-z0-9_\-]{4,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-]{8,}"),
]


def redact_text(text: str) -> str:
    for pattern in _PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) + _MASK) if m.groups() else _MASK, text)
    return text


def redact(value):
    """Recursively redact strings inside JSON-like structures."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value
