"""Conservative provider format token grammar for download argv values."""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")

_RESERVED = frozenset(
    {
        "best",
        "worst",
        "bestvideo",
        "bestaudio",
        "worstvideo",
        "worstaudio",
    }
)

_FORBIDDEN_CHARS = frozenset("+/,[]()|{} \t\r\n\v\f")


def validate_provider_format_token(token: str) -> str:
    """Accept only conservative exact-format tokens; reject selectors and empty.

    Tokens are argv values only — callers must never log or persist them.
    """
    if not isinstance(token, str) or not token:
        raise ValueError("provider format token invalid")
    if token.startswith("-"):
        raise ValueError("provider format token invalid")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in token):
        raise ValueError("provider format token invalid")
    if any(ch in _FORBIDDEN_CHARS for ch in token):
        raise ValueError("provider format token invalid")
    if not _TOKEN_RE.fullmatch(token):
        raise ValueError("provider format token invalid")
    if token.casefold() in _RESERVED:
        raise ValueError("provider format token reserved")
    return token
