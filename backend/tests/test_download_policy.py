"""Canonical Free/Premium download policy tests for PRD2-A3.1."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from fetchnow.core.config import Settings
from fetchnow.premium.policy import PremiumCapability
from fetchnow.premium.service import PremiumStatus
from fetchnow.quota.policy import effective_download_policy
from fetchnow.quota.service import QuotaService

_DB = "postgresql+asyncpg://unused@127.0.0.1:5432/unused"


def _settings(*, robokassa_mode: str = "disabled") -> Settings:
    values: dict[str, object] = {}
    if robokassa_mode == "test":
        values.update(
            ROBOKASSA_MERCHANT_LOGIN="demo",
            ROBOKASSA_SIGNATURE_ALGORITHM="sha256",
            ROBOKASSA_TEST_PASSWORD1="password-one",
            ROBOKASSA_TEST_PASSWORD2="password-two",
            ROBOKASSA_TEST_AMOUNT_MINOR=1_000,
            ROBOKASSA_RECEIPT_TAX="none",
            ROBOKASSA_RECEIPT_PAYMENT_METHOD="full_payment",
        )
    return Settings(
        APP_ENV="test",
        DATABASE_URL=_DB,
        FREE_DOWNLOAD_LIMIT=3,
        FREE_DOWNLOAD_WINDOW_SECONDS=86_400,
        FREE_DELIVERY_RATE_LIMIT_ENABLED=True,
        FREE_DELIVERY_RATE_BYTES_PER_SECOND=524_288,
        ROBOKASSA_MODE=robokassa_mode,
        **values,
    )


def test_disabled_free_rate_switch_projects_none() -> None:
    policy = effective_download_policy(
        Settings(
            APP_ENV="test",
            DATABASE_URL=_DB,
            FREE_DOWNLOAD_LIMIT=3,
            FREE_DOWNLOAD_WINDOW_SECONDS=86_400,
            FREE_DELIVERY_RATE_LIMIT_ENABLED=False,
            FREE_DELIVERY_RATE_BYTES_PER_SECOND=524_288,
        )
    )
    assert policy.tier == "free"
    assert policy.delivery_rate_bytes_per_second is None


def test_disabled_free_rate_switch_agrees_with_delivery() -> None:
    from fetchnow.delivery.service import DeliveryService

    settings = Settings(
        APP_ENV="test",
        DATABASE_URL=_DB,
        FREE_DOWNLOAD_LIMIT=3,
        FREE_DOWNLOAD_WINDOW_SECONDS=86_400,
        FREE_DELIVERY_RATE_LIMIT_ENABLED=False,
        FREE_DELIVERY_RATE_BYTES_PER_SECOND=524_288,
    )
    policy = effective_download_policy(settings)
    delivery = DeliveryService(settings)
    assert policy.delivery_rate_bytes_per_second is None
    assert delivery.delivery_rate_bytes_per_second is None


def test_free_policy_rate_matches_delivery_fallback() -> None:
    from fetchnow.delivery.service import DeliveryService

    settings = _settings()
    policy = effective_download_policy(settings)
    delivery = DeliveryService(settings)
    assert policy.delivery_rate_bytes_per_second == 524_288
    assert delivery.delivery_rate_bytes_per_second == 524_288
    assert (
        policy.delivery_rate_bytes_per_second == delivery.delivery_rate_bytes_per_second
    )


def test_absent_or_inactive_entitlement_projects_free_policy() -> None:
    settings = _settings()
    for capability in (
        None,
        PremiumCapability(False, None, None),
    ):
        policy = effective_download_policy(settings, capability)
        assert policy.tier == "free"
        assert policy.download_limit == 3
        assert policy.quota_window_seconds == 86_400
        assert policy.delivery_rate_bytes_per_second == 524_288
        assert policy.premium_expires_at is None


@pytest.mark.parametrize("robokassa_mode", ["disabled", "test"])
def test_active_entitlement_projects_unlimited_policy_independent_of_provider(
    robokassa_mode: str,
) -> None:
    expires_at = datetime(2026, 9, 4, tzinfo=UTC)
    policy = effective_download_policy(
        _settings(robokassa_mode=robokassa_mode),
        PremiumCapability(True, expires_at, "premium_24h"),
    )
    assert policy.tier == "premium"
    assert policy.download_limit is None
    assert policy.quota_window_seconds is None
    assert policy.delivery_rate_bytes_per_second is None
    assert policy.premium_expires_at == expires_at


@pytest.mark.asyncio
async def test_quota_policy_resolver_uses_a2_capability_and_propagates_failure() -> (
    None
):
    expires_at = datetime.now(tz=UTC) + timedelta(hours=1)

    class PremiumStub:
        async def status_for_client(self, **_kwargs: object) -> PremiumStatus:
            return PremiumStatus(
                capability=PremiumCapability(True, expires_at, "premium_24h"),
                entitlement=None,
            )

    policy = await QuotaService(
        _settings(),
        premium_service=PremiumStub(),  # type: ignore[arg-type]
    ).resolve_policy(identity_id=uuid.uuid4(), session=object())  # type: ignore[arg-type]
    assert policy.tier == "premium"
    assert policy.premium_expires_at == expires_at

    class FailingPremiumStub:
        async def status_for_client(self, **_kwargs: object) -> PremiumStatus:
            raise RuntimeError("database unavailable")

    with pytest.raises(RuntimeError, match="database unavailable"):
        await QuotaService(
            _settings(),
            premium_service=FailingPremiumStub(),  # type: ignore[arg-type]
        ).resolve_policy(identity_id=uuid.uuid4(), session=object())  # type: ignore[arg-type]
