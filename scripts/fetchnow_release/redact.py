"""Safe diagnostic redaction helpers (never print secrets)."""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

_SECRET_KEYS = re.compile(
    r"(?i)(POSTGRES_PASSWORD|DATABASE_URL|PASSWORD|SECRET|TOKEN|API_KEY)\s*[=:]\s*\S+"
)
_PG_URI = re.compile(r"postgresql(?:\+\w+)?://[^\s\"']+")
_HTTP_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_REDACTED_URL = "<redacted-url>"


def _sanitize_one_http_url(url: str) -> str:
    """Keep scheme/host/port/path; drop userinfo, query, and fragment.

    Malformed authorities fail closed to ``<redacted-url>`` and never raise.
    """
    try:
        parts = urlsplit(url, allow_fragments=True)
        scheme = (parts.scheme or "").lower()
        if scheme not in {"http", "https"}:
            return _REDACTED_URL
        hostname = parts.hostname
        if not hostname:
            return _REDACTED_URL
        try:
            port = parts.port
        except ValueError:
            return _REDACTED_URL
        if port is not None and not (1 <= int(port) <= 65535):
            return _REDACTED_URL
        host = f"[{hostname}]" if ":" in hostname else hostname
        netloc = host if port is None else f"{host}:{port}"
        path = parts.path or ""
        return urlunsplit((scheme, netloc, path, "", ""))
    except Exception:  # noqa: BLE001
        return _REDACTED_URL


def sanitize_http_urls(text: str) -> str:
    """Redact HTTP/HTTPS userinfo, query strings, and fragments in ``text``."""
    try:
        return _HTTP_URL_RE.sub(lambda match: _sanitize_one_http_url(match.group(0)), text)
    except Exception:  # noqa: BLE001
        return "<redacted>"


def redact(text: str) -> str:
    """Redact common secret patterns from diagnostic strings.

    Never raises: a sanitizer failure replaces the whole string.
    """
    try:
        out = sanitize_http_urls(text)
        out = _PG_URI.sub("postgresql://<redacted>", out)
        return _SECRET_KEYS.sub(r"\1=<redacted>", out)
    except Exception:  # noqa: BLE001
        return "<redacted>"
