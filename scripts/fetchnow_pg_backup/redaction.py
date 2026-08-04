"""Secret-safe text helpers."""

from __future__ import annotations

import re

_SECRET_PATTERNS = (
    re.compile(r"(password\s*[=:]\s*)\S+", re.IGNORECASE),
    re.compile(r"(POSTGRES_PASSWORD\s*[=:]\s*)\S+", re.IGNORECASE),
    re.compile(r"(postgresql(?:\+asyncpg)?://[^:\s]+):([^@/\s]+)@", re.IGNORECASE),
)


def redact(text: str) -> str:
    """Redact likely secret material from diagnostic text."""
    out = text
    for pat in _SECRET_PATTERNS:
        if pat.groups == 2:
            out = pat.sub(r"\1:***@", out)
        else:
            out = pat.sub(r"\1***", out)
    return out
