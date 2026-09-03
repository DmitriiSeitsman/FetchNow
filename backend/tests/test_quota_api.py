"""Public Free quota API contract tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from fetchnow.api.main import create_app
from fetchnow.api.v1 import media_downloads
from fetchnow.core.config import Settings
from fetchnow.quota.errors import FreeQuotaExceededError, QuotaStatus
from fetchnow.quota.service import AnonymousIdentity, QuotaService
from fetchnow.quota.tokens import generate_anonymous_token


async def _client_with_state(
    settings: Settings, service: QuotaService | None = None
) -> tuple[AsyncClient, object]:
    app = create_app(settings)
    app.state.settings = settings
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.commit = AsyncMock()
    app.state.session_factory = MagicMock(return_value=session)
    if service is not None:
        app.state.quota_service = service
    client = AsyncClient(transport=ASGITransport(app=app), base_url="https://test")
    return client, app


@pytest.mark.asyncio
async def test_quota_endpoint_is_fail_closed_when_flag_disabled() -> None:
    settings = Settings(APP_ENV="test", FREE_DOWNLOAD_QUOTA_ENABLED=False)
    client, _app = await _client_with_state(settings)
    async with client:
        response = await client.get("/api/v1/media/quota")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "FREE_QUOTA_DISABLED"
    assert "set-cookie" not in response.headers


@pytest.mark.asyncio
async def test_quota_bootstrap_returns_safe_status_and_secure_cookie() -> None:
    settings = Settings(APP_ENV="test", FREE_DOWNLOAD_QUOTA_ENABLED=True)
    token = generate_anonymous_token()
    identity = AnonymousIdentity(
        id=uuid.uuid4(),
        expires_at=datetime.now(tz=UTC) + timedelta(days=365),
        raw_token=token,
        cookie_max_age=31_536_000,
    )
    status = QuotaStatus("free", 3, 1, 1, 1, None, 86_400, None)

    class StubQuotaService(QuotaService):
        async def bootstrap_identity(self, **_kwargs: object) -> AnonymousIdentity:
            return identity

        async def status(self, **_kwargs: object) -> QuotaStatus:
            return status

    client, _app = await _client_with_state(settings, StubQuotaService(settings))
    async with client:
        response = await client.get("/api/v1/media/quota")
    assert response.status_code == 200
    assert response.json() == {
        "tier": "free",
        "downloadLimit": 3,
        "downloadsUsed": 1,
        "downloadsReserved": 1,
        "downloadsRemaining": 1,
        "windowSeconds": 86_400,
        "premiumExpiresAt": None,
        "resetAt": None,
    }
    cookie = response.headers["set-cookie"]
    assert token in cookie
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=Lax" in cookie
    assert "Path=/" in cookie
    assert "Domain=" not in cookie
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_quota_endpoint_returns_safe_nullable_premium_contract() -> None:
    settings = Settings(APP_ENV="test", FREE_DOWNLOAD_QUOTA_ENABLED=True)
    expires_at = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    identity = AnonymousIdentity(
        id=uuid.uuid4(), expires_at=expires_at + timedelta(days=365)
    )
    status = QuotaStatus("premium", None, None, None, None, None, None, expires_at)

    class StubQuotaService(QuotaService):
        async def bootstrap_identity(self, **_kwargs: object) -> AnonymousIdentity:
            return identity

        async def status(self, **_kwargs: object) -> QuotaStatus:
            return status

    client, _app = await _client_with_state(settings, StubQuotaService(settings))
    async with client:
        response = await client.get("/api/v1/media/quota")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "tier": "premium",
        "downloadLimit": None,
        "downloadsUsed": None,
        "downloadsReserved": None,
        "downloadsRemaining": None,
        "windowSeconds": None,
        "premiumExpiresAt": "2026-09-04T12:00:00Z",
        "resetAt": None,
    }
    assert "entitlement" not in response.text
    assert "payment" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("reliable_reset", [False, True])
async def test_download_quota_429_contract_only_promises_reliable_reset(
    monkeypatch: pytest.MonkeyPatch, reliable_reset: bool
) -> None:
    settings = Settings(
        APP_ENV="test",
        MEDIA_DOWNLOADS_ENABLED=True,
        FREE_DOWNLOAD_QUOTA_ENABLED=True,
    )
    client, app = await _client_with_state(settings)
    reset_at = datetime(2026, 8, 30, 9, 0, tzinfo=UTC) if reliable_reset else None
    status = QuotaStatus("free", 3, 2, 1, 0, reset_at)
    identity = AnonymousIdentity(
        id=uuid.uuid4(),
        expires_at=datetime(2027, 8, 29, tzinfo=UTC),
    )

    class StubQuotaService:
        def __init__(self, _settings: Settings) -> None:
            pass

        async def require_identity(self, **_kwargs: object) -> AnonymousIdentity:
            return identity

        @staticmethod
        def error_details(value: QuotaStatus) -> dict[str, object]:
            return QuotaService.error_details(value)

    download_service = MagicMock()
    download_service.create = AsyncMock(side_effect=FreeQuotaExceededError(status))
    app.state.download_job_service = download_service
    monkeypatch.setattr(media_downloads, "QuotaService", StubQuotaService)
    token = "A" * 43
    async with client:
        response = await client.post(
            f"/api/v1/media/jobs/{uuid.uuid4()}/downloads",
            json={"formatOptionId": "fmt_abc"},
            headers={
                "Authorization": f"Bearer {token}",
                "Cookie": "__Host-fetchnow_client=placeholder",
            },
        )
    assert response.status_code == 429
    error = response.json()["error"]
    assert error["code"] == "FREE_DOWNLOAD_QUOTA_EXHAUSTED"
    assert error["message"] == "The Free download quota is exhausted."
    assert error["details"] == {
        "limit": 3,
        "remaining": 0,
        "resetAt": "2026-08-30T09:00:00Z" if reliable_reset else None,
    }
    assert isinstance(error["request_id"], str)
    assert ("retry-after" in response.headers) is reliable_reset
