"""Opaque anonymous-client tokens and strict cookie parsing."""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import secrets

TOKEN_BYTES = 32
TOKEN_CHARS = 43
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
TOKEN_DOMAIN = b"fetchnow:v1:anonymous-client\0"
COOKIE_NAME = "__Host-fetchnow_client"
MAX_COOKIE_HEADER_BYTES = 4096


def generate_anonymous_token() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(TOKEN_BYTES)).decode(
        "ascii"
    ).rstrip("=")


def parse_anonymous_token(token: str) -> bytes | None:
    if not isinstance(token, str) or TOKEN_RE.fullmatch(token) is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(token + "=")
    except (ValueError, binascii.Error):
        return None
    if len(raw) != TOKEN_BYTES:
        return None
    canonical = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return raw if canonical == token else None


def hash_anonymous_token(token: str) -> bytes | None:
    raw = parse_anonymous_token(token)
    if raw is None:
        return None
    return hashlib.sha256(TOKEN_DOMAIN + raw).digest()


def extract_anonymous_cookie(cookie_header: str | None) -> str | None:
    """Return one canonical cookie token; malformed/duplicate input is invalid."""
    if (
        cookie_header is None
        or not isinstance(cookie_header, str)
        or len(cookie_header) > MAX_COOKIE_HEADER_BYTES
        or any(char in cookie_header for char in ("\r", "\n", "\0"))
    ):
        return None
    found: str | None = None
    for part in cookie_header.split(";"):
        piece = part.strip()
        if "=" not in piece:
            continue
        name, value = piece.split("=", 1)
        if name.strip() != COOKIE_NAME:
            continue
        if found is not None:
            return None
        found = value.strip()
    if found is None or parse_anonymous_token(found) is None:
        return None
    return found
