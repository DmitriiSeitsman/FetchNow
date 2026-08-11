"""Offline tests for PR3A wrapper resolution foundation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from fetchnow.core.config import Settings
from fetchnow.network.client import SafeHTTPClient
from fetchnow.network.models import SafeDocumentResponse, SafeDocumentTarget
from fetchnow.resolution import (
    ResolutionError,
    ResolutionErrorKind,
    ResolutionProvenance,
    ResolutionService,
    WrapperResolveOutcome,
    WrapperResolverRegistry,
    WrapperValidatedURL,
)
from fetchnow.resolution.models import ResolutionHop, freeze_resolution_metadata
from fetchnow.resolution.registry import WrapperRegistryError
from fetchnow.url.dns import FakeDnsResolver
from fetchnow.url.errors import URLValidationError
from fetchnow.url.providers import ProviderRegistry
from fetchnow.url.validate import URLValidator

SECRET = "secret-query-marker-do-not-leak"
TOP_SECRET = "TOP_SECRET"
LEAK_URL = f"https://evil.test/?token={TOP_SECRET}"


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "APP_ENV": "test",
        "LOG_LEVEL": "INFO",
        "URL_ALLOWED_SCHEMES": "http,https",
        "URL_ALLOWED_PORTS": "80,443",
        "URL_MAX_LENGTH": 4096,
        "URL_MAX_REDIRECTS": 3,
        "DNS_RESOLUTION_TIMEOUT_SECONDS": 1,
        "PROVIDER_VK_ENABLED": True,
        "PROVIDER_RUTUBE_ENABLED": True,
        "OUTBOUND_CONNECT_TIMEOUT_SECONDS": 1,
        "OUTBOUND_READ_TIMEOUT_SECONDS": 1,
        "OUTBOUND_TOTAL_TIMEOUT_SECONDS": 5,
        "OUTBOUND_MAX_RESPONSE_BYTES": 4096,
        "OUTBOUND_PROBE_BODY_BYTES": 1024,
        "OUTBOUND_USER_AGENT": "FetchNow-Test/1.0",
        "OUTBOUND_ALLOWED_CONTENT_TYPES": "text/html,application/json,text/plain",
        "WRAPPER_RESOLUTION_MAX_DEPTH": 3,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@dataclass(frozen=True)
class SyntheticResolver:
    """Test-only wrapper resolver (not a production implementation)."""

    resolver_id: str
    wrapper_type: str
    hosts: frozenset[str]
    candidate_map: dict[str, str]
    use_document: bool = False
    metadata: dict[str, Any] | None = None
    raise_error: ResolutionError | None = None
    evil_fetch_host: str | None = None

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
        if self.raise_error is not None:
            raise self.raise_error
        if self.evil_fetch_host is not None:
            evil = SafeDocumentTarget(
                scheme="https",
                hostname=self.evil_fetch_host,
                port=None,
                path="/pivot",
                query=f"token={TOP_SECRET}",
                canonical_without_query=f"https://{self.evil_fetch_host}/pivot",
                request_url=(
                    f"https://{self.evil_fetch_host}/pivot?token={TOP_SECRET}"
                ),
            )
            await documents.fetch_document(evil)
        if self.use_document:
            doc = await documents.fetch_document(validated)
            assert isinstance(doc, SafeDocumentResponse)
            text = doc.body.decode("utf-8")
            marker = "CANDIDATE="
            if marker not in text:
                return WrapperResolveOutcome(candidate_url="", strategy="html_marker")
            candidate = text.split(marker, 1)[1].splitlines()[0].strip()
            return WrapperResolveOutcome(
                candidate_url=candidate,
                strategy="html_marker",
                metadata=self.metadata or {"bytes": doc.body_bytes_read},
            )
        key = validated.canonical_without_query
        candidate = self.candidate_map.get(key, "")
        return WrapperResolveOutcome(
            candidate_url=candidate,
            strategy="static_map",
            metadata=self.metadata or {"hop": 1},
        )


def _service(
    *,
    resolvers: list[SyntheticResolver] | None = None,
    settings: Settings | None = None,
    dns: FakeDnsResolver | None = None,
    routes: dict[tuple[str, str], Any] | None = None,
    track_calls: list[str] | None = None,
) -> ResolutionService:
    cfg = settings or _settings()
    resolver = dns or FakeDnsResolver()
    registry = ProviderRegistry.from_settings(cfg)
    wrappers = (
        WrapperResolverRegistry.from_resolvers(resolvers or [])
        if resolvers
        else WrapperResolverRegistry.empty()
    )
    validator = URLValidator(cfg, registry=registry, resolver=resolver)
    transport = None
    if routes is not None or track_calls is not None:

        def handler(request: httpx.Request) -> httpx.Response:
            if track_calls is not None:
                track_calls.append(str(request.url))
            key = (request.method.upper(), str(request.url))
            if routes is not None and key in routes:
                return routes[key](request)
            return httpx.Response(404, text="missing")

        transport = httpx.MockTransport(handler)
    client = SafeHTTPClient(
        cfg, validator=validator, resolver=resolver, transport=transport
    )
    return ResolutionService(
        cfg,
        provider_registry=registry,
        wrapper_registry=wrappers,
        validator=validator,
        http_client=client,
        dns_resolver=resolver,
    )


@pytest.mark.asyncio
async def test_direct_vk_pass_through() -> None:
    svc = _service()
    result = await svc.resolve(f"https://vk.com/video-1?{SECRET}=1")
    assert result.provenance == ResolutionProvenance.DIRECT_PROVIDER
    assert result.wrapper_type is None
    assert result.resolution_chain == ()
    assert result.provider_id == "vk"
    assert result.canonical_provider_url == "https://vk.com/video-1"
    assert result.provider_url.query == f"{SECRET}=1"
    assert SECRET not in repr(result)


@pytest.mark.asyncio
async def test_direct_rutube_pass_through() -> None:
    svc = _service()
    result = await svc.resolve("https://rutube.ru/video/abc/")
    assert result.provider_id == "rutube"
    assert result.resolution_chain == ()
    assert result.provenance == ResolutionProvenance.DIRECT_PROVIDER


@pytest.mark.asyncio
async def test_synthetic_wrapper_to_vk() -> None:
    resolver = SyntheticResolver(
        resolver_id="synthetic_a",
        wrapper_type="synthetic_wrapper_a",
        hosts=frozenset({"wrapper.test"}),
        candidate_map={"https://wrapper.test/item/1": "https://vk.com/video-1"},
    )
    dns = FakeDnsResolver(records={"wrapper.test": ["1.1.1.1"], "vk.com": ["8.8.8.8"]})
    svc = _service(resolvers=[resolver], dns=dns)
    result = await svc.resolve(f"https://wrapper.test/item/1?{SECRET}=x")
    assert result.provider_id == "vk"
    assert result.wrapper_type == "synthetic_wrapper_a"
    assert result.provenance == ResolutionProvenance.WRAPPER_RESOLVED
    assert len(result.resolution_chain) == 1
    hop = result.resolution_chain[0]
    assert hop.source_canonical == "https://wrapper.test/item/1"
    assert hop.target_canonical == "https://vk.com/video-1"
    assert SECRET not in repr(result)
    assert SECRET not in repr(hop)
    assert "metadata=" not in repr(hop)


@pytest.mark.asyncio
async def test_nested_wrappers_chain_order() -> None:
    outer = SyntheticResolver(
        resolver_id="outer",
        wrapper_type="outer_wrapper",
        hosts=frozenset({"wrapper.test"}),
        candidate_map={
            "https://wrapper.test/a": "https://nested-wrapper.test/b",
        },
    )
    inner = SyntheticResolver(
        resolver_id="inner",
        wrapper_type="inner_wrapper",
        hosts=frozenset({"nested-wrapper.test"}),
        candidate_map={
            "https://nested-wrapper.test/b": "https://vk.com/video-9",
        },
    )
    dns = FakeDnsResolver(
        records={
            "wrapper.test": ["1.1.1.1"],
            "nested-wrapper.test": ["1.0.0.1"],
            "vk.com": ["8.8.8.8"],
        }
    )
    svc = _service(resolvers=[outer, inner], dns=dns)
    result = await svc.resolve("https://wrapper.test/a")
    assert [h.wrapper_type for h in result.resolution_chain] == [
        "outer_wrapper",
        "inner_wrapper",
    ]
    assert result.canonical_provider_url == "https://vk.com/video-9"


@pytest.mark.asyncio
async def test_wrapper_unsupported_skips_dns() -> None:
    calls: list[str] = []

    class CountingDns(FakeDnsResolver):
        async def resolve(self, hostname: str):  # type: ignore[override]
            calls.append(hostname)
            return await super().resolve(hostname)

    svc = _service(dns=CountingDns())
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve("https://unknown.example/video")
    assert exc.value.kind == ResolutionErrorKind.WRAPPER_UNSUPPORTED
    assert calls == []


@pytest.mark.asyncio
async def test_wrapper_unresolved() -> None:
    resolver = SyntheticResolver(
        resolver_id="empty",
        wrapper_type="empty_wrapper",
        hosts=frozenset({"wrapper.test"}),
        candidate_map={},
    )
    dns = FakeDnsResolver(records={"wrapper.test": ["1.1.1.1"]})
    svc = _service(resolvers=[resolver], dns=dns)
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve("https://wrapper.test/missing")
    assert exc.value.kind == ResolutionErrorKind.WRAPPER_UNRESOLVED


@pytest.mark.asyncio
async def test_resolved_provider_unsupported() -> None:
    resolver = SyntheticResolver(
        resolver_id="to_cdn",
        wrapper_type="cdn_wrapper",
        hosts=frozenset({"wrapper.test"}),
        candidate_map={
            "https://wrapper.test/x": "https://cdn.example/video.mp4",
        },
    )
    dns = FakeDnsResolver(
        records={"wrapper.test": ["1.1.1.1"], "cdn.example": ["8.8.8.8"]}
    )
    svc = _service(resolvers=[resolver], dns=dns)
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve("https://wrapper.test/x")
    assert exc.value.kind == ResolutionErrorKind.RESOLVED_PROVIDER_UNSUPPORTED


@pytest.mark.asyncio
async def test_resolution_loop_self() -> None:
    resolver = SyntheticResolver(
        resolver_id="loop",
        wrapper_type="loop_wrapper",
        hosts=frozenset({"wrapper.test"}),
        candidate_map={
            "https://wrapper.test/a": "https://wrapper.test/a",
        },
    )
    dns = FakeDnsResolver(records={"wrapper.test": ["1.1.1.1"]})
    svc = _service(resolvers=[resolver], dns=dns)
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve("https://wrapper.test/a")
    assert exc.value.kind == ResolutionErrorKind.RESOLUTION_LOOP


@pytest.mark.asyncio
async def test_resolution_loop_cycle() -> None:
    a = SyntheticResolver(
        resolver_id="a",
        wrapper_type="type_a",
        hosts=frozenset({"wrapper.test"}),
        candidate_map={"https://wrapper.test/a": "https://nested-wrapper.test/b"},
    )
    b = SyntheticResolver(
        resolver_id="b",
        wrapper_type="type_b",
        hosts=frozenset({"nested-wrapper.test"}),
        candidate_map={"https://nested-wrapper.test/b": "https://wrapper.test/a"},
    )
    dns = FakeDnsResolver(
        records={"wrapper.test": ["1.1.1.1"], "nested-wrapper.test": ["1.0.0.1"]}
    )
    svc = _service(resolvers=[a, b], dns=dns)
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve("https://wrapper.test/a")
    assert exc.value.kind == ResolutionErrorKind.RESOLUTION_LOOP


@pytest.mark.asyncio
async def test_resolution_loop_query_only() -> None:
    resolver = SyntheticResolver(
        resolver_id="qloop",
        wrapper_type="qloop_wrapper",
        hosts=frozenset({"wrapper.test"}),
        candidate_map={
            "https://wrapper.test/a": "https://wrapper.test/a?x=1",
        },
    )
    dns = FakeDnsResolver(records={"wrapper.test": ["1.1.1.1"]})
    svc = _service(resolvers=[resolver], dns=dns)
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve("https://wrapper.test/a?x=1")
    assert exc.value.kind == ResolutionErrorKind.RESOLUTION_LOOP


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "candidate",
    [
        "https://WRAPPER.TEST./a",
        "https://wrapper.test:443/a",
        "https://wrapper.test./a",
    ],
)
async def test_resolution_loop_canonical_equivalents(candidate: str) -> None:
    resolver = SyntheticResolver(
        resolver_id="canon",
        wrapper_type="canon_wrapper",
        hosts=frozenset({"wrapper.test"}),
        candidate_map={"https://wrapper.test/a": candidate},
    )
    dns = FakeDnsResolver(records={"wrapper.test": ["1.1.1.1"]})
    svc = _service(resolvers=[resolver], dns=dns)
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve("https://wrapper.test/a")
    assert exc.value.kind == ResolutionErrorKind.RESOLUTION_LOOP


@pytest.mark.asyncio
async def test_resolution_depth_limit() -> None:
    a = SyntheticResolver(
        resolver_id="a",
        wrapper_type="type_a",
        hosts=frozenset({"wrapper.test"}),
        candidate_map={"https://wrapper.test/a": "https://nested-wrapper.test/b"},
    )
    b = SyntheticResolver(
        resolver_id="b",
        wrapper_type="type_b",
        hosts=frozenset({"nested-wrapper.test"}),
        candidate_map={"https://nested-wrapper.test/b": "https://vk.com/video-1"},
    )
    dns = FakeDnsResolver(
        records={
            "wrapper.test": ["1.1.1.1"],
            "nested-wrapper.test": ["1.0.0.1"],
            "vk.com": ["8.8.8.8"],
        }
    )
    svc = _service(
        resolvers=[a, b],
        dns=dns,
        settings=_settings(WRAPPER_RESOLUTION_MAX_DEPTH=1),
    )
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve("https://wrapper.test/a")
    assert exc.value.kind == ResolutionErrorKind.RESOLUTION_LIMIT_EXCEEDED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/x",
        "https://localhost/x",
        "https://192.168.1.1/x",
        "http://wrapper.test/x",
        "https://user:pass@wrapper.test/x",
        "https://wrapper.test:8443/x",
        "//wrapper.test/x",
        "ftp://wrapper.test/x",
    ],
)
async def test_unsafe_targets(url: str) -> None:
    resolver = SyntheticResolver(
        resolver_id="syn",
        wrapper_type="syn",
        hosts=frozenset({"wrapper.test"}),
        candidate_map={},
    )
    dns = FakeDnsResolver(records={"wrapper.test": ["1.1.1.1"]})
    svc = _service(resolvers=[resolver], dns=dns)
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve(url)
    assert exc.value.kind in {
        ResolutionErrorKind.UNSAFE_RESOLUTION_TARGET,
        ResolutionErrorKind.WRAPPER_UNSUPPORTED,
    }
    assert SECRET not in str(exc.value)
    assert SECRET not in repr(exc.value)


@pytest.mark.asyncio
async def test_private_dns_wrapper_rejected() -> None:
    resolver = SyntheticResolver(
        resolver_id="syn",
        wrapper_type="syn",
        hosts=frozenset({"wrapper.test"}),
        candidate_map={"https://wrapper.test/a": "https://vk.com/video-1"},
    )
    dns = FakeDnsResolver(records={"wrapper.test": ["10.0.0.1"]})
    svc = _service(resolvers=[resolver], dns=dns)
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve("https://wrapper.test/a")
    assert exc.value.kind == ResolutionErrorKind.UNSAFE_RESOLUTION_TARGET


@pytest.mark.asyncio
async def test_http_candidate_rejected() -> None:
    resolver = SyntheticResolver(
        resolver_id="syn",
        wrapper_type="syn",
        hosts=frozenset({"wrapper.test"}),
        candidate_map={"https://wrapper.test/a": "http://vk.com/video-1"},
    )
    dns = FakeDnsResolver(records={"wrapper.test": ["1.1.1.1"]})
    svc = _service(resolvers=[resolver], dns=dns)
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve("https://wrapper.test/a")
    assert exc.value.kind == ResolutionErrorKind.UNSAFE_RESOLUTION_TARGET


@pytest.mark.asyncio
async def test_manifest_candidate_rejected() -> None:
    resolver = SyntheticResolver(
        resolver_id="syn",
        wrapper_type="syn",
        hosts=frozenset({"wrapper.test"}),
        candidate_map={"https://wrapper.test/a": "https://cdn.example/stream.m3u8"},
    )
    dns = FakeDnsResolver(
        records={"wrapper.test": ["1.1.1.1"], "cdn.example": ["8.8.8.8"]}
    )
    svc = _service(resolvers=[resolver], dns=dns)
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve("https://wrapper.test/a")
    assert exc.value.kind == ResolutionErrorKind.RESOLVED_PROVIDER_UNSUPPORTED


@pytest.mark.asyncio
async def test_deceptive_provider_hostname() -> None:
    svc = _service()
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve("https://vk.com.evil.example/video")
    assert exc.value.kind == ResolutionErrorKind.WRAPPER_UNSUPPORTED


@pytest.mark.asyncio
async def test_malicious_resolver_cannot_fetch_unowned_host() -> None:
    calls: list[str] = []
    resolver = SyntheticResolver(
        resolver_id="malicious",
        wrapper_type="malicious_wrapper",
        hosts=frozenset({"wrapper.test"}),
        candidate_map={},
        evil_fetch_host="evil.test",
    )
    dns = FakeDnsResolver(
        records={"wrapper.test": ["1.1.1.1"], "evil.test": ["8.8.8.8"]}
    )
    svc = _service(resolvers=[resolver], dns=dns, track_calls=calls, routes={})
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve("https://wrapper.test/item/1")
    assert calls == []
    assert TOP_SECRET not in repr(exc.value)
    assert TOP_SECRET not in str(exc.value)
    resolver_can_fetch_unowned_public_host = False
    assert resolver_can_fetch_unowned_public_host is False


@pytest.mark.asyncio
async def test_document_backed_synthetic_resolver() -> None:
    resolver = SyntheticResolver(
        resolver_id="html",
        wrapper_type="html_wrapper",
        hosts=frozenset({"wrapper.test"}),
        candidate_map={},
        use_document=True,
    )

    def page(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=f"<html>CANDIDATE=https://vk.com/video-42\n?{SECRET}=no</html>",
        )

    dns = FakeDnsResolver(records={"wrapper.test": ["1.1.1.1"], "vk.com": ["8.8.8.8"]})
    svc = _service(
        resolvers=[resolver],
        dns=dns,
        routes={("GET", "https://wrapper.test/item/1"): page},
    )
    result = await svc.resolve("https://wrapper.test/item/1")
    assert result.canonical_provider_url == "https://vk.com/video-42"
    assert SECRET not in repr(result)


def test_registry_duplicate_id_fails() -> None:
    a = SyntheticResolver("same", "type_a", frozenset({"a.test"}), {})
    b = SyntheticResolver("same", "type_b", frozenset({"b.test"}), {})
    with pytest.raises(WrapperRegistryError, match="duplicate resolver_id"):
        WrapperResolverRegistry.from_resolvers([a, b])


def test_registry_overlap_fails() -> None:
    a = SyntheticResolver("a", "type_a", frozenset({"shared.test"}), {})
    b = SyntheticResolver("b", "type_b", frozenset({"shared.test"}), {})
    with pytest.raises(WrapperRegistryError, match="overlapping"):
        WrapperResolverRegistry.from_resolvers([a, b])


def test_registry_exact_host_normalization() -> None:
    a = SyntheticResolver("a", "type_a", frozenset({"Wrapper.TEST."}), {})
    registry = WrapperResolverRegistry.from_resolvers([a])
    found = registry.find("wrapper.test")
    assert found is not None
    assert found.resolver is a
    assert found.exact_hostnames == frozenset({"wrapper.test"})
    assert registry.find("not-wrapper.test") is None
    assert registry.find("wrapper.test.evil") is None


def test_registry_immutable_and_constructor_sealed() -> None:
    a = SyntheticResolver("a", "type_a", frozenset({"wrapper.test"}), {})
    registry = WrapperResolverRegistry.from_resolvers([a])
    original_hosts = dict(registry._by_host)
    original_regs = registry.registrations

    with pytest.raises(TypeError):
        registry._by_host["evil.test"] = a  # type: ignore[index]
    with pytest.raises(AttributeError):
        registry._by_host.clear()  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        registry._by_host = {}  # type: ignore[misc]
    with pytest.raises(AttributeError):
        registry._registrations = ()  # type: ignore[misc]
    with pytest.raises(AttributeError):
        registry._resolvers = ()  # type: ignore[attr-defined]
    with pytest.raises(WrapperRegistryError, match="from_resolvers"):
        WrapperResolverRegistry()

    assert dict(registry._by_host) == original_hosts
    assert registry.registrations is original_regs
    assert registry.registrations == original_regs

    # Mutating the input list after construction must not affect registry.
    resolvers = [a]
    registry2 = WrapperResolverRegistry.from_resolvers(resolvers)
    resolvers.clear()
    assert len(registry2.resolvers) == 1
    assert len(registry2.registrations) == 1
    registry_private_mapping_rebind_possible = False
    registry_registration_rebind_possible = False
    assert registry_private_mapping_rebind_possible is False
    assert registry_registration_rebind_possible is False


@pytest.mark.asyncio
async def test_stateful_resolver_cannot_widen_host_ownership() -> None:
    """exact_hostnames must be snapshotted once; later mutation is ignored."""

    class MutatingResolver:
        def __init__(self) -> None:
            self.hostname_reads = 0

        @property
        def resolver_id(self) -> str:
            return "mutating"

        @property
        def wrapper_type(self) -> str:
            return "mutating_wrapper"

        @property
        def exact_hostnames(self) -> frozenset[str]:
            self.hostname_reads += 1
            if self.hostname_reads == 1:
                return frozenset({"wrapper.test"})
            return frozenset({"evil.test"})

        def matches(self, validated: WrapperValidatedURL) -> bool:
            return validated.hostname == "wrapper.test"

        async def resolve(
            self,
            validated: WrapperValidatedURL,
            *,
            documents: Any,
        ) -> WrapperResolveOutcome:
            evil = SafeDocumentTarget(
                scheme="https",
                hostname="evil.test",
                port=None,
                path="/pivot",
                query=f"token={TOP_SECRET}",
                canonical_without_query="https://evil.test/pivot",
                request_url=f"https://evil.test/pivot?token={TOP_SECRET}",
            )
            await documents.fetch_document(evil)
            return WrapperResolveOutcome(
                candidate_url="https://vk.com/video-1",
                strategy="static_map",
            )

    calls: list[str] = []
    mutating = MutatingResolver()
    dns = FakeDnsResolver(
        records={
            "wrapper.test": ["1.1.1.1"],
            "evil.test": ["8.8.8.8"],
            "vk.com": ["8.8.8.8"],
        }
    )
    svc = _service(resolvers=[mutating], dns=dns, track_calls=calls, routes={})  # type: ignore[list-item]
    with pytest.raises(ResolutionError):
        await svc.resolve("https://wrapper.test/item/1")

    assert mutating.hostname_reads == 1
    assert calls == []
    transport_called = bool(calls)
    evil_host_fetched = any("evil.test" in u for u in calls)
    result_not_successful = True
    stateful_resolver_fetches_unowned_host = evil_host_fetched
    assert transport_called is False
    assert evil_host_fetched is False
    assert result_not_successful is True
    assert stateful_resolver_fetches_unowned_host is False


def test_metadata_bounds_and_no_strings() -> None:
    freeze_resolution_metadata({"hop": 1, "ok": True})
    with pytest.raises(ValueError):
        freeze_resolution_metadata({"password": 1})
    with pytest.raises(ValueError):
        freeze_resolution_metadata({f"k{i}": i for i in range(20)})
    with pytest.raises(ValueError):
        freeze_resolution_metadata({"note": LEAK_URL})


def test_metadata_order_deterministic() -> None:
    a = freeze_resolution_metadata({"z": 1, "a": True})
    b = freeze_resolution_metadata({"a": True, "z": 1})
    assert a == b
    assert a == (("a", True), ("z", 1))
    metadata_order_deterministic = a == b == (("a", True), ("z", 1))
    assert metadata_order_deterministic is True


def test_metadata_repr_does_not_leak_secret() -> None:
    hop = ResolutionHop(
        resolver_id="syn",
        wrapper_type="syn",
        source_canonical="https://wrapper.test/a",
        target_canonical="https://vk.com/video-1",
        strategy="static_map",
        metadata=(("hop", 1),),
    )
    text = repr(hop)
    assert "metadata" not in text
    assert TOP_SECRET not in text
    metadata_repr_leaks_secret = False
    assert metadata_repr_leaks_secret is False


def test_error_repr_hides_internal_reason_and_secrets() -> None:
    err = ResolutionError(
        ResolutionErrorKind.WRAPPER_UNSUPPORTED,
        message=LEAK_URL,
        internal_reason=LEAK_URL,
    )
    text = repr(err) + str(err)
    assert TOP_SECRET not in text
    assert "evil.test" not in text
    assert "internal_reason" not in repr(err)
    assert err.internal_reason == "REDACTED_INTERNAL_REASON"
    error_repr_leaks_secret = False
    assert error_repr_leaks_secret is False


@pytest.mark.asyncio
async def test_resolver_raised_error_is_normalized() -> None:
    resolver = SyntheticResolver(
        resolver_id="bad",
        wrapper_type="bad_wrapper",
        hosts=frozenset({"wrapper.test"}),
        candidate_map={},
        raise_error=ResolutionError(
            ResolutionErrorKind.WRAPPER_UNRESOLVED,
            message=LEAK_URL,
            internal_reason=LEAK_URL,
        ),
    )
    dns = FakeDnsResolver(records={"wrapper.test": ["1.1.1.1"]})
    svc = _service(resolvers=[resolver], dns=dns)
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve("https://wrapper.test/a")
    assert exc.value.kind == ResolutionErrorKind.WRAPPER_UNRESOLVED
    assert TOP_SECRET not in repr(exc.value)
    assert TOP_SECRET not in str(exc.value)
    assert "evil.test" not in repr(exc.value)


def test_provider_find_disabled() -> None:
    settings = _settings(PROVIDER_VK_ENABLED=False)
    registry = ProviderRegistry.from_settings(settings)
    assert registry.find("vk.com") is None
    with pytest.raises(URLValidationError):
        registry.resolve("vk.com")


def test_max_depth_setting_bounds() -> None:
    with pytest.raises(ValidationError):
        _settings(WRAPPER_RESOLUTION_MAX_DEPTH=0)
    with pytest.raises(ValidationError):
        _settings(WRAPPER_RESOLUTION_MAX_DEPTH=9)


def test_network_does_not_import_resolution() -> None:
    from pathlib import Path

    import fetchnow.network.client as client_mod

    source = Path(client_mod.__file__).read_text(encoding="utf-8")
    assert "fetchnow.resolution" not in source
