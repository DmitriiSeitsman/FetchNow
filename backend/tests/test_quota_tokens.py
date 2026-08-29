"""Anonymous identity token and cookie security tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fetchnow.core.config import Settings
from fetchnow.quota.service import AnonymousIdentity, QuotaService
from fetchnow.quota.tokens import (
    COOKIE_NAME,
    extract_anonymous_cookie,
    generate_anonymous_token,
    hash_anonymous_token,
    parse_anonymous_token,
)


def test_anonymous_token_is_opaque_256_bit_canonical_value() -> None:
    token = generate_anonymous_token()
    assert len(token) == 43
    assert len(parse_anonymous_token(token) or b"") == 32
    assert len(hash_anonymous_token(token) or b"") == 32


def test_malformed_and_duplicate_cookie_values_are_rejected() -> None:
    token = generate_anonymous_token()
    assert extract_anonymous_cookie(f"{COOKIE_NAME}={token}") == token
    assert extract_anonymous_cookie(f"{COOKIE_NAME}=chosen") is None
    assert (
        extract_anonymous_cookie(
            f"{COOKIE_NAME}={token}; {COOKIE_NAME}={generate_anonymous_token()}"
        )
        is None
    )


def test_production_identity_cookie_security_flags_and_absolute_ttl() -> None:
    settings = Settings(APP_ENV="production", ANONYMOUS_CLIENT_TTL_SECONDS=31_536_000)
    expires = datetime.now(tz=UTC) + timedelta(days=365)
    identity = AnonymousIdentity(
        id=uuid.uuid4(),
        expires_at=expires,
        raw_token=generate_anonymous_token(),
        cookie_max_age=31_536_000,
    )
    header = QuotaService(settings).set_cookie_header(identity)
    assert header is not None
    assert header.startswith(f"{COOKIE_NAME}=")
    assert "Path=/" in header
    assert "Max-Age=31536000" in header
    assert "HttpOnly" in header
    assert "Secure" in header
    assert "SameSite=Lax" in header
    assert "Domain=" not in header
