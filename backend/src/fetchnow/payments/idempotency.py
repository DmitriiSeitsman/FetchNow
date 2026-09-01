"""Opaque creation-idempotency key validation and hashing."""

from __future__ import annotations

import hashlib
import re

from fetchnow.payments.errors import InvalidIdempotencyKeyError

_DOMAIN = b"fetchnow:v1:payment-order-idempotency\0"
_KEY = re.compile(r"^[A-Za-z0-9._~-]{22,128}$")


def hash_idempotency_key(value: str) -> bytes:
    if not isinstance(value, str) or _KEY.fullmatch(value) is None:
        raise InvalidIdempotencyKeyError()
    return hashlib.sha256(_DOMAIN + value.encode("ascii")).digest()
