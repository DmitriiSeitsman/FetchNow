"""Provider capability matrix and download-path enforcement tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from fetchnow.capabilities.models import (
    CapabilityState,
    ContentKind,
    MediaOperation,
    MetadataField,
    ProviderCapabilities,
)
from fetchnow.capabilities.registry import (
    DEFAULT_PROVIDER_CAPABILITIES,
    ProviderCapabilityRegistry,
    assert_operation_allowed,
)
from fetchnow.core.config import Settings
from fetchnow.downloads.errors import DownloadError, DownloadErrorCode
from fetchnow.downloads.executor import DownloadClaimSnapshot, DownloadExecutor
from fetchnow.downloads.service import DownloadJobService
from fetchnow.jobs.credentials import generate_access_token
from fetchnow.jobs.metadata_codec import media_metadata_to_jsonable
from fetchnow.jobs.models import MediaJob
from fetchnow.jobs.states import MediaJobState
from fetchnow.media_inspection.models import (
    CodecFamily,
    FormatCategory,
    MediaFormat,
    MediaMetadata,
)
from fetchnow.url.models import ProviderID

_DB = "postgresql+asyncpg://fetchnow:fetchnow@localhost:5432/fetchnow"


def _settings() -> Settings:
    return Settings(
        APP_ENV="test",
        LOG_LEVEL="WARNING",
        DATABASE_URL=_DB,
        MEDIA_DOWNLOADS_ENABLED=True,
        PROVIDER_VK_ENABLED=True,
        PROVIDER_RUTUBE_ENABLED=True,
    )


def _registry_with(
    *,
    provider_id: ProviderID,
    operation: MediaOperation,
    state: CapabilityState,
) -> ProviderCapabilityRegistry:
    descriptors = dict(DEFAULT_PROVIDER_CAPABILITIES)
    base = descriptors[provider_id]
    operations = dict(base.operations)
    operations[operation] = state
    descriptors[provider_id] = ProviderCapabilities(
        provider_id=provider_id,
        operations=operations,
        content_kinds=base.content_kinds,
        metadata=base.metadata,
    )
    return ProviderCapabilityRegistry.from_mapping(descriptors)


def _format() -> MediaFormat:
    return MediaFormat(
        format_option_id="fmt_abc123",
        container="mp4",
        width=1280,
        height=720,
        fps=30.0,
        has_video=True,
        has_audio=True,
        category=FormatCategory.PROGRESSIVE,
        video_codec=CodecFamily.AVC,
        audio_codec=CodecFamily.AAC,
        approx_bytes=100,
        quality_label="p720",
        free_tier_eligible=True,
    )


def _parent() -> MediaJob:
    now = datetime.now(tz=UTC)
    metadata = MediaMetadata(
        provider_id="vk",
        canonical_provider_url="https://vk.com/video-1_2",
        media_id="-1_2",
        title="Example",
        duration_seconds=60,
        formats=(_format(),),
        extraction_tool="ytdlp",
        extraction_tool_version=None,
        muxing_required=False,
    )
    return MediaJob(
        id=uuid.uuid4(),
        schema_version=1,
        public_state=MediaJobState.INSPECTED.value,
        credential_hash="credential-hash",
        request_fingerprint="request-fingerprint",
        provider_id="vk",
        canonical_provider_url="https://vk.com/video-1_2",
        media_id="-1_2",
        hostname="vk.com",
        path="/video-1_2",
        scheme="https",
        port=None,
        result_metadata=media_metadata_to_jsonable(metadata, max_bytes=65_536),
        attempt_count=1,
        max_attempts=3,
        available_at=now,
        fence_token=1,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=1),
    )


@pytest.mark.parametrize("provider_id", list(ProviderID))
def test_default_matrix_is_complete(provider_id: ProviderID) -> None:
    descriptor = ProviderCapabilityRegistry.default().require(provider_id.value)
    assert descriptor.provider_id is provider_id
    assert set(descriptor.operations) == set(MediaOperation)
    assert set(descriptor.content_kinds) == set(ContentKind)
    assert set(descriptor.metadata) == set(MetadataField)


def test_capability_models_are_immutable() -> None:
    registry = ProviderCapabilityRegistry.default()
    descriptor = registry.require(ProviderID.VK.value)
    with pytest.raises(TypeError):
        descriptor.operations[MediaOperation.DOWNLOAD_VIDEO] = CapabilityState.DISABLED
    with pytest.raises(AttributeError):
        descriptor.provider_id = ProviderID.RUTUBE  # type: ignore[misc]
    with pytest.raises(AttributeError):
        registry._capabilities = MappingProxyType({})  # type: ignore[misc]


def test_incomplete_matrix_fails_closed() -> None:
    with pytest.raises(ValueError, match="ProviderID"):
        ProviderCapabilityRegistry.from_mapping(
            {ProviderID.VK: DEFAULT_PROVIDER_CAPABILITIES[ProviderID.VK]}
        )
    with pytest.raises(ValueError, match="MediaOperation"):
        ProviderCapabilities(
            provider_id=ProviderID.VK,
            operations=MappingProxyType({}),
            content_kinds=DEFAULT_PROVIDER_CAPABILITIES[ProviderID.VK].content_kinds,
            metadata=DEFAULT_PROVIDER_CAPABILITIES[ProviderID.VK].metadata,
        )


def test_unknown_provider_is_rejected() -> None:
    registry = ProviderCapabilityRegistry.default()
    assert registry.get("unknown_provider") is None
    with pytest.raises(DownloadError) as exc:
        assert_operation_allowed(
            registry,
            provider_id="unknown_provider",
            operation=MediaOperation.DOWNLOAD_VIDEO,
        )
    assert exc.value.code is DownloadErrorCode.PROVIDER_CAPABILITY_DISABLED


@pytest.mark.parametrize("provider_id", list(ProviderID))
@pytest.mark.parametrize("operation", list(MediaOperation))
def test_enqueue_and_worker_share_operation_verdict(
    provider_id: ProviderID, operation: MediaOperation
) -> None:
    registry = ProviderCapabilityRegistry.default()
    for _path in ("enqueue", "worker"):
        if registry.require(provider_id.value).allows(operation):
            assert_operation_allowed(
                registry, provider_id=provider_id.value, operation=operation
            )
        else:
            with pytest.raises(DownloadError) as exc:
                assert_operation_allowed(
                    registry, provider_id=provider_id.value, operation=operation
                )
            assert exc.value.code is DownloadErrorCode.PROVIDER_CAPABILITY_DISABLED


def test_current_vk_product_policy() -> None:
    capabilities = ProviderCapabilityRegistry.default().require(ProviderID.VK.value)
    assert (
        capabilities.operations[MediaOperation.EXTRACT_AUDIO]
        is CapabilityState.DISABLED
    )
    assert capabilities.metadata[MetadataField.THUMBNAIL] is CapabilityState.PLANNED
    assert capabilities.content_kinds[ContentKind.LIVE] is CapabilityState.DISABLED
    assert capabilities.content_kinds[ContentKind.PLAYLIST] is CapabilityState.DISABLED


@pytest.mark.asyncio
async def test_enqueue_rejects_disabled_operation_before_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _parent()
    service = DownloadJobService(
        _settings(),
        capability_registry=_registry_with(
            provider_id=ProviderID.VK,
            operation=MediaOperation.DOWNLOAD_VIDEO,
            state=CapabilityState.DISABLED,
        ),
    )
    session = MagicMock(spec=AsyncSession)
    monkeypatch.setattr(
        "fetchnow.downloads.service.MediaJobRepository.get_by_id",
        AsyncMock(return_value=parent),
    )
    monkeypatch.setattr(
        "fetchnow.downloads.service.MediaJobRepository.database_now",
        AsyncMock(return_value=datetime.now(tz=UTC)),
    )
    monkeypatch.setattr("fetchnow.downloads.service.tokens_match", lambda *_args: True)
    enqueue = AsyncMock()
    monkeypatch.setattr(
        "fetchnow.downloads.service.MediaDownloadJobRepository.enqueue_or_get",
        enqueue,
    )

    with pytest.raises(DownloadError) as exc:
        await service.create(
            media_job_id=parent.id,
            format_option_id="fmt_abc123",
            access_token=generate_access_token(),
            session=session,
        )

    assert exc.value.code is DownloadErrorCode.PROVIDER_CAPABILITY_DISABLED
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_rechecks_disabled_operation_before_inspection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    executor = DownloadExecutor(
        settings,
        session_factory=MagicMock(),
        inspection_service=MagicMock(),
        inspection_registry=MagicMock(),
        capability_registry=_registry_with(
            provider_id=ProviderID.VK,
            operation=MediaOperation.DOWNLOAD_VIDEO,
            state=CapabilityState.DISABLED,
        ),
        artifact_store=MagicMock(),
        worker_id="test-worker",
    )
    snap = DownloadClaimSnapshot(
        job_id=uuid.uuid4(),
        media_job_id=uuid.uuid4(),
        fence=1,
        attempt_count=1,
        format_option_id="fmt_abc123",
        provider_id="vk",
        canonical_provider_url="https://vk.com/video-1_2",
        media_id="-1_2",
        hostname="vk.com",
        path="/video-1_2",
        scheme="https",
        port=None,
        selected_format_snapshot={"formatOptionId": "fmt_abc123"},
        expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
    )
    resolution = MagicMock(provider_id="vk")
    monkeypatch.setattr(
        "fetchnow.downloads.executor.rebuild_resolution_result",
        AsyncMock(return_value=resolution),
    )
    inspect_draft = AsyncMock()
    executor._inspection.inspect_draft = inspect_draft

    with pytest.raises(DownloadError) as exc:
        await executor._resolve_selection(snap)

    assert exc.value.code is DownloadErrorCode.PROVIDER_CAPABILITY_DISABLED
    inspect_draft.assert_not_awaited()
