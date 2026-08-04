"""Safe diagnostic redaction helpers (never print secrets)."""

from __future__ import annotations

import re

_SECRET_KEYS = re.compile(
    r"(?i)(POSTGRES_PASSWORD|DATABASE_URL|PASSWORD|SECRET|TOKEN|API_KEY)\s*[=:]\s*\S+"
)
_PG_URI = re.compile(r"postgresql(?:\+\w+)?://[^\s\"']+")


def redact(text: str) -> str:
    """Redact common secret patterns from diagnostic strings."""
    out = _SECRET_KEYS.sub(r"\1=<redacted>", text)
    out = _PG_URI.sub("postgresql://<redacted>", out)
    return out
