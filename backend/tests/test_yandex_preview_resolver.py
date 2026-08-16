"""Offline unit tests for Yandex Video Preview resolver (PR3B correction)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from fetchnow.core.config import Settings
from fetchnow.network.client import SafeHTTPClient
from fetchnow.resolution import (
    ResolutionError,
    ResolutionErrorKind,
    ResolutionProvenance,
    ResolutionService,
    build_wrapper_registry,
)
from fetchnow.resolution.registry import WrapperResolverRegistry
from fetchnow.resolution.yandex_preview import (
    STRATEGY_TARGET_VIDEO_URL,
    STRATEGY_VIEWER_IFRAME_VIDEO_URL,
    _collect_target_records,
    _extract_target_bound,
    _is_stable_provider_path,
    _NodeBudget,
    _normalize_stable_provider_url,
)
from fetchnow.url.dns import FakeDnsResolver
from fetchnow.url.providers import ProviderRegistry
from fetchnow.url.validate import URLValidator
from yandex_preview_regression_data import (
    DUPS_COMPAT_HTML,
    EMBED_DECOY,
    EXPECTED_CANONICAL,
    EXPECTED_VK_IDENTITY_PATH,
    PREVIEW_ID,
    REGRESSION_HTML,
    RELATED_DECOY,
    SUBMITTED_URL,
    VOLATILE_MARKER,
)

SECRET = "yandex-secret-marker-do-not-leak"
HOSTS = frozenset(
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
        "rutube.ru",
        "www.rutube.ru",
    }
)


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
        "OUTBOUND_MAX_RESPONSE_BYTES": 65536,
        "OUTBOUND_PROBE_BODY_BYTES": 1024,
        "OUTBOUND_USER_AGENT": "FetchNow-Test/1.0",
        "OUTBOUND_ALLOWED_CONTENT_TYPES": "text/html,application/json,text/plain",
        "WRAPPER_RESOLUTION_MAX_DEPTH": 3,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _service(
    *,
    html_body: bytes,
    settings: Settings | None = None,
    dns: FakeDnsResolver | None = None,
    track_calls: list[str] | None = None,
    preview_path: str | None = None,
) -> ResolutionService:
    cfg = settings or _settings()
    resolver = dns or FakeDnsResolver(
        records={
            "yandex.ru": ["1.1.1.1"],
            "vkvideo.ru": ["8.8.8.8"],
            "vk.com": ["8.8.8.8"],
            "vk.ru": ["8.8.8.8"],
            "m.vk.ru": ["8.8.8.8"],
            "rutube.ru": ["8.8.4.4"],
            "youtube.com": ["8.8.8.8"],
            "www.youtube.com": ["8.8.8.8"],
            "ok.ru": ["8.8.8.8"],
        }
    )
    providers = ProviderRegistry.from_settings(cfg)
    wrappers = build_wrapper_registry(cfg, providers)
    validator = URLValidator(cfg, registry=providers, resolver=resolver)
    path = preview_path or f"/video/preview/{PREVIEW_ID}"

    def handler(request: httpx.Request) -> httpx.Response:
        if track_calls is not None:
            track_calls.append(str(request.url))
        assert "authorization" not in {k.lower() for k in request.headers}
        assert "cookie" not in {k.lower() for k in request.headers}
        if request.url.path == path:
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                content=html_body,
            )
        return httpx.Response(404, text="missing")

    client = SafeHTTPClient(
        cfg,
        validator=validator,
        resolver=resolver,
        transport=httpx.MockTransport(handler),
    )
    return ResolutionService(
        cfg,
        provider_registry=providers,
        wrapper_registry=wrappers,
        validator=validator,
        http_client=client,
        dns_resolver=resolver,
    )


def _target_html(
    preview_id: str,
    *,
    video_url: str | None = "https://vk.com/video-1_2",
    record_url: str | None = None,
    embed_url: str | None = None,
    related_url: str | None = None,
    extra_root: dict[str, Any] | None = None,
    nested_encoded: bool = False,
) -> bytes:
    record: dict[str, Any] = {"videoId": preview_id}
    if video_url is not None:
        record["player"] = {"videoUrl": video_url}
    if record_url is not None:
        record["url"] = record_url
    if embed_url is not None:
        record["embedUrl"] = embed_url
    if related_url is not None:
        record["related"] = [
            {
                "videoId": "00000000000000000000",
                "player": {"videoUrl": related_url},
            }
        ]
    state: dict[str, Any] = {"dups": {preview_id: record}}
    if extra_root:
        state.update(extra_root)
    if nested_encoded:
        payload = json.dumps({"dups": {preview_id: record}})
        state = {"wrapper": payload}
    body = (
        "<!DOCTYPE html><html><body>"
        f'<script type="application/json">{json.dumps(state)}</script>'
        "</body></html>"
    )
    return body.encode()


# --- VK stable path identity ---


@pytest.mark.parametrize(
    ("path", "accepted"),
    [
        ("/video123_456", True),
        ("/video-123_456", True),
        ("/video123_456/", True),
        ("/video-123_456/", True),
        ("/clip123_456", True),
        ("/clip-123_456", True),
        ("/clip123_456/", True),
        ("/clip-123_456/", True),
        ("/video--123_456", False),
        ("/clip--123_456", False),
        ("/clips-123_456", False),
        ("/clip123_-456", False),
        ("/video_456", False),
        ("/video123_", False),
        ("/videoabc_456", False),
        ("/video123_abc", False),
        ("/video123_456/extra", False),
        ("/clip-123_456/extra", False),
        ("/video_ext.php", False),
    ],
)
def test_vk_stable_path_shapes(path: str, accepted: bool) -> None:
    assert _is_stable_provider_path("vk.com", path) is accepted
    assert _is_stable_provider_path("vk.ru", path) is accepted
    assert _is_stable_provider_path("vkvideo.ru", path) is accepted


def test_yandex_authoritative_vk_ru_clip_rewrites_to_video() -> None:
    url, upgraded = _normalize_stable_provider_url(
        "https://vk.ru/clip-235548483_456239236",
        supported_hosts=HOSTS,
        allow_http_upgrade=True,
    )
    assert upgraded is False
    assert url == "https://vk.ru/video-235548483_456239236"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "https://rutube.ru/video/abcdef0123456789",
            "https://rutube.ru/video/abcdef0123456789/",
        ),
        (
            "https://rutube.ru/video/abcdef0123456789/",
            "https://rutube.ru/video/abcdef0123456789/",
        ),
        (
            "http://rutube.ru/video/abcdef0123456789",
            "https://rutube.ru/video/abcdef0123456789/",
        ),
        (
            "https://rutube.ru/shorts/3eac3b4561676c17df9132a9a1e62e3e",
            "https://rutube.ru/video/3eac3b4561676c17df9132a9a1e62e3e/",
        ),
        (
            "https://rutube.ru/shorts/3eac3b4561676c17df9132a9a1e62e3e/",
            "https://rutube.ru/video/3eac3b4561676c17df9132a9a1e62e3e/",
        ),
    ],
)
def test_yandex_rutube_canonicalizes_video_and_shorts(raw: str, expected: str) -> None:
    url, _upgraded = _normalize_stable_provider_url(
        raw,
        supported_hosts=HOSTS,
        allow_http_upgrade=True,
    )
    assert url == expected


@pytest.mark.parametrize(
    "raw",
    [
        "https://rutube.ru/play/embed/abcdef0123456789/",
        "https://rutube.ru/video/abcdef0123456789.m3u8",
        "https://rutube.ru/video/",
        "https://rutube.ru/videos/abcdef0123456789/",
        "https://rutube.ru/yappy/abcdef0123456789/",
        "https://rutube.ru/shorts/",
    ],
)
def test_yandex_rutube_malformed_and_embed_rejected(raw: str) -> None:
    url, upgraded = _normalize_stable_provider_url(
        raw,
        supported_hosts=HOSTS,
        allow_http_upgrade=True,
    )
    assert url is None
    assert upgraded is False


def test_http_positive_owner_upgraded() -> None:
    url, upgraded = _normalize_stable_provider_url(
        "http://vk.com/video123_456",
        supported_hosts=HOSTS,
        allow_http_upgrade=True,
    )
    assert url == "https://vk.com/video123_456"
    assert upgraded is True


def test_http_negative_owner_upgraded() -> None:
    url, upgraded = _normalize_stable_provider_url(
        "http://vk.com/video-123_456",
        supported_hosts=HOSTS,
        allow_http_upgrade=True,
    )
    assert url == "https://vk.com/video-123_456"
    assert upgraded is True


def test_http_double_minus_not_upgraded() -> None:
    url, upgraded = _normalize_stable_provider_url(
        "http://vk.com/video--123_456",
        supported_hosts=HOSTS,
        allow_http_upgrade=True,
    )
    assert url is None
    assert upgraded is False


# --- 1. Live-shaped fixture ---


@pytest.mark.asyncio
async def test_live_shaped_fixture_resolves_stable_vk() -> None:
    svc = _service(html_body=REGRESSION_HTML)
    result = await svc.resolve(SUBMITTED_URL)
    assert result.provider_id == "vk"
    assert result.canonical_provider_url == EXPECTED_CANONICAL
    assert EXPECTED_VK_IDENTITY_PATH in result.canonical_provider_url
    assert "/video_ext.php" not in result.canonical_provider_url
    assert result.provenance == ResolutionProvenance.WRAPPER_RESOLVED
    assert result.wrapper_type == "yandex_video_preview"
    hop = result.resolution_chain[0]
    assert hop.strategy == STRATEGY_VIEWER_IFRAME_VIDEO_URL
    meta = dict(hop.metadata)
    assert meta["target_bound"] is True
    assert meta["scheme_upgraded"] is True
    assert meta["iframe_payload"] is True
    live_shaped_fixture_returns_video_ext = (
        "/video_ext.php" in result.canonical_provider_url
    )
    assert live_shaped_fixture_returns_video_ext is False
    live_shaped_iframe_fixture_resolves_expected = (
        result.canonical_provider_url == EXPECTED_CANONICAL
        and hop.strategy == STRATEGY_VIEWER_IFRAME_VIDEO_URL
    )
    assert live_shaped_iframe_fixture_resolves_expected is True


@pytest.mark.asyncio
async def test_dups_compat_fixture_resolves_stable_vk() -> None:
    svc = _service(html_body=DUPS_COMPAT_HTML)
    result = await svc.resolve(SUBMITTED_URL)
    assert result.canonical_provider_url == EXPECTED_CANONICAL
    hop = result.resolution_chain[0]
    assert hop.strategy == STRATEGY_TARGET_VIDEO_URL
    meta = dict(hop.metadata)
    assert meta["target_bound"] is True
    assert meta["scheme_upgraded"] is True
    assert "iframe_payload" not in meta


@pytest.mark.asyncio
async def test_dups_preview_id_selected_among_records() -> None:
    other = "99999999999999999999"
    state = {
        "dups": {
            other: {
                "videoId": other,
                "player": {"videoUrl": "https://vk.com/video-9_9"},
            },
            PREVIEW_ID: {
                "videoId": PREVIEW_ID,
                "player": {"videoUrl": "https://vk.com/video-1_2"},
            },
        }
    }
    body = (
        "<html><script type='application/json'>"
        + json.dumps(state)
        + "</script></html>"
    ).encode()
    svc = _service(html_body=body)
    result = await svc.resolve(SUBMITTED_URL)
    assert result.canonical_provider_url == "https://vk.com/video-1_2"


@pytest.mark.asyncio
async def test_related_supported_url_not_selected() -> None:
    body = _target_html(
        PREVIEW_ID,
        video_url="https://vk.com/video-1_2",
        related_url=RELATED_DECOY,
    )
    svc = _service(html_body=body)
    result = await svc.resolve(SUBMITTED_URL)
    assert result.canonical_provider_url == "https://vk.com/video-1_2"
    assert RELATED_DECOY not in result.canonical_provider_url


@pytest.mark.asyncio
async def test_different_video_id_not_selected() -> None:
    state = {
        "dups": {
            "00000000000000000000": {
                "videoId": "00000000000000000000",
                "player": {"videoUrl": "https://vk.com/video-9_9"},
            }
        },
        "items": [
            {
                "videoId": "11111111111111111111",
                "player": {"videoUrl": "https://vk.com/video-8_8"},
            }
        ],
    }
    body = (
        "<html><script type='application/json'>"
        + json.dumps(state)
        + "</script></html>"
    ).encode()
    svc = _service(html_body=body)
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve(SUBMITTED_URL)
    assert exc.value.kind == ResolutionErrorKind.WRAPPER_UNRESOLVED
    assert exc.value.internal_reason == "TARGET_VIEWER_STATE_NOT_FOUND"


@pytest.mark.asyncio
async def test_missing_target_record_unresolved() -> None:
    body = b"<html><script type='application/json'>{\"dups\":{}}</script></html>"
    svc = _service(html_body=body, preview_path="/video/preview/1")
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve("https://yandex.ru/video/preview/1")
    assert exc.value.kind == ResolutionErrorKind.WRAPPER_UNRESOLVED
    assert exc.value.internal_reason == "TARGET_VIEWER_STATE_NOT_FOUND"


@pytest.mark.asyncio
async def test_conflicting_target_records_unresolved() -> None:
    # Same preview id appears as two distinct dict objects with different sources.
    state = {
        "dups": {
            "1": {"videoId": "1", "player": {"videoUrl": "https://vk.com/video-1_1"}}
        },
        "alt": {"videoId": "1", "player": {"videoUrl": "https://vk.com/video-2_2"}},
    }
    body = (
        "<html><script type='application/json'>"
        + json.dumps(state)
        + "</script></html>"
    ).encode()
    svc = _service(html_body=body, preview_path="/video/preview/1")
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve("https://yandex.ru/video/preview/1")
    assert exc.value.kind == ResolutionErrorKind.WRAPPER_UNRESOLVED
    assert exc.value.internal_reason == "AMBIGUOUS_TARGET_RECORD"


@pytest.mark.asyncio
async def test_http_vk_video_url_upgrades_without_fetch() -> None:
    calls: list[str] = []
    body = _target_html("1", video_url="http://vk.com/video-1_2")
    svc = _service(html_body=body, preview_path="/video/preview/1", track_calls=calls)
    result = await svc.resolve("https://yandex.ru/video/preview/1")
    assert result.canonical_provider_url == "https://vk.com/video-1_2"
    assert dict(result.resolution_chain[0].metadata)["scheme_upgraded"] is True
    assert all("vk.com" not in u for u in calls)
    assert all(u.startswith("https://yandex.ru/") for u in calls)


def test_http_arbitrary_host_not_upgraded() -> None:
    url, upgraded = _normalize_stable_provider_url(
        "http://evil.example/video-1_2",
        supported_hosts=HOSTS,
        allow_http_upgrade=True,
    )
    assert url is None
    assert upgraded is False


def test_http_credentials_not_upgraded() -> None:
    url, _ = _normalize_stable_provider_url(
        "http://user:pass@vk.com/video-1_2",
        supported_hosts=HOSTS,
        allow_http_upgrade=True,
    )
    assert url is None


def test_http_explicit_port_not_upgraded() -> None:
    url, _ = _normalize_stable_provider_url(
        "http://vk.com:8080/video-1_2",
        supported_hosts=HOSTS,
        allow_http_upgrade=True,
    )
    assert url is None


def test_http_embed_url_not_upgraded() -> None:
    url, _ = _normalize_stable_provider_url(
        "http://vkvideo.ru/video_ext.php?oid=-1&id=2&hash=x",
        supported_hosts=HOSTS,
        allow_http_upgrade=True,
    )
    assert url is None


@pytest.mark.asyncio
async def test_embed_url_video_ext_never_result() -> None:
    body = _target_html(
        "1",
        video_url=None,
        record_url=None,
        embed_url=EMBED_DECOY,
    )
    # Only embedUrl present — fail closed (not selected).
    state = {
        "dups": {
            "1": {
                "videoId": "1",
                "embedUrl": EMBED_DECOY,
                "player": {"playerUri": f"<iframe src='{EMBED_DECOY}'></iframe>"},
            }
        }
    }
    body = (
        "<html><script type='application/json'>"
        + json.dumps(state)
        + "</script></html>"
    ).encode()
    svc = _service(html_body=body, preview_path="/video/preview/1")
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve("https://yandex.ru/video/preview/1")
    assert exc.value.kind == ResolutionErrorKind.WRAPPER_UNRESOLVED


@pytest.mark.asyncio
async def test_signed_query_embed_never_result() -> None:
    body = _target_html(
        PREVIEW_ID,
        video_url="https://vk.com/video-1_2",
        embed_url=EMBED_DECOY,
    )
    svc = _service(html_body=body)
    result = await svc.resolve(SUBMITTED_URL)
    assert result.canonical_provider_url == "https://vk.com/video-1_2"
    assert "hash=" not in result.canonical_provider_url
    assert "/video_ext.php" not in result.canonical_provider_url
    assert VOLATILE_MARKER not in repr(result)


@pytest.mark.asyncio
async def test_stable_candidate_passes_provider_and_validator() -> None:
    body = _target_html("1", video_url="https://vkvideo.ru/video-1_2")
    svc = _service(html_body=body, preview_path="/video/preview/1")
    result = await svc.resolve("https://yandex.ru/video/preview/1")
    assert result.provider_id == "vk"
    assert result.canonical_provider_url == "https://vkvideo.ru/video-1_2"


@pytest.mark.asyncio
async def test_yandex_authoritative_vk_ru_clip_resolves_to_video_canonical() -> None:
    body = _target_html(
        "1",
        video_url="https://vk.ru/clip-235548483_456239236",
        embed_url=EMBED_DECOY,
    )
    body = body.replace(
        b"</html>",
        b'<a href="https://vk.com/video-9_9">related</a></html>',
    )
    svc = _service(html_body=body, preview_path="/video/preview/1")
    result = await svc.resolve("https://yandex.ru/video/preview/1")
    assert result.canonical_provider_url == (
        "https://vk.ru/video-235548483_456239236"
    )
    assert result.provider_id == "vk"
    assert "/clip" not in result.canonical_provider_url
    assert "video-9_9" not in result.canonical_provider_url


@pytest.mark.asyncio
async def test_unsupported_youtube_in_target() -> None:
    body = _target_html("1", video_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    svc = _service(html_body=body, preview_path="/video/preview/1")
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve("https://yandex.ru/video/preview/1")
    assert exc.value.kind == ResolutionErrorKind.RESOLVED_PROVIDER_UNSUPPORTED


@pytest.mark.asyncio
async def test_unsupported_ok_in_target() -> None:
    body = _target_html("1", video_url="https://ok.ru/video/123")
    svc = _service(html_body=body, preview_path="/video/preview/1")
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve("https://yandex.ru/video/preview/1")
    assert exc.value.kind == ResolutionErrorKind.RESOLVED_PROVIDER_UNSUPPORTED


@pytest.mark.asyncio
async def test_unsupported_only_in_related_ignored() -> None:
    body = _target_html(
        "1",
        video_url="https://vk.com/video-1_2",
        related_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    )
    svc = _service(html_body=body, preview_path="/video/preview/1")
    result = await svc.resolve("https://yandex.ru/video/preview/1")
    assert result.canonical_provider_url == "https://vk.com/video-1_2"


@pytest.mark.asyncio
async def test_unknown_json_key_url_ignored() -> None:
    state = {
        "dups": {"1": {"videoId": "1"}},
        "mysteryBlob": {"href": "https://vk.com/video-9_9"},
        "stream_url": "https://vk.com/video-8_8",
        "contentUrl": "https://vk.com/video-7_7",
    }
    body = (
        "<html><script type='application/json'>"
        + json.dumps(state)
        + "</script></html>"
    ).encode()
    svc = _service(html_body=body, preview_path="/video/preview/1")
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve("https://yandex.ru/video/preview/1")
    assert exc.value.kind == ResolutionErrorKind.WRAPPER_UNRESOLVED


@pytest.mark.asyncio
async def test_arbitrary_terminal_string_and_list_ignored() -> None:
    state = {
        "dups": {"1": {"videoId": "1"}},
        "noise": ["https://vk.com/video-9_9", "https://rutube.ru/video/abc/"],
        "leaf": "https://vk.com/video-8_8",
    }
    body = (
        "<html><script type='application/json'>"
        + json.dumps(state)
        + "</script></html>"
    ).encode()
    svc = _service(html_body=body, preview_path="/video/preview/1")
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve("https://yandex.ru/video/preview/1")
    assert exc.value.kind == ResolutionErrorKind.WRAPPER_UNRESOLVED


@pytest.mark.asyncio
async def test_nested_encoded_json_string_target_bound() -> None:
    body = _target_html(
        "1",
        video_url="https://rutube.ru/video/abcdef0123456789/",
        nested_encoded=True,
    )
    svc = _service(html_body=body, preview_path="/video/preview/1")
    result = await svc.resolve("https://yandex.ru/video/preview/1")
    assert result.provider_id == "rutube"
    assert result.canonical_provider_url == "https://rutube.ru/video/abcdef0123456789/"


def test_structural_node_budget_fail_closed() -> None:
    budget = _NodeBudget(max_nodes=3)
    found: list[Any] = []
    seen: set[int] = set()
    with pytest.raises(ResolutionError) as exc:
        _collect_target_records(
            {"a": {"b": {"c": {"d": 1}}}},
            preview_id="1",
            depth=0,
            budget=budget,
            found=found,
            seen_ids=seen,
            parent_dups=False,
            dups_key=None,
        )
    assert exc.value.internal_reason == "PARSER_LIMIT_EXCEEDED"


@pytest.mark.asyncio
async def test_strategy_and_metadata_match_path() -> None:
    body = _target_html("1", video_url="https://vk.com/video-1_2")
    svc = _service(html_body=body, preview_path="/video/preview/1")
    result = await svc.resolve("https://yandex.ru/video/preview/1")
    hop = result.resolution_chain[0]
    assert hop.strategy == STRATEGY_TARGET_VIDEO_URL
    meta = dict(hop.metadata)
    assert meta["target_bound"] is True
    assert meta["scheme_upgraded"] is False


@pytest.mark.asyncio
async def test_public_surfaces_omit_query_hash_preview_source() -> None:
    svc = _service(html_body=REGRESSION_HTML)
    result = await svc.resolve(f"{SUBMITTED_URL}?{SECRET}=1#frag")
    blob = repr(result) + repr(result.resolution_chain)
    assert SECRET not in blob
    assert VOLATILE_MARKER not in blob
    # Query/secret must not appear; submitted wrapper path is source_canonical
    # without query (wrapper safety strips/normalizes).
    assert SECRET not in result.resolution_chain[0].source_canonical
    assert "?" not in result.canonical_provider_url


@pytest.mark.asyncio
async def test_logs_repr_omit_volatile_fixture_markers() -> None:
    svc = _service(html_body=REGRESSION_HTML)
    result = await svc.resolve(SUBMITTED_URL)
    assert VOLATILE_MARKER not in repr(result)
    assert EMBED_DECOY not in repr(result)


@pytest.mark.asyncio
async def test_direct_vk_provider_first_no_yandex_fetch() -> None:
    calls: list[str] = []
    svc = _service(html_body=REGRESSION_HTML, track_calls=calls)
    result = await svc.resolve("https://vk.com/video-1_2")
    assert result.provenance == ResolutionProvenance.DIRECT_PROVIDER
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://yandex.ru/video/preview/abc",
        "https://yandex.ru/video/preview/1/extra",
        "http://yandex.ru/video/preview/1",
        "https://user:pass@yandex.ru/video/preview/1",
        "https://yandex.ru:8443/video/preview/1",
        "https://www.yandex.ru/video/preview/1",
        "https://yandex.ru.evil.example/video/preview/1",
        "https://notyandex.ru/video/preview/1",
        "https://ya.ru/video/preview/1",
    ],
)
async def test_yandex_shape_rejected(url: str) -> None:
    svc = _service(html_body=REGRESSION_HTML)
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve(url)
    assert exc.value.kind in {
        ResolutionErrorKind.WRAPPER_UNSUPPORTED,
        ResolutionErrorKind.UNSAFE_RESOLUTION_TARGET,
    }


@pytest.mark.asyncio
async def test_record_url_used_when_video_url_absent() -> None:
    body = _target_html(
        "1",
        video_url=None,
        record_url="https://vk.com/video-3_4",
    )
    svc = _service(html_body=body, preview_path="/video/preview/1")
    result = await svc.resolve("https://yandex.ru/video/preview/1")
    assert result.canonical_provider_url == "https://vk.com/video-3_4"


@pytest.mark.asyncio
async def test_record_url_embed_rejected_when_only_source() -> None:
    body = _target_html("1", video_url=None, record_url=EMBED_DECOY)
    svc = _service(html_body=body, preview_path="/video/preview/1")
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve("https://yandex.ru/video/preview/1")
    assert exc.value.kind == ResolutionErrorKind.WRAPPER_UNRESOLVED


def test_extract_unit_prefers_player_video_url() -> None:
    text = (
        '<script type="application/json">'
        + json.dumps(
            {
                "dups": {
                    "1": {
                        "videoId": "1",
                        "player": {
                            "videoUrl": "http://vk.com/video-1_2",
                        },
                        "embedUrl": EMBED_DECOY,
                        "url": EMBED_DECOY,
                    }
                }
            }
        )
        + "</script>"
    )
    extracted = _extract_target_bound(text, preview_id="1", supported_hosts=HOSTS)
    assert extracted.candidate_url == "https://vk.com/video-1_2"
    assert extracted.strategy == STRATEGY_TARGET_VIDEO_URL
    assert extracted.target_bound is True
    assert extracted.scheme_upgraded is True


def test_registry_reads_exact_hostnames_once() -> None:
    class CountingResolver:
        reads = 0

        @property
        def resolver_id(self) -> str:
            return "counting"

        @property
        def wrapper_type(self) -> str:
            return "counting_wrapper"

        @property
        def exact_hostnames(self) -> frozenset[str]:
            type(self).reads += 1
            return frozenset({"yandex.ru"})

        def matches(self, validated: object) -> bool:
            return True

        async def resolve(self, validated: object, *, documents: object) -> object:
            raise AssertionError("not used")

    CountingResolver.reads = 0
    counting = CountingResolver()
    registry = WrapperResolverRegistry.from_resolvers([counting])
    assert CountingResolver.reads == 1
    assert registry.find("yandex.ru") is not None
    assert CountingResolver.reads == 1


def test_build_wrapper_registry_registers_yandex_once() -> None:
    settings = _settings()
    providers = ProviderRegistry.from_settings(settings)
    registry = build_wrapper_registry(settings, providers)
    assert len(registry.registrations) == 1
    assert registry.registrations[0].resolver_id == "yandex_video_preview"
