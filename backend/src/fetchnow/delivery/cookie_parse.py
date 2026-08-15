"""Fail-closed Cookie header parsing for browser delivery grants.

Never silently picks one of duplicate cookie names. Never includes cookie
contents in exceptions, repr, logs, or metrics.
"""

from __future__ import annotations

from fetchnow.downloads.browser_grant_tokens import (
    COOKIE_NAME,
    MAX_COOKIE_HEADER_BYTES,
    MAX_GRANT_TOKEN_CHARS,
)
from fetchnow.downloads.errors import DownloadErrorCode, raise_download_error


class CookieParseError(Exception):
    """Internal cookie parse failure without cookie contents."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)

    def __repr__(self) -> str:
        return f"CookieParseError(reason={self.reason!r})"


def extract_delivery_grant_token(cookie_header: str | None) -> str:
    """Return the exact grant token from Cookie or raise catalog-safe not-found.

    Requires exactly one ``__Secure-fetchnow_delivery`` name/value pair.
    """
    try:
        return _extract(cookie_header)
    except CookieParseError as exc:
        raise_download_error(
            DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND,
            internal_reason=exc.reason,
        )


def _extract(cookie_header: str | None) -> str:
    if cookie_header is None:
        raise CookieParseError("COOKIE_MISSING")
    if not isinstance(cookie_header, str):
        raise CookieParseError("COOKIE_TYPE")
    # Bound by character length; HTTP headers are ASCII in practice.
    if len(cookie_header) > MAX_COOKIE_HEADER_BYTES:
        raise CookieParseError("COOKIE_HEADER_TOO_LONG")
    if "\r" in cookie_header or "\n" in cookie_header or "\0" in cookie_header:
        raise CookieParseError("COOKIE_HEADER_CONTROL")

    found: str | None = None
    for part in cookie_header.split(";"):
        piece = part.strip()
        if not piece:
            continue
        if "=" not in piece:
            # Ignore attribute-only garbage; do not accept nameless values.
            continue
        name, value = piece.split("=", 1)
        name = name.strip()
        value = value.strip()
        if name != COOKIE_NAME:
            continue
        if found is not None:
            raise CookieParseError("COOKIE_DUPLICATE")
        if not value or len(value) > MAX_GRANT_TOKEN_CHARS:
            raise CookieParseError("COOKIE_VALUE_LENGTH")
        # Reject quoted values — grant tokens are unquoted base64url.
        if value.startswith('"') or value.endswith('"'):
            raise CookieParseError("COOKIE_VALUE_QUOTED")
        found = value

    if found is None:
        raise CookieParseError("COOKIE_MISSING")
    return found
