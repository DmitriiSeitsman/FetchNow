"""Offline tests for Yandex viewer-bound iframe extraction (PR3B)."""

from __future__ import annotations

import html
import json
from typing import Any
from urllib.parse import quote

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
from fetchnow.resolution.yandex_preview import (
    _MAX_JSON_DEPTH,
    STRATEGY_TARGET_VIDEO_URL,
    STRATEGY_VIEWER_IFRAME_VIDEO_URL,
    _classify_trusted_vk_player_iframe,
    _decode_counters_video_url,
    _extract_target_bound,
    _normalize_stable_provider_url,
    _parse_iframe_fragment_fields,
    _viewer_binding_status,
)
from fetchnow.url.dns import FakeDnsResolver
from fetchnow.url.providers import ProviderRegistry
from fetchnow.url.validate import URLValidator
from yandex_preview_regression_data import (
    BUILD_SEGMENT,
    DUPS_COMPAT_HTML,
    EMBED_DECOY,
    EXPECTED_CANONICAL,
    PLAYER_PATH,
    PREVIEW_ID,
    REGRESSION_HTML,
    RELATED_DECOY,
    SUBMITTED_URL,
    TRUSTED_IFRAME_SRC,
    VOLATILE_MARKER,
)

SECRET = "yandex-iframe-secret-marker"
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
            "rutube.ru": ["8.8.4.4"],
        }
    )
    providers = ProviderRegistry.from_settings(cfg)
    wrappers = build_wrapper_registry(cfg, providers)
    validator = URLValidator(cfg, registry=providers, resolver=resolver)
    path = preview_path or f"/video/preview/{PREVIEW_ID}"

    def handler(request: httpx.Request) -> httpx.Response:
        if track_calls is not None:
            track_calls.append(str(request.url))
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


def _data_state_attr(payload: object) -> str:
    return html.escape(json.dumps(payload, separators=(",", ":")), quote=True)


def _viewer_state(
    preview_id: str = PREVIEW_ID,
    *,
    location: str | None = None,
    has_viewer: bool = True,
    video_id: str | None = "",
    related_items: list[dict[str, Any]] | None = None,
    extra_root: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "location": (
            location if location is not None else f"/video/preview/{preview_id}"
        ),
        "preloadedState": {"clips": {"dups": {}, "items": []}},
    }
    if has_viewer:
        viewer: dict[str, Any] = {
            "internal": {"videoId": video_id, "isEmbedded": False},
            "clips": {"dups": {}},
        }
        if related_items is not None:
            viewer["related"] = {"items": related_items}
        state["preloadedState"]["viewer"] = viewer
    if extra_root:
        state.update(extra_root)
    return state


def _iframe_fragment(
    video_url: str,
    *,
    html_decoy: str | None = None,
    extra_counters: dict[str, Any] | None = None,
    counters_encoded: bool = True,
    counters_html_entities: bool = False,
) -> str:
    counters: dict[str, Any] = {"videoUrl": video_url}
    if extra_counters:
        counters.update(extra_counters)
    counters_json = json.dumps(counters, separators=(",", ":"))
    if counters_html_entities:
        counters_json = html.escape(counters_json, quote=False)
    counters_value = (
        quote(counters_json, safe="") if counters_encoded else counters_json
    )
    decoy = html_decoy or (
        f"<iframe src='{EMBED_DECOY}' allowfullscreen></iframe>"
    )
    return (
        f"html={quote(decoy, safe='')}"
        f"&event_prefix=fixture"
        f"&counters={counters_value}"
        f"&service=fixture"
    )


def _iframe_src(
    build_segment: str = BUILD_SEGMENT,
    *,
    fragment: str | None = None,
    scheme: str = "//",
) -> str:
    path = f"/video-player/{build_segment}/pages-common/vk/vk.html"
    frag = fragment or _iframe_fragment("http://vk.com/video-161264992_456240043")
    return f"{scheme}yastatic.net{path}#{frag}"


def _viewer_iframe_html(
    *,
    preview_id: str = PREVIEW_ID,
    viewer: dict[str, Any] | None = None,
    iframe_srcs: list[str] | None = None,
    extra_data_states: list[dict[str, Any]] | None = None,
    dups_script: dict[str, Any] | None = None,
) -> bytes:
    parts = ["<!DOCTYPE html><html><body>"]
    if extra_data_states:
        for state in extra_data_states:
            parts.append(f'<div data-state="{_data_state_attr(state)}"></div>')
    if viewer is not None:
        parts.append(f'<div data-state="{_data_state_attr(viewer)}"></div>')
    for src in iframe_srcs or []:
        parts.append(f'<iframe src="{html.escape(src, quote=True)}"></iframe>')
    if dups_script is not None:
        parts.append(
            '<script type="application/json">'
            + json.dumps(dups_script, separators=(",", ":"))
            + "</script>"
        )
    parts.append("</body></html>")
    return "".join(parts).encode()


def _dups_html(
    preview_id: str,
    *,
    video_url: str = "https://vk.com/video-1_2",
) -> bytes:
    state = {
        "dups": {
            preview_id: {
                "videoId": preview_id,
                "player": {"videoUrl": video_url},
            }
        }
    }
    return (
        "<html><script type='application/json'>"
        + json.dumps(state)
        + "</script></html>"
    ).encode()


# --- Viewer binding ---


def _raw_data_state(payload: object) -> str:
    return json.dumps(payload, separators=(",", ":"))


def test_viewer_binding_match() -> None:
    state = _viewer_state(PREVIEW_ID)
    raw = _raw_data_state(state)
    assert _viewer_binding_status([raw], preview_id=PREVIEW_ID) == "ok"


def test_viewer_binding_mismatch() -> None:
    state = _viewer_state(PREVIEW_ID, video_id="99999999999999999999")
    raw = _raw_data_state(state)
    assert _viewer_binding_status([raw], preview_id=PREVIEW_ID) == "mismatch"


def test_viewer_binding_missing() -> None:
    assert _viewer_binding_status([], preview_id=PREVIEW_ID) == "not_found"


def test_viewer_binding_ambiguous_conflicting_states() -> None:
    s1 = _raw_data_state(_viewer_state(PREVIEW_ID))
    s2 = _raw_data_state(_viewer_state(PREVIEW_ID))
    assert _viewer_binding_status([s1, s2], preview_id=PREVIEW_ID) == "ambiguous"


def test_viewer_binding_id_only_in_unrelated_field() -> None:
    state = {
        "location": "/video/preview/other",
        "unrelatedVideoId": PREVIEW_ID,
        "preloadedState": {
            "viewer": {
                "internal": {"videoId": ""},
                "clips": {"dups": {}},
            }
        },
    }
    raw = _raw_data_state(state)
    assert _viewer_binding_status([raw], preview_id=PREVIEW_ID) == "not_found"


@pytest.mark.asyncio
async def test_empty_dups_allows_iframe_path() -> None:
    svc = _service(html_body=REGRESSION_HTML)
    result = await svc.resolve(SUBMITTED_URL)
    assert result.canonical_provider_url == EXPECTED_CANONICAL
    assert result.resolution_chain[0].strategy == STRATEGY_VIEWER_IFRAME_VIDEO_URL


@pytest.mark.asyncio
async def test_related_id_does_not_bind() -> None:
    viewer = _viewer_state(
        PREVIEW_ID,
        related_items=[
            {"videoId": "00000000000000000000", "url": RELATED_DECOY},
        ],
    )
    body = _viewer_iframe_html(
        viewer=viewer,
        iframe_srcs=[TRUSTED_IFRAME_SRC],
    )
    svc = _service(html_body=body)
    result = await svc.resolve(SUBMITTED_URL)
    assert result.canonical_provider_url == EXPECTED_CANONICAL
    assert RELATED_DECOY not in result.canonical_provider_url


@pytest.mark.asyncio
async def test_viewer_binding_mismatch_unresolved() -> None:
    viewer = _viewer_state(PREVIEW_ID, video_id="99999999999999999999")
    body = _viewer_iframe_html(viewer=viewer, iframe_srcs=[TRUSTED_IFRAME_SRC])
    svc = _service(html_body=body)
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve(SUBMITTED_URL)
    assert exc.value.internal_reason == "TARGET_VIEWER_ID_MISMATCH"


@pytest.mark.asyncio
async def test_viewer_binding_missing_unresolved() -> None:
    body = _viewer_iframe_html(viewer=None, iframe_srcs=[TRUSTED_IFRAME_SRC])
    svc = _service(html_body=body)
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve(SUBMITTED_URL)
    assert exc.value.internal_reason == "TARGET_VIEWER_STATE_NOT_FOUND"


@pytest.mark.asyncio
async def test_viewer_binding_ambiguous_unresolved() -> None:
    s1 = _viewer_state(PREVIEW_ID)
    s2 = _viewer_state(PREVIEW_ID)
    body = _viewer_iframe_html(
        extra_data_states=[s1, s2],
        iframe_srcs=[TRUSTED_IFRAME_SRC],
    )
    svc = _service(html_body=body)
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve(SUBMITTED_URL)
    assert exc.value.internal_reason == "AMBIGUOUS_VIEWER_STATE"


# --- Iframe validation ---


@pytest.mark.parametrize(
    ("src", "accepted"),
    [
        (TRUSTED_IFRAME_SRC, True),
        (_iframe_src(scheme="//"), True),
        (_iframe_src(scheme="https://"), True),
        (_iframe_src(scheme="http://"), False),
        (_iframe_src(scheme="https://user:pass@"), False),
        (
            "https://yastatic.net:8443/video-player/0xdeadbeef01234567/"
            "pages-common/vk/vk.html",
            False,
        ),
        (
            "https://yastatic.net.evil.example/video-player/0xdeadbeef01234567/"
            "pages-common/vk/vk.html",
            False,
        ),
        (
            "https://evil.example/video-player/0xdeadbeef01234567/"
            "pages-common/vk/vk.html",
            False,
        ),
        ("https://yastatic.net/video-player/nothex/pages-common/vk/vk.html", False),
        ("https://yastatic.net/video-player/0xshort/pages-common/vk/vk.html", False),
        ("https://yastatic.net/other/pages-common/vk/vk.html", False),
    ],
)
def test_classify_trusted_vk_player_iframe(src: str, accepted: bool) -> None:
    result = _classify_trusted_vk_player_iframe(src)
    assert (result is not None) is accepted


def test_oversized_build_segment_rejected() -> None:
    long_build = "0x" + "a" * 25
    src = _iframe_src(build_segment=long_build)
    assert _classify_trusted_vk_player_iframe(src) is None


@pytest.mark.asyncio
async def test_iframe_limit_exceeded() -> None:
    src = _iframe_src()
    iframes = [src] * 9
    body = _viewer_iframe_html(
        viewer=_viewer_state(PREVIEW_ID),
        iframe_srcs=iframes,
    )
    svc = _service(html_body=body)
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve(SUBMITTED_URL)
    assert exc.value.internal_reason == "PARSER_LIMIT_EXCEEDED"


@pytest.mark.asyncio
async def test_conflicting_iframes_unresolved() -> None:
    src_a = _iframe_src(fragment=_iframe_fragment("http://vk.com/video-1_2"))
    src_b = _iframe_src(fragment=_iframe_fragment("http://vk.com/video-3_4"))
    body = _viewer_iframe_html(
        viewer=_viewer_state(PREVIEW_ID),
        iframe_srcs=[src_a, src_b],
    )
    svc = _service(html_body=body)
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve(SUBMITTED_URL)
    assert exc.value.internal_reason == "AMBIGUOUS_TARGET_PLAYER"


@pytest.mark.asyncio
async def test_duplicate_equivalent_iframes_ok() -> None:
    src = _iframe_src(fragment=_iframe_fragment("http://vk.com/video-1_2"))
    body = _viewer_iframe_html(
        viewer=_viewer_state("1"),
        iframe_srcs=[src, src],
    )
    svc = _service(html_body=body, preview_path="/video/preview/1")
    result = await svc.resolve("https://yandex.ru/video/preview/1")
    assert result.canonical_provider_url == "https://vk.com/video-1_2"


@pytest.mark.asyncio
async def test_no_network_request_to_yastatic() -> None:
    calls: list[str] = []
    svc = _service(html_body=REGRESSION_HTML, track_calls=calls)
    await svc.resolve(SUBMITTED_URL)
    assert all("yastatic.net" not in u for u in calls)
    assert all(u.startswith("https://yandex.ru/") for u in calls)


# --- Payload ---


@pytest.mark.asyncio
async def test_live_shaped_resolves() -> None:
    svc = _service(html_body=REGRESSION_HTML)
    result = await svc.resolve(SUBMITTED_URL)
    assert result.canonical_provider_url == EXPECTED_CANONICAL
    meta = dict(result.resolution_chain[0].metadata)
    assert meta["iframe_payload"] is True


@pytest.mark.asyncio
async def test_payload_html_entities() -> None:
    src = _iframe_src(
        fragment=_iframe_fragment(
            "http://vk.com/video-1_2",
            counters_html_entities=True,
        )
    )
    body = _viewer_iframe_html(
        viewer=_viewer_state("1"),
        iframe_srcs=[src],
    )
    svc = _service(html_body=body, preview_path="/video/preview/1")
    result = await svc.resolve("https://yandex.ru/video/preview/1")
    assert result.canonical_provider_url == "https://vk.com/video-1_2"


@pytest.mark.asyncio
async def test_payload_percent_encoding() -> None:
    src = _iframe_src(
        fragment=_iframe_fragment(
            "http://vk.com/video-1_2",
            counters_encoded=True,
        )
    )
    body = _viewer_iframe_html(
        viewer=_viewer_state("1"),
        iframe_srcs=[src],
    )
    svc = _service(html_body=body, preview_path="/video/preview/1")
    result = await svc.resolve("https://yandex.ru/video/preview/1")
    assert result.canonical_provider_url == "https://vk.com/video-1_2"


@pytest.mark.asyncio
async def test_payload_missing_video_url() -> None:
    counters = quote(json.dumps({"duration": 1}, separators=(",", ":")), safe="")
    src = f"//yastatic.net{PLAYER_PATH}#counters={counters}"
    body = _viewer_iframe_html(
        viewer=_viewer_state("1"),
        iframe_srcs=[src],
    )
    svc = _service(html_body=body, preview_path="/video/preview/1")
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve("https://yandex.ru/video/preview/1")
    assert exc.value.internal_reason == "TARGET_VIDEO_URL_NOT_FOUND"


@pytest.mark.asyncio
async def test_payload_malformed_counters() -> None:
    src = f"//yastatic.net{PLAYER_PATH}#counters=not-json"
    body = _viewer_iframe_html(
        viewer=_viewer_state("1"),
        iframe_srcs=[src],
    )
    svc = _service(html_body=body, preview_path="/video/preview/1")
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve("https://yandex.ru/video/preview/1")
    assert exc.value.internal_reason == "TARGET_PLAYER_PAYLOAD_INVALID"


@pytest.mark.asyncio
async def test_payload_oversized_counters() -> None:
    huge = "x" * 5000
    src = _iframe_src(
        fragment=_iframe_fragment(
            "http://vk.com/video-1_2",
            extra_counters={"noise": huge},
        )
    )
    body = _viewer_iframe_html(
        viewer=_viewer_state("1"),
        iframe_srcs=[src],
    )
    svc = _service(html_body=body, preview_path="/video/preview/1")
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve("https://yandex.ru/video/preview/1")
    assert exc.value.internal_reason == "TARGET_PLAYER_NOT_FOUND"


@pytest.mark.asyncio
async def test_payload_too_many_fragment_fields() -> None:
    fields = "&".join(f"f{i}=v" for i in range(40))
    counters = quote(
        json.dumps({"videoUrl": "http://vk.com/video-1_2"}),
        safe="",
    )
    src = f"//yastatic.net{PLAYER_PATH}#{fields}&counters={counters}"
    body = _viewer_iframe_html(
        viewer=_viewer_state("1"),
        iframe_srcs=[src],
    )
    svc = _service(html_body=body, preview_path="/video/preview/1")
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve("https://yandex.ru/video/preview/1")
    assert exc.value.internal_reason == "PARSER_LIMIT_EXCEEDED"


@pytest.mark.asyncio
async def test_payload_unknown_keys_ignored() -> None:
    src = _iframe_src(
        fragment=_iframe_fragment(
            "http://vk.com/video-1_2",
            extra_counters={"unknownKey": "ignored", "table": "fixture"},
        )
    )
    body = _viewer_iframe_html(
        viewer=_viewer_state("1"),
        iframe_srcs=[src],
    )
    svc = _service(html_body=body, preview_path="/video/preview/1")
    result = await svc.resolve("https://yandex.ru/video/preview/1")
    assert result.canonical_provider_url == "https://vk.com/video-1_2"


@pytest.mark.asyncio
async def test_embed_video_ext_never_selected() -> None:
    src = _iframe_src(
        fragment=_iframe_fragment(
            EMBED_DECOY,
            html_decoy=f"<iframe src='{EMBED_DECOY}'></iframe>",
        )
    )
    body = _viewer_iframe_html(
        viewer=_viewer_state("1"),
        iframe_srcs=[src],
    )
    svc = _service(html_body=body, preview_path="/video/preview/1")
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve("https://yandex.ru/video/preview/1")
    assert exc.value.kind == ResolutionErrorKind.WRAPPER_UNRESOLVED


@pytest.mark.asyncio
async def test_hash_query_not_in_repr() -> None:
    svc = _service(html_body=REGRESSION_HTML)
    result = await svc.resolve(f"{SUBMITTED_URL}?{SECRET}=1#frag")
    blob = repr(result) + repr(result.resolution_chain)
    assert SECRET not in blob
    assert VOLATILE_MARKER not in blob
    assert "hash=" not in blob
    assert "yastatic.net" not in blob


@pytest.mark.asyncio
async def test_http_upgrade_without_fetch() -> None:
    src = _iframe_src(fragment=_iframe_fragment("http://vk.com/video-1_2"))
    body = _viewer_iframe_html(
        viewer=_viewer_state("1"),
        iframe_srcs=[src],
    )
    calls: list[str] = []
    svc = _service(
        html_body=body,
        preview_path="/video/preview/1",
        track_calls=calls,
    )
    result = await svc.resolve("https://yandex.ru/video/preview/1")
    assert result.canonical_provider_url == "https://vk.com/video-1_2"
    assert dict(result.resolution_chain[0].metadata)["scheme_upgraded"] is True
    assert all("vk.com" not in u for u in calls)


@pytest.mark.parametrize(
    ("video_url", "expected"),
    [
        ("http://vk.com/video123_456", "https://vk.com/video123_456"),
        ("http://vk.com/video-123_456", "https://vk.com/video-123_456"),
    ],
)
@pytest.mark.asyncio
async def test_positive_negative_owners(
    video_url: str, expected: str
) -> None:
    src = _iframe_src(fragment=_iframe_fragment(video_url))
    body = _viewer_iframe_html(
        viewer=_viewer_state("1"),
        iframe_srcs=[src],
    )
    svc = _service(html_body=body, preview_path="/video/preview/1")
    result = await svc.resolve("https://yandex.ru/video/preview/1")
    assert result.canonical_provider_url == expected


def test_double_minus_rejected() -> None:
    url, upgraded = _normalize_stable_provider_url(
        "http://vk.com/video--123_456",
        supported_hosts=HOSTS,
        allow_http_upgrade=True,
    )
    assert url is None
    assert upgraded is False


@pytest.mark.asyncio
async def test_extract_unit_iframe_strategy() -> None:
    text = REGRESSION_HTML.decode()
    extracted = _extract_target_bound(
        text, preview_id=PREVIEW_ID, supported_hosts=HOSTS
    )
    assert extracted.candidate_url == EXPECTED_CANONICAL
    assert extracted.strategy == STRATEGY_VIEWER_IFRAME_VIDEO_URL
    assert extracted.iframe_payload is True
    assert extracted.scheme_upgraded is True


# --- Precedence ---


@pytest.mark.asyncio
async def test_precedence_dups_only() -> None:
    svc = _service(html_body=DUPS_COMPAT_HTML)
    result = await svc.resolve(SUBMITTED_URL)
    assert result.canonical_provider_url == EXPECTED_CANONICAL
    assert result.resolution_chain[0].strategy == STRATEGY_TARGET_VIDEO_URL


@pytest.mark.asyncio
async def test_precedence_iframe_only() -> None:
    svc = _service(html_body=REGRESSION_HTML)
    result = await svc.resolve(SUBMITTED_URL)
    assert result.resolution_chain[0].strategy == STRATEGY_VIEWER_IFRAME_VIDEO_URL


@pytest.mark.asyncio
async def test_precedence_both_same() -> None:
    dups = {
        "dups": {
            PREVIEW_ID: {
                "videoId": PREVIEW_ID,
                "player": {"videoUrl": "http://vk.com/video-161264992_456240043"},
            }
        }
    }
    body = _viewer_iframe_html(
        viewer=_viewer_state(PREVIEW_ID),
        iframe_srcs=[TRUSTED_IFRAME_SRC],
        dups_script=dups,
    )
    svc = _service(html_body=body)
    result = await svc.resolve(SUBMITTED_URL)
    assert result.canonical_provider_url == EXPECTED_CANONICAL
    assert result.resolution_chain[0].strategy == STRATEGY_TARGET_VIDEO_URL


@pytest.mark.asyncio
async def test_precedence_both_conflict() -> None:
    dups = {
        "dups": {
            PREVIEW_ID: {
                "videoId": PREVIEW_ID,
                "player": {"videoUrl": "https://vk.com/video-9_9"},
            }
        }
    }
    body = _viewer_iframe_html(
        viewer=_viewer_state(PREVIEW_ID),
        iframe_srcs=[TRUSTED_IFRAME_SRC],
        dups_script=dups,
    )
    svc = _service(html_body=body)
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve(SUBMITTED_URL)
    assert exc.value.internal_reason == "AMBIGUOUS_TARGET_RECORD"


@pytest.mark.asyncio
async def test_precedence_related_cannot_override() -> None:
    viewer = _viewer_state(
        PREVIEW_ID,
        related_items=[
            {
                "videoId": "00000000000000000000",
                "player": {"videoUrl": RELATED_DECOY},
            }
        ],
    )
    body = _viewer_iframe_html(
        viewer=viewer,
        iframe_srcs=[TRUSTED_IFRAME_SRC],
    )
    svc = _service(html_body=body)
    result = await svc.resolve(SUBMITTED_URL)
    assert result.canonical_provider_url == EXPECTED_CANONICAL
    assert RELATED_DECOY not in result.canonical_provider_url


@pytest.mark.asyncio
async def test_dups_only_without_viewer_still_works() -> None:
    svc = _service(html_body=_dups_html("1"), preview_path="/video/preview/1")
    result = await svc.resolve("https://yandex.ru/video/preview/1")
    assert result.canonical_provider_url == "https://vk.com/video-1_2"
    assert result.provenance == ResolutionProvenance.WRAPPER_RESOLVED


# --- Fragment / query hardening ---


@pytest.mark.asyncio
async def test_iframe_empty_query_accepted() -> None:
    src = _iframe_src(fragment=_iframe_fragment("http://vk.com/video-1_2"))
    assert "?" not in src.split("#", 1)[0]
    body = _viewer_iframe_html(viewer=_viewer_state("1"), iframe_srcs=[src])
    svc = _service(html_body=body, preview_path="/video/preview/1")
    result = await svc.resolve("https://yandex.ru/video/preview/1")
    assert result.canonical_provider_url == "https://vk.com/video-1_2"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    ["x=1", "counters=evil", f"token={SECRET}"],
)
async def test_iframe_nonempty_query_rejected(query: str) -> None:
    frag = _iframe_fragment("http://vk.com/video-1_2")
    src = f"//yastatic.net{PLAYER_PATH}?{query}#{frag}"
    body = _viewer_iframe_html(viewer=_viewer_state("1"), iframe_srcs=[src])
    svc = _service(html_body=body, preview_path="/video/preview/1")
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve("https://yandex.ru/video/preview/1")
    assert exc.value.internal_reason == "TARGET_PLAYER_PAYLOAD_INVALID"
    assert SECRET not in repr(exc.value)


def test_fragment_malformed_field_without_equals_rejected() -> None:
    with pytest.raises(ResolutionError) as exc:
        _parse_iframe_fragment_fields("counters")
    assert exc.value.internal_reason == "TARGET_PLAYER_PAYLOAD_INVALID"


def test_fragment_empty_field_name_rejected() -> None:
    with pytest.raises(ResolutionError) as exc:
        _parse_iframe_fragment_fields("=value&counters=%7B%7D")
    assert exc.value.internal_reason == "TARGET_PLAYER_PAYLOAD_INVALID"


def test_fragment_control_char_in_field_name_rejected() -> None:
    with pytest.raises(ResolutionError) as exc:
        _parse_iframe_fragment_fields("bad\x01name=1&counters=%7B%7D")
    assert exc.value.internal_reason == "TARGET_PLAYER_PAYLOAD_INVALID"


def test_fragment_oversized_field_name_rejected() -> None:
    name = "n" * 65
    with pytest.raises(ResolutionError) as exc:
        _parse_iframe_fragment_fields(f"{name}=1&counters=%7B%7D")
    assert exc.value.internal_reason == "PARSER_LIMIT_EXCEEDED"


def test_fragment_oversized_field_value_rejected() -> None:
    value = "v" * 4097
    with pytest.raises(ResolutionError) as exc:
        _parse_iframe_fragment_fields(f"counters={value}")
    assert exc.value.internal_reason == "PARSER_LIMIT_EXCEEDED"


def test_fragment_incomplete_percent_escape_rejected() -> None:
    # One hex digit without a second is incomplete; documented residual policy.
    with pytest.raises(ResolutionError) as exc:
        _parse_iframe_fragment_fields("counters=abc%2")
    assert exc.value.internal_reason == "TARGET_PLAYER_PAYLOAD_INVALID"
    with pytest.raises(ResolutionError) as exc2:
        _parse_iframe_fragment_fields("counters=abc%2G")
    assert exc2.value.internal_reason == "TARGET_PLAYER_PAYLOAD_INVALID"


def test_fragment_literal_percent_and_zz_residue_allowed_in_unknown_field() -> None:
    # CSS-like 100% and non-hex %ZZ residues are not incomplete hex escapes.
    fields = _parse_iframe_fragment_fields(
        "html=width%3D100%25&other=%25ZZ&counters="
        "%7B%22videoUrl%22%3A%22http%3A%2F%2Fvk.com%2Fvideo-1_2%22%7D"
    )
    names = [n for n, _ in fields]
    assert "html" in names and "counters" in names


@pytest.mark.asyncio
async def test_duplicate_counters_different_rejected() -> None:
    c1 = quote(json.dumps({"videoUrl": "http://vk.com/video-1_2"}), safe="")
    c2 = quote(json.dumps({"videoUrl": "http://vk.com/video-3_4"}), safe="")
    src = f"//yastatic.net{PLAYER_PATH}#counters={c1}&counters={c2}"
    body = _viewer_iframe_html(viewer=_viewer_state("1"), iframe_srcs=[src])
    svc = _service(html_body=body, preview_path="/video/preview/1")
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve("https://yandex.ru/video/preview/1")
    assert exc.value.internal_reason == "AMBIGUOUS_TARGET_PLAYER"


@pytest.mark.asyncio
async def test_duplicate_counters_identical_rejected() -> None:
    c1 = quote(json.dumps({"videoUrl": "http://vk.com/video-1_2"}), safe="")
    src = f"//yastatic.net{PLAYER_PATH}#counters={c1}&counters={c1}"
    body = _viewer_iframe_html(viewer=_viewer_state("1"), iframe_srcs=[src])
    svc = _service(html_body=body, preview_path="/video/preview/1")
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve("https://yandex.ru/video/preview/1")
    assert exc.value.internal_reason == "AMBIGUOUS_TARGET_PLAYER"


@pytest.mark.asyncio
async def test_counters_case_and_brackets_do_not_count() -> None:
    decoy = quote(json.dumps({"videoUrl": "http://vk.com/video-9_9"}), safe="")
    real = quote(json.dumps({"videoUrl": "http://vk.com/video-1_2"}), safe="")
    # Counters / counters[] are not the exact field name; only exact counters wins.
    src = (
        f"//yastatic.net{PLAYER_PATH}"
        f"#Counters={decoy}&counters[]={decoy}&counters={real}"
    )
    # counters[] fails field-name charset → invalid
    body = _viewer_iframe_html(viewer=_viewer_state("1"), iframe_srcs=[src])
    svc = _service(html_body=body, preview_path="/video/preview/1")
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve("https://yandex.ru/video/preview/1")
    assert exc.value.internal_reason == "TARGET_PLAYER_PAYLOAD_INVALID"


@pytest.mark.asyncio
async def test_counters_case_variant_alone_missing() -> None:
    decoy = quote(json.dumps({"videoUrl": "http://vk.com/video-9_9"}), safe="")
    src = f"//yastatic.net{PLAYER_PATH}#Counters={decoy}"
    body = _viewer_iframe_html(viewer=_viewer_state("1"), iframe_srcs=[src])
    svc = _service(html_body=body, preview_path="/video/preview/1")
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve("https://yandex.ru/video/preview/1")
    assert exc.value.internal_reason == "TARGET_VIDEO_URL_NOT_FOUND"


def test_nested_video_url_not_accepted() -> None:
    payload = json.dumps({"nested": {"videoUrl": "http://vk.com/video-1_2"}})
    with pytest.raises(ResolutionError) as exc:
        _decode_counters_video_url(payload)
    assert exc.value.internal_reason == "TARGET_VIDEO_URL_NOT_FOUND"


def test_list_and_scalar_root_rejected() -> None:
    with pytest.raises(ResolutionError) as exc:
        _decode_counters_video_url("[]")
    assert exc.value.internal_reason == "TARGET_PLAYER_PAYLOAD_INVALID"
    with pytest.raises(ResolutionError) as exc2:
        _decode_counters_video_url('"http://vk.com/video-1_2"')
    # string root decodes to str then fails mapping check or decode layer
    assert exc2.value.internal_reason in {
        "TARGET_PLAYER_PAYLOAD_INVALID",
        "PARSER_LIMIT_EXCEEDED",
    }


def test_json_depth_at_and_above_limit() -> None:
    # Root depth=1; pad chain places the leaf string at depth == MAX.
    node: Any = "leaf"
    for _ in range(_MAX_JSON_DEPTH - 2):
        node = {"pad": node}
    ok = {"videoUrl": "http://vk.com/video-1_2", "pad": node}
    assert _decode_counters_video_url(json.dumps(ok)) == "http://vk.com/video-1_2"

    too_deep: Any = "leaf"
    for _ in range(_MAX_JSON_DEPTH - 1):
        too_deep = {"pad": too_deep}
    bad = {"videoUrl": "http://vk.com/video-1_2", "pad": too_deep}
    with pytest.raises(ResolutionError) as exc:
        _decode_counters_video_url(json.dumps(bad))
    assert exc.value.internal_reason == "PARSER_LIMIT_EXCEEDED"


def test_json_nodes_at_and_above_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "fetchnow.resolution.yandex_preview._MAX_JSON_NODES", 3
    )
    assert (
        _decode_counters_video_url(
            json.dumps({"videoUrl": "http://vk.com/video-1_2", "a": 1})
        )
        == "http://vk.com/video-1_2"
    )
    with pytest.raises(ResolutionError) as exc:
        _decode_counters_video_url(
            json.dumps({"videoUrl": "http://vk.com/video-1_2", "a": 1, "b": 2})
        )
    assert exc.value.internal_reason == "PARSER_LIMIT_EXCEEDED"


def test_json_long_key_and_string_rejected() -> None:
    long_key = {"videoUrl": "http://vk.com/video-1_2", "k" * 65: 1}
    with pytest.raises(ResolutionError) as exc:
        _decode_counters_video_url(json.dumps(long_key))
    assert exc.value.internal_reason == "PARSER_LIMIT_EXCEEDED"

    long_str = {"videoUrl": "http://vk.com/video-1_2", "pad": "x" * 4097}
    with pytest.raises(ResolutionError) as exc2:
        _decode_counters_video_url(json.dumps(long_str))
    assert exc2.value.internal_reason == "PARSER_LIMIT_EXCEEDED"


def test_recursion_error_becomes_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise RecursionError

    monkeypatch.setattr(
        "fetchnow.resolution.yandex_preview._validate_counters_json_structure",
        boom,
    )
    with pytest.raises(ResolutionError) as exc:
        _decode_counters_video_url(
            json.dumps({"videoUrl": "http://vk.com/video-1_2"})
        )
    assert exc.value.internal_reason == "PARSER_LIMIT_EXCEEDED"


def test_nested_json_string_decode_at_limit() -> None:
    inner = json.dumps({"videoUrl": "http://vk.com/video-1_2"})
    mid = json.dumps(inner)
    outer = json.dumps(mid)
    # 3 json.loads layers == _MAX_DECODE_PASSES
    assert _decode_counters_video_url(outer) == "http://vk.com/video-1_2"


def test_nested_json_string_decode_above_limit() -> None:
    payload: Any = {"videoUrl": "http://vk.com/video-1_2"}
    for _ in range(4):
        payload = json.dumps(payload)
    assert isinstance(payload, str)
    with pytest.raises(ResolutionError) as exc:
        _decode_counters_video_url(payload)
    assert exc.value.internal_reason == "PARSER_LIMIT_EXCEEDED"


@pytest.mark.asyncio
async def test_missing_counters_field_rejected() -> None:
    src = f"//yastatic.net{PLAYER_PATH}#html=x&service=y"
    body = _viewer_iframe_html(viewer=_viewer_state("1"), iframe_srcs=[src])
    svc = _service(html_body=body, preview_path="/video/preview/1")
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve("https://yandex.ru/video/preview/1")
    assert exc.value.internal_reason == "TARGET_VIDEO_URL_NOT_FOUND"


@pytest.mark.asyncio
async def test_query_secret_absent_from_error_surfaces() -> None:
    frag = _iframe_fragment("http://vk.com/video-1_2")
    src = f"https://yastatic.net{PLAYER_PATH}?access_token={SECRET}#{frag}"
    body = _viewer_iframe_html(viewer=_viewer_state("1"), iframe_srcs=[src])
    svc = _service(html_body=body, preview_path="/video/preview/1")
    with pytest.raises(ResolutionError) as exc:
        await svc.resolve("https://yandex.ru/video/preview/1")
    blob = repr(exc.value) + str(exc.value.internal_reason)
    assert SECRET not in blob
    assert "access_token" not in blob
