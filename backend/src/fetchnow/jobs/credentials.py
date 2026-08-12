"""Anonymous media-job access-token parsing and domain-separated hashing."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets

from fetchnow.jobs.errors import JobErrorCode, raise_job_error

# Client generates 32 random bytes → base64url without padding (43 chars).
_ACCESS_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_ACCESS_TOKEN_BYTES = 32
_HASH_BYTES = 32

_ACCESS_DOMAIN = b"fetchnow:v1:media-job:access\0"
_FINGERPRINT_DOMAIN = b"fetchnow:v1:media-job:fingerprint\0"


def generate_access_token() -> str:
    """Return a new client-style access token (32 random bytes, base64url)."""
    raw = secrets.token_bytes(_ACCESS_TOKEN_BYTES)
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def parse_access_token(token: str) -> bytes:
    """Decode a client access token to exactly 32 raw bytes.

    Raises JobError(INVALID_ACCESS_TOKEN) on any malformation. Never includes
    the raw token in the exception message or repr.
    """
    if not isinstance(token, str) or _ACCESS_TOKEN_RE.fullmatch(token) is None:
        raise_job_error(
            JobErrorCode.INVALID_ACCESS_TOKEN,
            internal_reason="TOKEN_SHAPE",
        )
    try:
        raw = base64.urlsafe_b64decode(token + "=")
    except (ValueError, binascii.Error):
        raise_job_error(
            JobErrorCode.INVALID_ACCESS_TOKEN,
            internal_reason="TOKEN_DECODE",
        )
    if len(raw) != _ACCESS_TOKEN_BYTES:
        raise_job_error(
            JobErrorCode.INVALID_ACCESS_TOKEN,
            internal_reason="TOKEN_LENGTH",
        )
    # Reject non-canonical encodings (padding tricks, alternate alphabets).
    canonical = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    if canonical != token:
        raise_job_error(
            JobErrorCode.INVALID_ACCESS_TOKEN,
            internal_reason="TOKEN_NONCANONICAL",
        )
    return raw


def hash_access_token(token: str) -> bytes:
    """Return domain-separated SHA-256 credential hash (32 raw bytes)."""
    raw = parse_access_token(token)
    digest = hashlib.sha256(_ACCESS_DOMAIN + raw).digest()
    if len(digest) != _HASH_BYTES:
        raise_job_error(
            JobErrorCode.INTERNAL_ERROR,
            internal_reason="HASH_LENGTH",
        )
    return digest


def hash_request_fingerprint(*, access_token: str, normalized_request: str) -> bytes:
    """Return a private deterministic fingerprint for an enqueue request.

    The high-entropy client credential is the HMAC key.  Consequently a database
    reader cannot perform an offline dictionary attack against submitted URLs,
    while a retry can prove request equality before any DNS or HTTP operation.
    """
    if not isinstance(normalized_request, str) or not normalized_request:
        raise_job_error(
            JobErrorCode.INTERNAL_ERROR,
            internal_reason="FINGERPRINT_TYPE",
        )
    raw = parse_access_token(access_token)
    digest = hmac.new(
        raw,
        _FINGERPRINT_DOMAIN + normalized_request.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    if len(digest) != _HASH_BYTES:
        raise_job_error(
            JobErrorCode.INTERNAL_ERROR,
            internal_reason="HASH_LENGTH",
        )
    return digest


def tokens_match(left: bytes, right: bytes) -> bool:
    """Constant-time equality for credential or fingerprint digests."""
    if not isinstance(left, bytes | bytearray) or not isinstance(
        right, bytes | bytearray
    ):
        return False
    if len(left) != len(right):
        return False
    return hmac.compare_digest(bytes(left), bytes(right))
