"""Registry immutability and orchestration tests for media inspection."""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

import pytest

from fetchnow.media_inspection.defaults import (
    build_default_inspection_registry,
    build_media_inspection_service,
)
from fetchnow.media_inspection.errors import InspectionError, InspectionErrorKind
from fetchnow.media_inspection.models import (
    CodecFamily,
    ExtractedMediaDraft,
    InternalFormatCandidate,
)
from fetchnow.media_inspection.protocols import InspectionTarget, ProcessResult
from fetchnow.media_inspection.registry import (
    DEFAULT_YTDLP_EXTRACTOR_KEYS,
    ExtractorBinding,
    InspectionExtractorRegistry,
)
from fetchnow.media_inspection.service import MediaInspectionService
from fetchnow.resolution.models import ResolutionProvenance
from fetchnow.url.models import ProviderID
from fetchnow.url.providers import ProviderDescriptor, ProviderRegistry
from media_inspection_fixtures import VK_FIXTURE, dumps_fixture
from media_inspection_helpers import (
    FakeRunner,
    make_regular_executable,
    make_temp_root,
    providers,
    resolution,
    settings,
)


class _StubExtractor:
    def __init__(self, draft: ExtractedMediaDraft) -> None:
        self._draft = draft
        self.calls: list[InspectionTarget] = []
        self.mutable_hosts: set[str] = set()

    @property
    def extractor_id(self) -> str:
        return "stub"

    async def extract(self, target: InspectionTarget) -> ExtractedMediaDraft:
        self.calls.append(target)
        self.mutable_hosts.add("evil.example")
        return self._draft


def _draft(
    *,
    provider_id: str = "vk",
    url: str = "https://vk.com/video-1_2",
    media_id: str = "-1_2",
) -> ExtractedMediaDraft:
    return ExtractedMediaDraft(
        provider_id=provider_id,
        canonical_provider_url=url,
        media_id=media_id,
        title="ok",
        duration_seconds=10,
        candidates=(
            InternalFormatCandidate(
                container="mp4",
                width=1280,
                height=720,
                fps=30.0,
                has_video=True,
                has_audio=True,
                video_codec=CodecFamily.AVC,
                audio_codec=CodecFamily.AAC,
                approx_bytes=1_000_000,
            ),
        ),
        extractor_key="vk",
        tool_version="2026.7.4",
    )


def test_registry_constructor_bypass_possible_false() -> None:
    with pytest.raises(InspectionError) as exc:
        InspectionExtractorRegistry()  # type: ignore[call-arg]
    assert exc.value.kind is InspectionErrorKind.INTERNAL_INSPECTION_ERROR
    with pytest.raises(InspectionError):
        InspectionExtractorRegistry(
            bindings=(),
            _by_provider={},
        )  # type: ignore[call-arg]


def test_registry_mapping_mutation_possible_false() -> None:
    draft = _draft()
    stub = _StubExtractor(draft)
    registry = InspectionExtractorRegistry.from_bindings(
        [
            ExtractorBinding(
                provider_id="vk",
                exact_hostnames=frozenset({"vk.com"}),
                allowed_extractor_keys=frozenset({"vk"}),
                extractor_id="stub",
                extractor=stub,
            )
        ]
    )
    assert isinstance(registry._by_provider, MappingProxyType)
    with pytest.raises(TypeError):
        registry._by_provider["vk"] = stub  # type: ignore[index]
    with pytest.raises(TypeError):
        registry._by_host["evil.example"] = registry.bindings[0]  # type: ignore[index]


def test_registry_attribute_rebind_possible_false() -> None:
    draft = _draft()
    stub = _StubExtractor(draft)
    registry = InspectionExtractorRegistry.from_bindings(
        [
            ExtractorBinding(
                provider_id="vk",
                exact_hostnames=frozenset({"vk.com"}),
                allowed_extractor_keys=frozenset({"vk"}),
                extractor_id="stub",
                extractor=stub,
            )
        ]
    )
    with pytest.raises(AttributeError):
        registry.bindings = ()  # type: ignore[misc]
    with pytest.raises(AttributeError):
        del registry._bindings  # type: ignore[attr-defined]


def test_default_extractor_allowlist_mutation_possible_false() -> None:
    assert isinstance(DEFAULT_YTDLP_EXTRACTOR_KEYS, MappingProxyType)
    with pytest.raises(TypeError):
        DEFAULT_YTDLP_EXTRACTOR_KEYS["vk"] = frozenset({"generic"})  # type: ignore[index]
    with pytest.raises(TypeError):
        DEFAULT_YTDLP_EXTRACTOR_KEYS["youtube"] = frozenset({"youtube"})  # type: ignore[index]


@pytest.mark.asyncio
async def test_stateful_extractor_can_widen_policy_false() -> None:
    draft = _draft()
    stub = _StubExtractor(draft)
    registry = InspectionExtractorRegistry.from_bindings(
        [
            ExtractorBinding(
                provider_id="vk",
                exact_hostnames=frozenset({"vk.com"}),
                allowed_extractor_keys=frozenset({"vk"}),
                extractor_id="stub",
                extractor=stub,
            )
        ]
    )
    stub.mutable_hosts.add("evil.example")
    assert "evil.example" not in registry.owned_hostnames()
    with pytest.raises(InspectionError) as exc:
        registry.resolve(provider_id="vk", hostname="evil.example")
    assert exc.value.kind is InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH

    svc = MediaInspectionService(settings=settings(), registry=registry)
    meta = await svc.inspect(
        resolution(
            provider_id="vk",
            canonical="https://vk.com/video-1_2",
            hostname="vk.com",
            path="/video-1_2",
        )
    )
    assert meta.provider_id == "vk"
    assert meta.media_id == "-1_2"
    assert "evil.example" not in registry.owned_hostnames()


@pytest.mark.asyncio
async def test_direct_vk_success(tmp_path: Path) -> None:
    runner = FakeRunner(
        ProcessResult(
            exit_code=0,
            stdout=dumps_fixture(VK_FIXTURE),
            stderr=b"",
            timed_out=False,
            cancelled=False,
        )
    )
    s = settings(
        MEDIA_INSPECTION_YTDLP_PATH=make_regular_executable(tmp_path),
        MEDIA_INSPECTION_TEMP_ROOT=make_temp_root(tmp_path),
    )
    svc = build_media_inspection_service(s, providers(s), runner=runner)
    result = await svc.inspect(
        resolution(
            provider_id="vk",
            canonical="https://vk.com/video-123_456239017",
            hostname="vk.com",
            path="/video-123_456239017",
        )
    )
    assert result.provider_id == "vk"
    assert result.media_id == "-123_456239017"
    assert result.canonical_provider_url == "https://vk.com/video-123_456239017"
    assert "?" not in result.canonical_provider_url
    assert any(f.free_tier_eligible for f in result.formats)
    assert any(
        not f.free_tier_eligible and (f.height or 0) > 720 for f in result.formats
    )
    assert runner.calls
    assert "--skip-download" in runner.calls[0]["argv"]
    assert "https://vk.com/video-123_456239017" in runner.calls[0]["argv"]


@pytest.mark.asyncio
async def test_direct_rutube_success(tmp_path: Path) -> None:
    from media_inspection_fixtures import RUTUBE_FIXTURE

    runner = FakeRunner(
        ProcessResult(
            exit_code=0,
            stdout=dumps_fixture(RUTUBE_FIXTURE),
            stderr=b"",
            timed_out=False,
            cancelled=False,
        )
    )
    s = settings(
        MEDIA_INSPECTION_YTDLP_PATH=make_regular_executable(tmp_path),
        MEDIA_INSPECTION_TEMP_ROOT=make_temp_root(tmp_path),
    )
    svc = build_media_inspection_service(s, providers(s), runner=runner)
    result = await svc.inspect(
        resolution(
            provider_id="rutube",
            canonical="https://rutube.ru/video/abc123def456/",
            hostname="rutube.ru",
            path="/video/abc123def456/",
        )
    )
    assert result.provider_id == "rutube"
    assert result.media_id == "abc123def456"


@pytest.mark.asyncio
async def test_yandex_wrapper_uses_resolved_provider_not_wrapper_url(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(
        ProcessResult(
            exit_code=0,
            stdout=dumps_fixture(VK_FIXTURE),
            stderr=b"",
            timed_out=False,
            cancelled=False,
        )
    )
    s = settings(
        MEDIA_INSPECTION_YTDLP_PATH=make_regular_executable(tmp_path),
        MEDIA_INSPECTION_TEMP_ROOT=make_temp_root(tmp_path),
    )
    svc = build_media_inspection_service(s, providers(s), runner=runner)
    await svc.inspect(
        resolution(
            provider_id="vk",
            canonical="https://vk.com/video-123_456239017",
            hostname="vk.com",
            path="/video-123_456239017",
            provenance=ResolutionProvenance.WRAPPER_RESOLVED,
        )
    )
    tool_url = runner.calls[0]["argv"][-1]
    assert "yandex" not in tool_url
    assert tool_url.startswith("https://vk.com/")


@pytest.mark.asyncio
async def test_unsupported_provider_rejected_before_tool(tmp_path: Path) -> None:
    runner = FakeRunner()
    s = settings(
        MEDIA_INSPECTION_YTDLP_PATH=make_regular_executable(tmp_path),
        MEDIA_INSPECTION_TEMP_ROOT=make_temp_root(tmp_path),
    )
    svc = build_media_inspection_service(s, providers(s), runner=runner)
    with pytest.raises(InspectionError) as exc:
        await svc.inspect(
            resolution(
                provider_id="youtube",
                canonical="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                hostname="www.youtube.com",
            )
        )
    assert exc.value.kind in {
        InspectionErrorKind.INSPECTION_UNSUPPORTED_PROVIDER,
        InspectionErrorKind.INSPECTION_POLICY_REJECTED,
        InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH,
    }
    assert runner.calls == []


def test_empty_registry_fail_closed() -> None:
    with pytest.raises(InspectionError) as exc:
        InspectionExtractorRegistry.from_bindings([])
    assert exc.value.kind is InspectionErrorKind.INSPECTION_UNSUPPORTED_PROVIDER


def test_disabled_providers_yield_empty_fail_closed() -> None:
    s = settings(PROVIDER_VK_ENABLED=False, PROVIDER_RUTUBE_ENABLED=False)
    empty_providers = ProviderRegistry.from_descriptors(
        (
            ProviderDescriptor(
                id=ProviderID.VK.value,
                display_name="VK",
                exact_hostnames=frozenset({"vk.com"}),
                enabled=False,
            ),
        )
    )
    with pytest.raises(InspectionError):
        build_default_inspection_registry(s, empty_providers)


def test_default_extractor_keys_exclude_generic() -> None:
    for keys in DEFAULT_YTDLP_EXTRACTOR_KEYS.values():
        assert "generic" not in keys
        assert "default" not in keys


def test_generic_extractor_key_rejected_at_registry_build() -> None:
    draft = _draft()
    stub = _StubExtractor(draft)
    with pytest.raises(InspectionError):
        InspectionExtractorRegistry.from_bindings(
            [
                ExtractorBinding(
                    provider_id="vk",
                    exact_hostnames=frozenset({"vk.com"}),
                    allowed_extractor_keys=frozenset({"vk", "generic"}),
                    extractor_id="stub",
                    extractor=stub,
                )
            ]
        )
