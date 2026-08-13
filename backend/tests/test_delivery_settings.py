"""Delivery settings fail-closed tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fetchnow.core.config import Settings

_DB = "postgresql+asyncpg://unused@127.0.0.1:5432/unused"


def test_feature_disabled_by_default() -> None:
    s = Settings(APP_ENV="test", DATABASE_URL=_DB)
    assert s.media_delivery_enabled is False


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
