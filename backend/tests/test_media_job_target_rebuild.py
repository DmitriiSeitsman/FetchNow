"""Unit tests for rebuilding ResolutionResult from persisted job fields."""

from __future__ import annotations

import pytest

from fetchnow.core.config import Settings
from fetchnow.jobs.errors import JobError, JobErrorCode
from fetchnow.jobs.target_rebuild import rebuild_resolution_result
from fetchnow.media_inspection.defaults import build_default_inspection_registry
from fetchnow.resolution.errors import ResolutionError, ResolutionErrorKind
from fetchnow.url.providers import ProviderRegistry

_DB = "postgresql+asyncpg://fetchnow:fetchnow@localhost:5432/fetchnow"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        APP_ENV="test",
        DATABASE_URL=_DB,
        PROVIDER_VK_ENABLED=True,
        PROVIDER_RUTUBE_ENABLED=True,
    )


@pytest.fixture
def registries(settings: Settings) -> tuple[ProviderRegistry, object]:
    providers = ProviderRegistry.from_settings(settings)
    inspection = build_default_inspection_registry(settings, providers)
    return providers, inspection


@pytest.mark.asyncio
async def test_coherent_vk_target_rebuilds(
    settings: Settings,
    registries: tuple[ProviderRegistry, object],
) -> None:
    providers, inspection = registries
    result = await rebuild_resolution_result(
        provider_id="vk",
        canonical_provider_url="https://vk.com/video-123_456239017",
        media_id="-123_456239017",
        hostname="vk.com",
        path="/video-123_456239017",
        scheme="https",
        port=None,
        settings=settings,
        inspection_registry=inspection,  # type: ignore[arg-type]
        provider_registry=providers,
    )
    assert result.provider_id == "vk"
    assert result.canonical_provider_url == "https://vk.com/video-123_456239017"
    assert result.wrapper_type is None
    assert result.submitted_url == result.canonical_provider_url


@pytest.mark.asyncio
async def test_host_mismatch_never_builds(
    settings: Settings,
    registries: tuple[ProviderRegistry, object],
) -> None:
    providers, inspection = registries
    with pytest.raises(JobError) as exc_info:
        await rebuild_resolution_result(
            provider_id="vk",
            canonical_provider_url="https://vk.com/video-123_456239017",
            media_id="-123_456239017",
            hostname="rutube.ru",
            path="/video-123_456239017",
            scheme="https",
            port=None,
            settings=settings,
            inspection_registry=inspection,  # type: ignore[arg-type]
            provider_registry=providers,
        )
    assert exc_info.value.code == JobErrorCode.INTERNAL_ERROR
    assert exc_info.value.internal_reason == "PERSISTED_HOST_MISMATCH"


@pytest.mark.asyncio
async def test_query_in_canonical_never_builds(
    settings: Settings,
    registries: tuple[ProviderRegistry, object],
) -> None:
    providers, inspection = registries
    with pytest.raises(JobError) as exc_info:
        await rebuild_resolution_result(
            provider_id="vk",
            canonical_provider_url="https://vk.com/video-123_456239017?x=1",
            media_id="-123_456239017",
            hostname="vk.com",
            path="/video-123_456239017",
            scheme="https",
            port=None,
            settings=settings,
            inspection_registry=inspection,  # type: ignore[arg-type]
            provider_registry=providers,
        )
    assert exc_info.value.internal_reason == "PERSISTED_QUERY_OR_FRAGMENT"


@pytest.mark.asyncio
async def test_media_id_mismatch_never_builds(
    settings: Settings,
    registries: tuple[ProviderRegistry, object],
) -> None:
    providers, inspection = registries
    with pytest.raises(JobError) as exc_info:
        await rebuild_resolution_result(
            provider_id="vk",
            canonical_provider_url="https://vk.com/video-123_456239017",
            media_id="-999_111",
            hostname="vk.com",
            path="/video-123_456239017",
            scheme="https",
            port=None,
            settings=settings,
            inspection_registry=inspection,  # type: ignore[arg-type]
            provider_registry=providers,
        )
    assert exc_info.value.internal_reason == "PERSISTED_MEDIA_ID_MISMATCH"


@pytest.mark.asyncio
async def test_yandex_wrapper_host_rejected(
    settings: Settings,
    registries: tuple[ProviderRegistry, object],
) -> None:
    providers, inspection = registries
    with pytest.raises((JobError, ResolutionError)) as exc_info:
        await rebuild_resolution_result(
            provider_id="vk",
            canonical_provider_url="https://yandex.ru/video/preview/12345678901234567890",
            media_id="-123_456239017",
            hostname="yandex.ru",
            path="/video/preview/12345678901234567890",
            scheme="https",
            port=None,
            settings=settings,
            inspection_registry=inspection,  # type: ignore[arg-type]
            provider_registry=providers,
        )
    err = exc_info.value
    if isinstance(err, JobError):
        assert err.code in {
            JobErrorCode.INTERNAL_ERROR,
            JobErrorCode.MEDIA_INSPECTION_FAILED,
        }
    else:
        assert err.kind in {
            ResolutionErrorKind.RESOLVED_PROVIDER_UNSUPPORTED,
            ResolutionErrorKind.UNSAFE_RESOLUTION_TARGET,
            ResolutionErrorKind.WRAPPER_UNSUPPORTED,
        }
