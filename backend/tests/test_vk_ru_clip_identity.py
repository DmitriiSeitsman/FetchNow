"""PR13: vk.ru exact hosts and /clip ↔ /video stable identity."""

from __future__ import annotations

import copy
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import quote

import pytest

from fetchnow.core.config import Settings
from fetchnow.media_inspection.errors import InspectionError, InspectionErrorKind
from fetchnow.media_inspection.identity import (
    build_canonical_provider_url,
    parse_identity_from_url,
    parse_provider_media_identity,
)
from fetchnow.media_inspection.ytdlp_parse import parse_ytdlp_json
from fetchnow.network.client import SafeHTTPClient
from fetchnow.resolution.defaults import build_wrapper_registry
from fetchnow.resolution.models import ResolutionProvenance
from fetchnow.resolution.service import ResolutionService
from fetchnow.resolution.yandex_preview import (
    _is_stable_provider_path,
    _normalize_stable_provider_url,
)
from fetchnow.url.dns import FakeDnsResolver
from fetchnow.url.errors import URLValidationError
from fetchnow.url.models import ProviderID
from fetchnow.url.providers import ProviderRegistry, build_default_providers
from fetchnow.url.validate import URLValidator
from media_inspection_fixtures import VK_FIXTURE, dumps_fixture

_VK_HOSTS = frozenset(
    {
        "vk.com",
        "www.vk.com",
        "m.vk.com",
        "vk.ru",
        "www.vk.ru",
        "m.vk.ru",
        "vkvideo.ru",
        "www.vkvideo.ru",
        "m.vkvideo.ru",
    }
)

_CLIP_MEDIA_ID = "-235548483_456239236"
_CLIP_OWNER = "-235548483"
_CLIP_VIDEO = "456239236"


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "APP_ENV": "test",
        "LOG_LEVEL": "WARNING",
        "PROVIDER_VK_ENABLED": True,
        "PROVIDER_RUTUBE_ENABLED": True,
        "URL_ALLOWED_SCHEMES": "http,https",
        "URL_ALLOWED_PORTS": "80,443",
        "URL_MAX_LENGTH": 4096,
        "DNS_RESOLUTION_TIMEOUT_SECONDS": 1,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_resolution_package_does_not_import_media_inspection() -> None:
    import inspect

    import fetchnow.resolution.service as service_mod
    import fetchnow.resolution.yandex_preview as yandex_mod
    import fetchnow.url.provider_identity as identity_mod

    for module in (service_mod, yandex_mod, identity_mod):
        source = inspect.getsource(module)
        assert "fetchnow.media_inspection" not in source
        assert "fetchnow.jobs" not in source
        assert "fetchnow.downloads" not in source
        assert "fetchnow.api" not in source


def test_provider_identity_is_shared_neutral_grammar() -> None:
    from fetchnow.url.provider_identity import parse_stable_provider_identity

    video = parse_stable_provider_identity(
        provider_id="vk",
        hostname="vk.ru",
        path="/video-1_2",
        allowed_hostnames=_VK_HOSTS,
    )
    clip = parse_stable_provider_identity(
        provider_id="vk",
        hostname="vk.ru",
        path="/clip-1_2",
        allowed_hostnames=_VK_HOSTS,
    )
    assert video.media_id == clip.media_id == "-1_2"
    assert video.canonical_path == clip.canonical_path == "/video-1_2"


def test_provider_registry_accepts_nine_exact_vk_hosts() -> None:
    providers = build_default_providers(_settings())
    vk = next(p for p in providers if p.id == ProviderID.VK.value)
    assert vk.exact_hostnames == _VK_HOSTS
    for host in _VK_HOSTS:
        assert ProviderRegistry.from_descriptors(providers).find(host) is not None


@pytest.mark.parametrize(
    "host",
    [
        "login.vk.ru",
        "api.vk.ru",
        "evil.vk.ru",
        "vk.ru.evil.test",
        "notvk.ru",
        "vkru.example",
        "vk.com.evil.test",
        "m.vk.ru.attacker",
        "www.vk.ru.evil",
    ],
)
def test_provider_registry_rejects_vk_lookalikes(host: str) -> None:
    registry = ProviderRegistry.from_settings(_settings())
    assert registry.find(host) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("host", sorted(_VK_HOSTS))
async def test_url_validator_accepts_exact_vk_hosts(host: str) -> None:
    validator = URLValidator(
        _settings(),
        registry=ProviderRegistry.from_settings(_settings()),
        resolver=FakeDnsResolver(default_addresses=("8.8.8.8",)),
    )
    result = await validator.validate(f"https://{host}/clip-1_2")
    assert result.provider_id == ProviderID.VK
    assert result.url.hostname == host


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://login.vk.ru/clip-1_2",
        "https://evil.vk.ru/clip-1_2",
        "https://vk.ru.evil.test/clip-1_2",
        "https://notvk.ru/clip-1_2",
    ],
)
async def test_url_validator_rejects_lookalikes_before_tool(url: str) -> None:
    resolver = FakeDnsResolver(default_addresses=("8.8.8.8",))
    validator = URLValidator(
        _settings(),
        registry=ProviderRegistry.from_settings(_settings()),
        resolver=resolver,
    )
    with pytest.raises(URLValidationError) as exc:
        await validator.validate(url)
    assert exc.value.code == "UNSUPPORTED_PROVIDER"


@pytest.mark.parametrize(
    "path",
    [
        "/video123_456",
        "/video-123_456",
        "/clip123_456",
        "/clip-123_456",
        "/video123_456/",
        "/clip-123_456/",
    ],
)
def test_identity_accepts_video_and_clip_shapes(path: str) -> None:
    identity = parse_provider_media_identity(
        provider_id="vk",
        hostname="vk.ru",
        path=path,
        allowed_hostnames=_VK_HOSTS,
    )
    owner = path.rstrip("/").split("_")[0]
    owner = owner.removeprefix("/video").removeprefix("/clip")
    video = path.rstrip("/").rsplit("_", 1)[1]
    assert identity.media_id == f"{owner}_{video}"
    assert identity.canonical_path == f"/video{owner}_{video}"


@pytest.mark.parametrize(
    "path",
    [
        "/video--123_456",
        "/clip--123_456",
        "/clip123_-456",
        "/clip+123_456",
        "/clip",
        "/clips-123_456",
        "/clip-123",
        "/clip-123_456/extra",
        "/video_ext.php",
        "/embed/1",
        f"/clip-123_456{quote('/', safe='')}",
        "/clip%2D123_456",
    ],
)
def test_identity_rejects_malformed_clip_and_forbidden_paths(path: str) -> None:
    with pytest.raises(InspectionError) as exc:
        parse_provider_media_identity(
            provider_id="vk",
            hostname="vk.ru",
            path=path,
            allowed_hostnames=_VK_HOSTS,
        )
    assert exc.value.kind in {
        InspectionErrorKind.INSPECTION_POLICY_REJECTED,
        InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH,
    }


def test_video_and_clip_identity_equal_and_canonical_deterministic() -> None:
    video = parse_provider_media_identity(
        provider_id="vk",
        hostname="vk.ru",
        path="/video-235548483_456239236",
        allowed_hostnames=_VK_HOSTS,
    )
    clip = parse_provider_media_identity(
        provider_id="vk",
        hostname="vk.ru",
        path="/clip-235548483_456239236",
        allowed_hostnames=_VK_HOSTS,
    )
    assert video.media_id == clip.media_id == _CLIP_MEDIA_ID
    assert video.canonical_path == clip.canonical_path == (
        f"/video{_CLIP_OWNER}_{_CLIP_VIDEO}"
    )
    assert build_canonical_provider_url(
        hostname="vk.ru", canonical_path=video.canonical_path
    ) == build_canonical_provider_url(
        hostname="vk.ru", canonical_path=clip.canonical_path
    )
    assert build_canonical_provider_url(
        hostname="vk.ru", canonical_path=clip.canonical_path
    ) == f"https://vk.ru/video{_CLIP_OWNER}_{_CLIP_VIDEO}"


def test_yandex_normalize_rewrites_clip_to_video() -> None:
    url, upgraded = _normalize_stable_provider_url(
        "http://vk.ru/clip-235548483_456239236",
        supported_hosts=_VK_HOSTS,
        allow_http_upgrade=True,
    )
    assert upgraded is True
    assert url == "https://vk.ru/video-235548483_456239236"
    assert _is_stable_provider_path("vk.ru", "/clip-1_2") is True
    assert _is_stable_provider_path("vk.ru", "/clips-1_2") is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "hostname", "path", "canonical"),
    [
        (
            "https://vk.ru/clip-235548483_456239236",
            "vk.ru",
            "/video-235548483_456239236",
            "https://vk.ru/video-235548483_456239236",
        ),
        (
            "https://www.vk.ru/clip123_456",
            "www.vk.ru",
            "/video123_456",
            "https://www.vk.ru/video123_456",
        ),
        (
            "https://m.vk.ru/clip-1_2",
            "m.vk.ru",
            "/video-1_2",
            "https://m.vk.ru/video-1_2",
        ),
        (
            "https://vk.ru/video-235548483_456239236",
            "vk.ru",
            "/video-235548483_456239236",
            "https://vk.ru/video-235548483_456239236",
        ),
        (
            "https://vk.ru/clip-235548483_456239236?list=x",
            "vk.ru",
            "/video-235548483_456239236",
            "https://vk.ru/video-235548483_456239236",
        ),
    ],
)
async def test_direct_clip_resolves_to_coherent_video_canonical(
    raw: str,
    hostname: str,
    path: str,
    canonical: str,
) -> None:
    cfg = _settings()
    providers = ProviderRegistry.from_settings(cfg)
    dns_calls: list[str] = []

    class TrackingDns(FakeDnsResolver):
        async def resolve(self, hostname: str):  # type: ignore[override]
            dns_calls.append(hostname)
            return await super().resolve(hostname)

    dns = TrackingDns(default_addresses=("8.8.8.8",))
    validator = URLValidator(cfg, registry=providers, resolver=dns)
    http = MagicMock()
    http.aclose = AsyncMock()
    http.fetch_document = AsyncMock(
        side_effect=AssertionError("direct provider must not HTTP fetch")
    )
    wrappers = build_wrapper_registry(cfg, providers)
    service = ResolutionService(
        cfg,
        provider_registry=providers,
        wrapper_registry=wrappers,
        validator=validator,
        http_client=http,
        dns_resolver=dns,
    )
    validated = await validator.validate(raw)
    dns_after_validate = list(dns_calls)
    rewritten = service._with_stable_provider_identity(validated)
    assert dns_calls == dns_after_validate
    assert rewritten.url.path == path
    assert rewritten.url.canonical == canonical
    assert rewritten.url.hostname == hostname
    assert "?" not in rewritten.url.canonical
    assert "#" not in rewritten.url.canonical

    result = await service.resolve(raw)
    assert result.provider_id == ProviderID.VK.value
    assert result.provenance == ResolutionProvenance.DIRECT_PROVIDER
    assert result.wrapper_type is None
    assert result.provider_url.hostname == hostname
    assert result.provider_url.path == path
    assert result.provider_url.canonical == canonical
    assert result.canonical_provider_url == canonical
    assert result.canonical_provider_url == result.validated.url.canonical
    assert result.validated.url.path == path
    assert "/clip" not in result.canonical_provider_url
    http.fetch_document.assert_not_called()


@pytest.mark.asyncio
async def test_direct_vk_ru_clip_is_provider_not_wrapper() -> None:
    cfg = _settings()
    providers = ProviderRegistry.from_settings(cfg)
    dns = FakeDnsResolver(default_addresses=("8.8.8.8",))
    validator = URLValidator(cfg, registry=providers, resolver=dns)
    client = SafeHTTPClient(cfg, validator=validator, resolver=dns)
    wrappers = build_wrapper_registry(cfg, providers)
    service = ResolutionService(
        cfg,
        provider_registry=providers,
        wrapper_registry=wrappers,
        validator=validator,
        http_client=client,
        dns_resolver=dns,
    )
    try:
        result = await service.resolve(
            "https://vk.ru/clip-235548483_456239236"
        )
    finally:
        await client.aclose()
    assert result.provider_id == ProviderID.VK.value
    assert result.provider_url.hostname == "vk.ru"
    assert result.provider_url.path == "/video-235548483_456239236"
    assert result.canonical_provider_url == (
        "https://vk.ru/video-235548483_456239236"
    )
    assert result.canonical_provider_url == result.validated.url.canonical
    assert result.provenance == ResolutionProvenance.DIRECT_PROVIDER
    assert result.wrapper_type is None


@pytest.mark.asyncio
async def test_wrapper_terminal_clip_normalizes_chain_and_result() -> None:
    from dataclasses import dataclass
    from typing import Any

    from fetchnow.resolution.models import WrapperResolveOutcome
    from fetchnow.resolution.protocols import WrapperValidatedURL
    from fetchnow.resolution.registry import WrapperResolverRegistry

    @dataclass(frozen=True)
    class ClipCandidateResolver:
        resolver_id: str = "clip_wrap"
        wrapper_type: str = "clip_wrap"
        hosts: frozenset[str] = frozenset({"wrapper.test"})

        @property
        def exact_hostnames(self) -> frozenset[str]:
            return self.hosts

        def matches(self, validated: WrapperValidatedURL) -> bool:
            return validated.hostname in self.hosts

        async def resolve(
            self,
            validated: WrapperValidatedURL,
            *,
            documents: Any,
        ) -> WrapperResolveOutcome:
            del validated, documents
            return WrapperResolveOutcome(
                candidate_url="https://vk.ru/clip-235548483_456239236",
                strategy="static_map",
                metadata={"hop": 1},
            )

    cfg = _settings()
    providers = ProviderRegistry.from_settings(cfg)
    dns = FakeDnsResolver(
        records={
            "wrapper.test": ["1.1.1.1"],
            "vk.ru": ["8.8.8.8"],
        },
        default_addresses=("8.8.8.8",),
    )
    wrappers = WrapperResolverRegistry.from_resolvers([ClipCandidateResolver()])
    validator = URLValidator(cfg, registry=providers, resolver=dns)
    http = MagicMock()
    http.aclose = AsyncMock()
    http.fetch_document = AsyncMock(
        side_effect=AssertionError("static map must not fetch")
    )
    service = ResolutionService(
        cfg,
        provider_registry=providers,
        wrapper_registry=wrappers,
        validator=validator,
        http_client=http,
        dns_resolver=dns,
    )
    result = await service.resolve("https://wrapper.test/item")
    assert result.provenance == ResolutionProvenance.WRAPPER_RESOLVED
    assert result.provider_url.path == "/video-235548483_456239236"
    assert result.canonical_provider_url == (
        "https://vk.ru/video-235548483_456239236"
    )
    assert result.canonical_provider_url == result.validated.url.canonical
    assert len(result.resolution_chain) == 1
    assert result.resolution_chain[0].target_canonical == (
        result.canonical_provider_url
    )
    assert "/clip" not in result.resolution_chain[0].target_canonical


@pytest.mark.asyncio
async def test_wrapper_mismatched_clip_identity_rejected() -> None:
    from dataclasses import dataclass
    from typing import Any

    from fetchnow.resolution.errors import ResolutionError, ResolutionErrorKind
    from fetchnow.resolution.models import WrapperResolveOutcome
    from fetchnow.resolution.protocols import WrapperValidatedURL
    from fetchnow.resolution.registry import WrapperResolverRegistry

    @dataclass(frozen=True)
    class BadHostResolver:
        resolver_id: str = "bad_wrap"
        wrapper_type: str = "bad_wrap"
        hosts: frozenset[str] = frozenset({"wrapper.test"})

        @property
        def exact_hostnames(self) -> frozenset[str]:
            return self.hosts

        def matches(self, validated: WrapperValidatedURL) -> bool:
            return validated.hostname in self.hosts

        async def resolve(
            self,
            validated: WrapperValidatedURL,
            *,
            documents: Any,
        ) -> WrapperResolveOutcome:
            del validated, documents
            return WrapperResolveOutcome(
                candidate_url="https://evil.example/clip-1_2",
                strategy="static_map",
            )

    cfg = _settings()
    providers = ProviderRegistry.from_settings(cfg)
    dns = FakeDnsResolver(
        records={"wrapper.test": ["1.1.1.1"], "evil.example": ["8.8.8.8"]},
        default_addresses=("8.8.8.8",),
    )
    wrappers = WrapperResolverRegistry.from_resolvers([BadHostResolver()])
    validator = URLValidator(cfg, registry=providers, resolver=dns)
    service = ResolutionService(
        cfg,
        provider_registry=providers,
        wrapper_registry=wrappers,
        validator=validator,
        http_client=MagicMock(aclose=AsyncMock()),
        dns_resolver=dns,
    )
    with pytest.raises(ResolutionError) as exc:
        await service.resolve("https://wrapper.test/item")
    assert exc.value.kind is ResolutionErrorKind.RESOLVED_PROVIDER_UNSUPPORTED


@pytest.mark.asyncio
async def test_m_vk_ru_clip_after_same_provider_redirect() -> None:
    """Validate accepts m.vk.ru/clip as same-provider exact host."""
    cfg = _settings()
    providers = ProviderRegistry.from_settings(cfg)
    dns = FakeDnsResolver(default_addresses=("8.8.8.8",))
    validator = URLValidator(cfg, registry=providers, resolver=dns)
    result = await validator.validate(
        "https://m.vk.ru/clip-235548483_456239236"
    )
    assert result.provider_id == ProviderID.VK
    assert result.url.hostname == "m.vk.ru"


def test_inspection_accepts_clip_webpage_url_for_video_target() -> None:
    payload = copy.deepcopy(VK_FIXTURE)
    payload["id"] = _CLIP_MEDIA_ID
    payload["webpage_url"] = (
        f"https://vk.ru/clip{_CLIP_OWNER}_{_CLIP_VIDEO}"
    )
    payload["original_url"] = (
        f"https://vk.ru/clip{_CLIP_OWNER}_{_CLIP_VIDEO}"
    )
    draft = parse_ytdlp_json(
        dumps_fixture(payload),
        expected_provider_id="vk",
        expected_canonical_url=(
            f"https://vk.ru/video{_CLIP_OWNER}_{_CLIP_VIDEO}"
        ),
        expected_media_id=_CLIP_MEDIA_ID,
        allowed_extractor_keys=frozenset({"vk"}),
        allowed_hostnames=_VK_HOSTS,
        max_height=2160,
        max_width=3840,
        max_bytes=10**9,
        max_duration=3600,
        tool_version="2026.7.4",
    )
    assert draft.media_id == _CLIP_MEDIA_ID
    assert draft.canonical_provider_url == (
        f"https://vk.ru/video{_CLIP_OWNER}_{_CLIP_VIDEO}"
    )
    assert draft.extractor_key == "vk"


@pytest.mark.parametrize(
    ("webpage", "payload_id", "extractor_key"),
    [
        ("https://vk.ru/clip-999_888", "-999_888", "vk"),
        (
            "https://evil.example/clip-235548483_456239236",
            _CLIP_MEDIA_ID,
            "vk",
        ),
        (
            f"https://vk.ru/clip{_CLIP_OWNER}_{_CLIP_VIDEO}",
            _CLIP_MEDIA_ID,
            "generic",
        ),
        ("https://vk.ru/clip--1_2", "-1_2", "vk"),
        (
            "https://rutube.ru/video/abc123def456/",
            _CLIP_MEDIA_ID,
            "vk",
        ),
        (
            "https://cdn.example.invalid/direct.mp4",
            _CLIP_MEDIA_ID,
            "vk",
        ),
    ],
)
def test_inspection_rejects_clip_identity_failures(
    webpage: str,
    payload_id: str,
    extractor_key: str,
) -> None:
    payload = copy.deepcopy(VK_FIXTURE)
    payload["id"] = payload_id
    payload["webpage_url"] = webpage
    payload["original_url"] = webpage
    payload["extractor_key"] = extractor_key
    payload["extractor"] = extractor_key
    with pytest.raises(InspectionError) as exc:
        parse_ytdlp_json(
            dumps_fixture(payload),
            expected_provider_id="vk",
            expected_canonical_url=(
                f"https://vk.ru/video{_CLIP_OWNER}_{_CLIP_VIDEO}"
            ),
            expected_media_id=_CLIP_MEDIA_ID,
            allowed_extractor_keys=frozenset({"vk"}),
            allowed_hostnames=_VK_HOSTS,
            max_height=2160,
            max_width=3840,
            max_bytes=10**9,
            max_duration=3600,
            tool_version="2026.7.4",
        )
    assert exc.value.kind in {
        InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH,
        InspectionErrorKind.INSPECTION_POLICY_REJECTED,
        InspectionErrorKind.INSPECTION_INVALID_OUTPUT,
    }


def test_conflicting_clip_and_video_identity_fields_rejected() -> None:
    payload = copy.deepcopy(VK_FIXTURE)
    payload["id"] = _CLIP_MEDIA_ID
    payload["webpage_url"] = (
        f"https://vk.ru/clip{_CLIP_OWNER}_{_CLIP_VIDEO}"
    )
    payload["original_url"] = "https://vk.ru/video-999_888"
    with pytest.raises(InspectionError) as exc:
        parse_ytdlp_json(
            dumps_fixture(payload),
            expected_provider_id="vk",
            expected_canonical_url=(
                f"https://vk.ru/video{_CLIP_OWNER}_{_CLIP_VIDEO}"
            ),
            expected_media_id=_CLIP_MEDIA_ID,
            allowed_extractor_keys=frozenset({"vk"}),
            allowed_hostnames=_VK_HOSTS,
            max_height=2160,
            max_width=3840,
            max_bytes=10**9,
            max_duration=3600,
            tool_version="2026.7.4",
        )
    assert exc.value.kind is InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH


def test_parse_identity_from_url_cross_alias_clip() -> None:
    identity = parse_identity_from_url(
        f"https://m.vk.ru/clip{_CLIP_OWNER}_{_CLIP_VIDEO}",
        provider_id="vk",
        allowed_hostnames=_VK_HOSTS,
    )
    assert identity.media_id == _CLIP_MEDIA_ID
    assert identity.canonical_path == f"/video{_CLIP_OWNER}_{_CLIP_VIDEO}"
    assert identity.hostname == "m.vk.ru"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "path", "canonical"),
    [
        (
            "https://rutube.ru/video/abc123",
            "/video/abc123",
            "https://rutube.ru/video/abc123",
        ),
        (
            "https://rutube.ru/video/abc123/",
            "/video/abc123/",
            "https://rutube.ru/video/abc123/",
        ),
        (
            "https://rutube.ru/video/abc123?list=x",
            "/video/abc123",
            "https://rutube.ru/video/abc123",
        ),
    ],
)
async def test_rutube_resolution_preserves_path_form(
    raw: str,
    path: str,
    canonical: str,
) -> None:
    cfg = _settings()
    providers = ProviderRegistry.from_settings(cfg)
    dns_calls: list[str] = []

    class TrackingDns(FakeDnsResolver):
        async def resolve(self, hostname: str):  # type: ignore[override]
            dns_calls.append(hostname)
            return await super().resolve(hostname)

    dns = TrackingDns(default_addresses=("8.8.8.8",))
    validator = URLValidator(cfg, registry=providers, resolver=dns)
    http = MagicMock()
    http.aclose = AsyncMock()
    http.fetch_document = AsyncMock(
        side_effect=AssertionError("direct rutube must not HTTP fetch")
    )
    wrappers = build_wrapper_registry(cfg, providers)
    service = ResolutionService(
        cfg,
        provider_registry=providers,
        wrapper_registry=wrappers,
        validator=validator,
        http_client=http,
        dns_resolver=dns,
    )
    validated = await validator.validate(raw)
    after_validate = list(dns_calls)
    rewritten = service._with_stable_provider_identity(validated)
    assert dns_calls == after_validate
    assert rewritten.url.path == path
    assert rewritten.url.canonical == canonical
    assert rewritten is validated or (
        rewritten.url.path == validated.url.path
        and rewritten.url.canonical == validated.url.canonical
    )

    result = await service.resolve(raw)
    assert result.provider_id == ProviderID.RUTUBE.value
    assert result.provider_url.path == path
    assert result.canonical_provider_url == canonical
    assert result.canonical_provider_url == result.validated.url.canonical
    assert "?" not in result.canonical_provider_url
    http.fetch_document.assert_not_called()


@pytest.mark.parametrize(
    ("url", "kind", "reason"),
    [
        (
            "https://vk.com:not-a-port/video-1_2",
            InspectionErrorKind.INSPECTION_INVALID_OUTPUT,
            "IDENTITY_URL_PORT_INVALID",
        ),
        (
            "https://vk.com:70000/video-1_2",
            InspectionErrorKind.INSPECTION_INVALID_OUTPUT,
            "IDENTITY_URL_PORT_INVALID",
        ),
        (
            "https://vk.com:8443/video-1_2",
            InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH,
            "IDENTITY_URL_PORT_UNTRUSTED",
        ),
    ],
)
def test_identity_from_url_port_classification(
    url: str,
    kind: InspectionErrorKind,
    reason: str,
) -> None:
    with pytest.raises(InspectionError) as exc:
        parse_identity_from_url(
            url,
            provider_id="vk",
            allowed_hostnames=_VK_HOSTS,
        )
    assert exc.value.kind is kind
    assert exc.value.internal_reason == reason
    text = f"{exc.value!s}{exc.value!r}"
    assert url not in text
    assert "not-a-port" not in text
    assert "70000" not in text
    assert "8443" not in text
    assert "vk.com" not in text
    assert "https://" not in text
    assert "user:" not in text
    assert "pass@" not in text


def test_identity_from_url_credentials_mismatch_no_leak() -> None:
    url = "https://user:pass@vk.com/video-1_2"
    with pytest.raises(InspectionError) as exc:
        parse_identity_from_url(
            url,
            provider_id="vk",
            allowed_hostnames=_VK_HOSTS,
        )
    assert exc.value.kind is InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH
    assert exc.value.internal_reason == "IDENTITY_URL_CREDENTIALS"
    text = f"{exc.value!s}{exc.value!r}"
    assert url not in text
    assert "user" not in text
    assert "pass" not in text
    assert "vk.com" not in text


def test_identity_from_url_default_https_port_accepted() -> None:
    identity = parse_identity_from_url(
        "https://vk.com/video-1_2",
        provider_id="vk",
        allowed_hostnames=_VK_HOSTS,
    )
    assert identity.media_id == "-1_2"
    assert identity.canonical_path == "/video-1_2"


@pytest.mark.parametrize("url", ["https://vk.com:443/video-1_2", "http://vk.com:80/video-1_2"])
def test_identity_from_url_explicit_allowed_ports(url: str) -> None:
    identity = parse_identity_from_url(
        url,
        provider_id="vk",
        allowed_hostnames=_VK_HOSTS,
    )
    assert identity.media_id == "-1_2"
    assert identity.canonical_path == "/video-1_2"


def test_download_reinspect_clip_input_selects_exact_format() -> None:
    """Offline clip → canonical video identity → exact format token."""
    from fetchnow.downloads.selection import resolve_selection_from_draft
    from fetchnow.media_inspection.models import (
        CodecFamily,
        ExtractedMediaDraft,
        FormatCategory,
        InternalFormatCandidate,
        MediaFormat,
    )
    from fetchnow.media_inspection.normalize import (
        format_option_id_for,
        quality_label_for,
    )

    identity = parse_provider_media_identity(
        provider_id="vk",
        hostname="vk.ru",
        path=f"/clip{_CLIP_OWNER}_{_CLIP_VIDEO}",
        allowed_hostnames=_VK_HOSTS,
    )
    canonical = build_canonical_provider_url(
        hostname=identity.hostname,
        canonical_path=identity.canonical_path,
    )
    assert canonical.endswith(f"/video{_CLIP_OWNER}_{_CLIP_VIDEO}")
    candidate = InternalFormatCandidate(
        container="mp4",
        width=1280,
        height=720,
        fps=30.0,
        has_video=True,
        has_audio=True,
        video_codec=CodecFamily.AVC,
        audio_codec=CodecFamily.AAC,
        approx_bytes=5_000_000,
        provider_format_token="url720",
    )
    label = quality_label_for(candidate.height, has_video=True)
    option_id = format_option_id_for(candidate, label)
    persisted = MediaFormat(
        format_option_id=option_id,
        container="mp4",
        width=1280,
        height=720,
        fps=30.0,
        has_video=True,
        has_audio=True,
        category=FormatCategory.PROGRESSIVE,
        video_codec=CodecFamily.AVC,
        audio_codec=CodecFamily.AAC,
        approx_bytes=5_000_000,
        quality_label=label,
        free_tier_eligible=True,
    )
    draft = ExtractedMediaDraft(
        provider_id="vk",
        media_id=identity.media_id,
        canonical_provider_url=canonical,
        title=None,
        duration_seconds=10,
        candidates=(candidate,),
        extractor_key="vk",
        tool_version="2026.7.4",
    )
    selected = resolve_selection_from_draft(
        draft,
        _settings(
            DATABASE_URL=(
                "postgresql+asyncpg://fetchnow:fetchnow@localhost:5432/fetchnow"
            ),
            MAX_SOURCE_FILE_BYTES=10**9,
            MEDIA_DOWNLOAD_MAX_BYTES=10**9,
        ),
        option_id,
        persisted,
    )
    assert selected.provider_format_token == "url720"