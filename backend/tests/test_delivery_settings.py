"""Delivery settings fail-closed tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from fetchnow.core.config import Settings
from fetchnow.delivery.service import DeliveryAuthorization, DeliveryService
from fetchnow.premium.policy import PremiumCapability
from fetchnow.quota.policy import effective_download_policy

_DB = "postgresql+asyncpg://unused@127.0.0.1:5432/unused"


def test_feature_disabled_by_default() -> None:
    s = Settings(APP_ENV="test", DATABASE_URL=_DB)
    assert s.media_delivery_enabled is False
    assert s.free_delivery_rate_limit_enabled is False
    assert s.free_delivery_rate_bytes_per_second == 524_288


def test_enabled_requires_root() -> None:
    with pytest.raises(ValidationError):
        Settings(
            APP_ENV="test",
            DATABASE_URL=_DB,
            MEDIA_DELIVERY_ENABLED=True,
            MEDIA_DELIVERY_ROOT="",
        )


def test_relative_root_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(
            APP_ENV="test",
            DATABASE_URL=_DB,
            MEDIA_DELIVERY_ENABLED=True,
            MEDIA_DELIVERY_ROOT="relative/path",
        )


def test_chunk_and_concurrency_bounds() -> None:
    with pytest.raises(ValidationError):
        Settings(
            APP_ENV="test",
            DATABASE_URL=_DB,
            MEDIA_DELIVERY_CHUNK_BYTES=1,
        )
    with pytest.raises(ValidationError):
        Settings(
            APP_ENV="test",
            DATABASE_URL=_DB,
            MEDIA_DELIVERY_CONCURRENCY=0,
        )
    s = Settings(
        APP_ENV="test",
        DATABASE_URL=_DB,
        MEDIA_DELIVERY_ENABLED=True,
        MEDIA_DELIVERY_ROOT="/var/lib/fetchnow/tmp/downloads",
        MEDIA_DELIVERY_CHUNK_BYTES=65536,
        MEDIA_DELIVERY_CONCURRENCY=8,
    )
    assert s.media_delivery_chunk_bytes == 65536
    assert s.media_delivery_concurrency == 8


@pytest.mark.parametrize("rate", [0, -1, 262_143, 67_108_865])
def test_free_delivery_rate_rejects_invalid_and_absurd_values(rate: int) -> None:
    with pytest.raises(ValidationError):
        Settings(
            APP_ENV="test",
            DATABASE_URL=_DB,
            FREE_DELIVERY_RATE_BYTES_PER_SECOND=rate,
        )


@pytest.mark.parametrize("rate", [262_144, 67_108_864])
def test_free_delivery_rate_accepts_validation_boundaries(rate: int) -> None:
    settings = Settings(
        APP_ENV="test",
        DATABASE_URL=_DB,
        FREE_DELIVERY_RATE_BYTES_PER_SECOND=rate,
    )
    assert settings.free_delivery_rate_bytes_per_second == rate


def test_effective_free_policy_projects_delivery_and_capabilities() -> None:
    disabled = effective_download_policy(Settings(APP_ENV="test", DATABASE_URL=_DB))
    assert disabled.tier == "free"
    assert disabled.delivery_rate_bytes_per_second is None
    assert disabled.allow_combined is True
    assert disabled.allow_audio_only is False
    assert disabled.allow_video_only is False

    enabled = effective_download_policy(
        Settings(
            APP_ENV="test",
            DATABASE_URL=_DB,
            FREE_DELIVERY_RATE_LIMIT_ENABLED=True,
            FREE_DELIVERY_RATE_BYTES_PER_SECOND=524_288,
        )
    )
    assert enabled.delivery_rate_bytes_per_second == 524_288


def test_delivery_uses_admission_policy_snapshot_without_expiry_polling() -> None:
    settings = Settings(
        APP_ENV="test",
        DATABASE_URL=_DB,
        FREE_DELIVERY_RATE_LIMIT_ENABLED=True,
        FREE_DELIVERY_RATE_BYTES_PER_SECOND=524_288,
    )
    service = DeliveryService(settings)
    now = datetime.now(tz=UTC)
    admitted_premium = effective_download_policy(
        settings,
        PremiumCapability(True, now - timedelta(seconds=1), "premium_24h"),
    )
    premium_authz = DeliveryAuthorization(
        job=MagicMock(), now=now, policy=admitted_premium
    )
    assert service.delivery_rate_bytes_per_second_for(premium_authz) is None

    new_free_authz = DeliveryAuthorization(
        job=MagicMock(), now=now, policy=effective_download_policy(settings)
    )
    assert service.delivery_rate_bytes_per_second_for(new_free_authz) == 524_288
