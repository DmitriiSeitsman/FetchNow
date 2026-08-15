"""Cryptographic tokens for browser-native delivery grants (PR14).

Raw tokens are never persisted. Only a domain-separated SHA-256 digest is
stored. Exceptions and repr never include the raw token.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets

from fetchnow.downloads.errors import DownloadErrorCode, raise_download_error

_TOKEN_BYTES = 32
_HASH_BYTES = 32
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_GRANT_DOMAIN = b"fetchnow:v1:browser-delivery-grant\0"

COOKIE_NAME = "__Secure-fetchnow_delivery"
MAX_COOKIE_HEADER_BYTES = 4096
MAX_GRANT_TOKEN_CHARS = 43


def generate_grant_token() -> str:
    """Return a new grant token (32 random bytes, canonical base64url)."""
    raw = secrets.token_bytes(_TOKEN_BYTES)
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def parse_grant_token(token: str) -> bytes:
    """Decode a grant token to exactly 32 raw bytes.

    Raises DownloadError(DOWNLOAD_JOB_NOT_FOUND) on any malformation so
    unauthorized shapes stay indistinguishable from missing grants.
    """
    if not isinstance(token, str) or _TOKEN_RE.fullmatch(token) is None:
        raise_download_error(
            DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND,
            internal_reason="GRANT_TOKEN_SHAPE",
        )
    try:
        raw = base64.urlsafe_b64decode(token + "=")
    except (ValueError, binascii.Error):
        raise_download_error(
            DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND,
            internal_reason="GRANT_TOKEN_DECODE",
        )
    if len(raw) != _TOKEN_BYTES:
        raise_download_error(
            DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND,
            internal_reason="GRANT_TOKEN_LENGTH",
        )
    canonical = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    if canonical != token:
        raise_download_error(
            DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND,
            internal_reason="GRANT_TOKEN_NONCANONICAL",
        )
    return raw


def hash_grant_token(token: str) -> bytes:
    """Return domain-separated SHA-256 of the raw grant token (32 bytes)."""
    raw = parse_grant_token(token)
    digest = hashlib.sha256(_GRANT_DOMAIN + raw).digest()
    if len(digest) != _HASH_BYTES:
        raise_download_error(
            DownloadErrorCode.INTERNAL_ERROR,
            internal_reason="GRANT_HASH_LENGTH",
        )
    return digest


def grant_hashes_match(left: bytes, right: bytes) -> bool:
    """Constant-time equality for 32-byte grant hashes."""
    if not isinstance(left, bytes | bytearray) or not isinstance(
        right, bytes | bytearray
    ):
        return False
    if len(left) != _HASH_BYTES or len(right) != _HASH_BYTES:
        return False
    return hmac.compare_digest(bytes(left), bytes(right))


def content_path_for_grant(grant_id: object) -> str:
    """Return the exact same-origin content path for a grant UUID."""
    return f"/api/v1/media/browser-grants/{grant_id}/content"
