"""Public Premium status API contracts."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from fetchnow.api.main import create_app
from fetchnow.api.v1 import premium
from fetchnow.core.config import Settings
from fetchnow.premium.policy import PremiumCapability
from fetchnow.premium.service import PremiumStatus
from fetchnow.quota.service import AnonymousIdentity


def _settings(*, robokassa_mode: str = "test") -> Settings:
    if robokassa_mode == "disabled":
        return Settings(APP_ENV="test", ROBOKASSA_MODE="disabled")
    return Settings(
        APP_ENV="test",
        ROBOKASSA_MODE="test",
        ROBOKASSA_MERCHANT_LOGIN="demo",
        ROBOKASSA_SIGNATURE_ALGORITHM="sha256",
        ROBOKASSA_TEST_PASSWORD1="test-password-one",
        ROBOKASSA_TEST_PASSWORD2="test-password-two",
        ROBOKASSA_TEST_AMOUNT_MINOR=100,
        ROBOKASSA_RECEIPT_TAX="none",
        ROBOKASSA_RECEIPT_PAYMENT_METHOD="full_payment",
    )


class _IdentityService:
    identity_id = uuid.uuid4()

    def __init__(self, _settings: Settings) -> None:
        pass

    async def require_identity(self, **_kwargs: object) -> AnonymousIdentity:
        return AnonymousIdentity(self.identity_id, datetime(2027, 9, 1, tzinfo=UTC))


class _StubPremiumService:
    def __init__(self, status: PremiumStatus) -> None:
        self._status = status
        self.status_for_client = AsyncMock(return_value=status)


async def _client(
    monkeypatch: pytest.MonkeyPatch,
    premium_service: _StubPremiumService,
    *,
    settings: Settings | None = None,
) -> AsyncClient:
    configured = settings or _settings()
    app = create_app(configured)
    app.state.settings = configured
    app.state.premium_service = premium_service
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.commit = AsyncMock()
    app.state.session_factory = MagicMock(return_value=session)
    monkeypatch.setattr(premium, "QuotaService", _IdentityService)
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(
        "fetchnow.premium.repository.PremiumEntitlementRepository.database_now",
        AsyncMock(return_value=now),
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="https://test")


@pytest.mark.asyncio
async def test_premium_status_active_when_robokassa_mode_is_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expires = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    status = PremiumStatus(
        capability=PremiumCapability(
            is_premium=True,
            premium_expires_at=expires,
            product_code="premium_24h",
        ),
        entitlement=None,
    )
    async with await _client(
        monkeypatch,
        _StubPremiumService(status),
        settings=_settings(robokassa_mode="test"),
    ) as client:
        response = await client.get(
            "/api/v1/premium/status",
            headers={"Cookie": "__Host-fetchnow_client=abc"},
        )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "active": True,
        "expiresAt": "2026-09-02T12:00:00Z",
        "productCode": "premium_24h",
        "remainingSeconds": 86_400,
    }
    assert "provider" not in response.text.lower()
    assert "signature" not in response.text.lower()


@pytest.mark.asyncio
async def test_premium_status_active_when_robokassa_mode_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expires = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    status = PremiumStatus(
        capability=PremiumCapability(
            is_premium=True,
            premium_expires_at=expires,
            product_code="premium_24h",
        ),
        entitlement=None,
    )
    async with await _client(
        monkeypatch,
        _StubPremiumService(status),
        settings=_settings(robokassa_mode="disabled"),
    ) as client:
        response = await client.get(
            "/api/v1/premium/status",
            headers={"Cookie": "__Host-fetchnow_client=abc"},
        )
    assert response.status_code == 200
    assert response.json()["active"] is True
    assert response.json()["productCode"] == "premium_24h"


@pytest.mark.asyncio
async def test_premium_status_inactive_when_robokassa_mode_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = PremiumStatus(
        capability=PremiumCapability(
            is_premium=False, premium_expires_at=None, product_code=None
        ),
        entitlement=None,
    )
    async with await _client(
        monkeypatch,
        _StubPremiumService(status),
        settings=_settings(robokassa_mode="disabled"),
    ) as client:
        response = await client.get("/api/v1/premium/status")
    assert response.status_code == 200
    assert response.json() == {"active": False}


@pytest.mark.asyncio
async def test_premium_status_inactive_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    status = PremiumStatus(
        capability=PremiumCapability(
            is_premium=False, premium_expires_at=None, product_code=None
        ),
        entitlement=None,
    )
    async with await _client(monkeypatch, _StubPremiumService(status)) as client:
        response = await client.get("/api/v1/premium/status")
    assert response.status_code == 200
    assert response.json() == {"active": False}
