"""API tests for POST /api/v1/media/resolve (PR3B)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from fetchnow.api.main import create_app
from fetchnow.api.v1 import media as media_mod
from fetchnow.core.config import Settings
from fetchnow.network.client import SafeHTTPClient
from fetchnow.resolution.defaults import build_wrapper_registry
from fetchnow.resolution.service import ResolutionService
from fetchnow.url.dns import FakeDnsResolver
from fetchnow.url.providers import ProviderRegistry
from fetchnow.url.validate import URLValidator
from yandex_preview_regression_data import (
    EMBED_DECOY,
    EXPECTED_CANONICAL,
    REGRESSION_HTML,
    SUBMITTED_URL,
    VOLATILE_MARKER,
)

SECRET = "resolve-api-secret-marker"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        APP_NAME="FetchNow",
        APP_ENV="test",
        LOG_LEVEL="INFO",
        DATABASE_URL="postgresql+asyncpg://fetchnow:fetchnow@localhost:5432/fetchnow",
        URL_ALLOWED_SCHEMES="http,https",
        URL_ALLOWED_PORTS="80,443",
        URL_MAX_LENGTH=4096,
        URL_MAX_REDIRECTS=3,
        DNS_RESOLUTION_TIMEOUT_SECONDS=1,
        PROVIDER_VK_ENABLED=True,
        PROVIDER_RUTUBE_ENABLED=True,
        OUTBOUND_CONNECT_TIMEOUT_SECONDS=1,
        OUTBOUND_READ_TIMEOUT_SECONDS=1,
        OUTBOUND_TOTAL_TIMEOUT_SECONDS=5,
        OUTBOUND_MAX_RESPONSE_BYTES=65536,
        OUTBOUND_PROBE_BODY_BYTES=1024,
        OUTBOUND_USER_AGENT="FetchNow-Test/1.0",
        OUTBOUND_ALLOWED_CONTENT_TYPES="text/html,application/json,text/plain",
        WRAPPER_RESOLUTION_MAX_DEPTH=3,
    )


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(settings)
    dns = FakeDnsResolver(
        records={
            "vk.com": ["8.8.8.8"],
            "vk.ru": ["8.8.8.8"],
            "www.vk.ru": ["8.8.8.8"],
            "m.vk.ru": ["8.8.8.8"],
            "vkvideo.ru": ["8.8.8.8"],
            "rutube.ru": ["8.8.4.4"],
            "yandex.ru": ["1.1.1.1"],
        },
        default_addresses=("8.8.8.8",),
    )
    providers = ProviderRegistry.from_settings(settings)
    validator = URLValidator(settings, registry=providers, resolver=dns)
    wrappers = build_wrapper_registry(settings, providers)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.host}{request.url.path}")
        if request.url.host == "yandex.ru":
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=REGRESSION_HTML,
            )
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"ok")

    http = SafeHTTPClient(
        settings,
        validator=validator,
        resolver=dns,
        transport=httpx.MockTransport(handler),
    )
    resolution = ResolutionService(
        settings,
        provider_registry=providers,
        wrapper_registry=wrappers,
        validator=validator,
        http_client=http,
        dns_resolver=dns,
    )
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as ac,
        app.router.lifespan_context(app),
    ):
        app.state.dns_resolver = dns
        app.state.url_validator = validator
        app.state.safe_http_client = http
        app.state.provider_registry = providers
        app.state.wrapper_registry = wrappers
        app.state.resolution_service = resolution
        ac._fetchnow_calls = calls  # type: ignore[attr-defined]
        ac._fetchnow_app = app  # type: ignore[attr-defined]
        ac._fetchnow_dns = dns  # type: ignore[attr-defined]
        ac._fetchnow_http = http  # type: ignore[attr-defined]
        yield ac
    await http.aclose()


@pytest.mark.asyncio
async def test_resolve_direct_vk_no_wrapper_fetch(client: AsyncClient) -> None:
    calls: list[str] = client._fetchnow_calls  # type: ignore[attr-defined]
    calls.clear()
    response = await client.post(
        "/api/v1/media/resolve",
        json={"url": f"https://vk.com/video-1?{SECRET}=1"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["provider"]["id"] == "vk"
    assert body["url"]["canonical"] == "https://vk.com/video-1"
    assert body["provenance"] == "direct_provider"
    assert body["wrapperType"] is None
    assert body["resolutionChain"] == []
    assert SECRET not in response.text
    assert not any("yandex.ru" in c for c in calls)


@pytest.mark.asyncio
async def test_resolve_direct_vk_ru_clip_returns_video_canonical(
    client: AsyncClient,
) -> None:
    calls: list[str] = client._fetchnow_calls  # type: ignore[attr-defined]
    calls.clear()
    response = await client.post(
        "/api/v1/media/resolve",
        json={"url": "https://vk.ru/clip-235548483_456239236?list=secret"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["provider"]["id"] == "vk"
    assert body["provenance"] == "direct_provider"
    assert body["wrapperType"] is None
    assert body["url"]["hostname"] == "vk.ru"
    assert body["url"]["path"] == "/video-235548483_456239236"
    assert body["url"]["canonical"] == (
        "https://vk.ru/video-235548483_456239236"
    )
    assert "clip" not in body["url"]["path"]
    assert "clip" not in body["url"]["canonical"]
    assert "?" not in body["url"]["canonical"]
    assert "#" not in body["url"]["canonical"]
    assert "list=" not in response.text
    assert "secret" not in response.text
    assert body["resolutionChain"] == []
    assert not any("yandex.ru" in c for c in calls)
    # Frontend contract: backend /video… result is acceptable.
    assert body["url"]["canonical"].startswith("https://vk.ru/video")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "path", "canonical"),
    [
        (
            "https://rutube.ru/video/abc123",
            "/video/abc123/",
            "https://rutube.ru/video/abc123/",
        ),
        (
            "https://rutube.ru/video/abc123/",
            "/video/abc123/",
            "https://rutube.ru/video/abc123/",
        ),
        (
            "https://rutube.ru/shorts/3eac3b4561676c17df9132a9a1e62e3e/",
            "/video/3eac3b4561676c17df9132a9a1e62e3e/",
            "https://rutube.ru/video/3eac3b4561676c17df9132a9a1e62e3e/",
        ),
    ],
)
async def test_resolve_direct_rutube_canonicalizes_video_and_shorts(
    client: AsyncClient,
    raw: str,
    path: str,
    canonical: str,
) -> None:
    calls: list[str] = client._fetchnow_calls  # type: ignore[attr-defined]
    calls.clear()
    response = await client.post("/api/v1/media/resolve", json={"url": raw})
    assert response.status_code == 200
    body = response.json()
    assert body["provider"]["id"] == "rutube"
    assert body["provenance"] == "direct_provider"
    assert body["url"]["path"] == path
    assert body["url"]["canonical"] == canonical
    assert "?" not in body["url"]["canonical"]
    assert "/shorts/" not in body["url"]["canonical"]
    assert not any("yandex.ru" in c for c in calls)


@pytest.mark.asyncio
async def test_resolve_yandex_to_vkvideo(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/media/resolve",
        json={"url": f"{SUBMITTED_URL}?{SECRET}=1"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["provider"]["id"] == "vk"
    assert body["url"]["canonical"] == EXPECTED_CANONICAL
    assert body["provenance"] == "wrapper_resolved"
    assert body["wrapperType"] == "yandex_video_preview"
    assert len(body["resolutionChain"]) == 1
    hop = body["resolutionChain"][0]
    assert hop["resolverId"] == "yandex_video_preview"
    assert hop["strategy"] == "yandex_viewer_iframe_video_url"
    assert "metadata" not in hop
    assert SECRET not in response.text
    assert "video_ext.php" not in body["url"]["canonical"]
    assert "hash=" not in body["url"]["canonical"]
    assert "yastatic.net" not in response.text
    assert EMBED_DECOY not in response.text
    assert VOLATILE_MARKER not in response.text


@pytest.mark.asyncio
async def test_resolve_wrapper_unsupported(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/media/resolve",
        json={"url": "https://unknown.example/video"},
    )
    assert response.status_code == 422
    err = response.json()["error"]
    assert err["code"] == "WRAPPER_UNSUPPORTED"
    assert "internal_reason" not in err


@pytest.mark.asyncio
async def test_resolve_error_mapping_unresolved(settings: Settings) -> None:
    app = create_app(settings)
    dns = FakeDnsResolver(records={"yandex.ru": ["1.1.1.1"]})
    providers = ProviderRegistry.from_settings(settings)
    validator = URLValidator(settings, registry=providers, resolver=dns)
    wrappers = build_wrapper_registry(settings, providers)

    def empty_page(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<html></html>",
        )

    empty_client = SafeHTTPClient(
        settings,
        validator=validator,
        resolver=dns,
        transport=httpx.MockTransport(empty_page),
    )
    service = ResolutionService(
        settings,
        provider_registry=providers,
        wrapper_registry=wrappers,
        validator=validator,
        http_client=empty_client,
        dns_resolver=dns,
    )
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as ac,
        app.router.lifespan_context(app),
    ):
        app.state.resolution_service = service
        app.state.url_validator = validator
        app.state.safe_http_client = empty_client
        response = await ac.post(
            "/api/v1/media/resolve",
            json={"url": "https://yandex.ru/video/preview/1"},
        )
    await empty_client.aclose()
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "WRAPPER_UNRESOLVED"


@pytest.mark.asyncio
async def test_validate_unchanged_for_yandex(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/media/validate",
        json={"url": SUBMITTED_URL},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_PROVIDER"


@pytest.mark.asyncio
async def test_shared_resolution_service_per_lifespan(settings: Settings) -> None:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        first = app.state.resolution_service
        second = app.state.resolution_service
        assert first is second
        assert app.state.safe_http_client is not None
        assert app.state.wrapper_registry is not None
        assert app.state.dns_resolver is not None
        assert app.state.url_validator._resolver is app.state.dns_resolver
        assert app.state.safe_http_client._resolver is app.state.dns_resolver
        assert app.state.resolution_service._safety._resolver is app.state.dns_resolver


@pytest.mark.asyncio
async def test_injected_fake_dns_shared_identity(client: AsyncClient) -> None:
    dns = client._fetchnow_dns  # type: ignore[attr-defined]
    http = client._fetchnow_http  # type: ignore[attr-defined]
    app = client._fetchnow_app  # type: ignore[attr-defined]
    assert app.state.dns_resolver is dns
    assert app.state.url_validator._resolver is dns
    assert http._resolver is dns
    assert app.state.resolution_service._safety._resolver is dns


@pytest.mark.asyncio
async def test_safe_http_client_closed_once_on_lifespan(settings: Settings) -> None:
    app = create_app(settings)
    closes = {"n": 0}
    async with app.router.lifespan_context(app):
        client = app.state.safe_http_client
        original = client.aclose

        async def tracked() -> None:
            closes["n"] += 1
            await original()

        client.aclose = tracked  # type: ignore[method-assign]
    assert closes["n"] == 1


@pytest.mark.asyncio
async def test_fallback_requires_shared_dns(settings: Settings) -> None:
    app = create_app(settings)
    scope = {
        "type": "http",
        "app": app,
        "method": "POST",
        "path": "/api/v1/media/resolve",
        "headers": [],
    }
    request = Request(scope)
    # No lifespan: dns_resolver absent → fail closed.
    with pytest.raises(RuntimeError, match="dns_resolver"):
        media_mod._get_dns_resolver(request)
