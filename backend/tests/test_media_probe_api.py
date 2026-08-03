"""API tests for POST /api/v1/media/probe."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from fetchnow.api.main import create_app
from fetchnow.core.config import Settings
from fetchnow.network.client import SafeHTTPClient
from fetchnow.url.dns import FakeDnsResolver
from fetchnow.url.providers import ProviderRegistry
from fetchnow.url.validate import URLValidator


@pytest.fixture
def settings() -> Settings:
    return Settings(
        APP_NAME="FetchNow",
        APP_ENV="test",
        LOG_LEVEL="INFO",
        DATABASE_URL="postgresql+asyncpg://fetchnow:fetchnow@localhost:5432/fetchnow",
        URL_ALLOWED_SCHEMES="http,https",
        URL_ALLOWED_PORTS="80,443",
        URL_MAX_REDIRECTS=5,
        PROVIDER_VK_ENABLED=True,
        PROVIDER_RUTUBE_ENABLED=True,
        OUTBOUND_MAX_RESPONSE_BYTES=1024,
        OUTBOUND_PROBE_BODY_BYTES=1024,
        OUTBOUND_ALLOWED_CONTENT_TYPES="text/html,application/json,text/plain",
        OUTBOUND_CONNECT_TIMEOUT_SECONDS=2,
        OUTBOUND_READ_TIMEOUT_SECONDS=2,
        OUTBOUND_TOTAL_TIMEOUT_SECONDS=5,
    )


def _routes() -> dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]]:
    return {
        ("HEAD", "https://vk.com/video-123_456"): lambda r: httpx.Response(
            200, headers={"content-type": "text/html", "content-length": "10"}
        ),
        ("HEAD", "https://vk.com/feed"): lambda r: httpx.Response(418),
        ("GET", "https://vk.com/feed"): lambda r: httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"<html>ok</html>"
        ),
        ("HEAD", "https://rutube.ru/video/abcdef0123456789/"): lambda r: httpx.Response(
            200, headers={"content-type": "text/html"}
        ),
        ("HEAD", "https://vk.com/redir"): lambda r: httpx.Response(
            302, headers={"Location": "https://vk.com/final"}
        ),
        ("HEAD", "https://vk.com/final"): lambda r: httpx.Response(
            200, headers={"content-type": "text/html"}
        ),
        ("HEAD", "https://vk.com/to-local"): lambda r: httpx.Response(
            302, headers={"Location": "https://127.0.0.1/x"}
        ),
        ("HEAD", "https://vk.com/loop-a"): lambda r: httpx.Response(
            302, headers={"Location": "https://vk.com/loop-b"}
        ),
        ("HEAD", "https://vk.com/loop-b"): lambda r: httpx.Response(
            302, headers={"Location": "https://vk.com/loop-a"}
        ),
        ("HEAD", "https://vk.com/big"): lambda r: httpx.Response(405),
        ("GET", "https://vk.com/big"): lambda r: httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=b"x" * 5000,
        ),
        ("HEAD", "https://vk.com/video-mp4"): lambda r: httpx.Response(405),
        ("GET", "https://vk.com/video-mp4"): lambda r: httpx.Response(
            200,
            headers={"content-type": "video/mp4"},
            content=b"data",
        ),
    }


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(settings)
    resolver = FakeDnsResolver(default_addresses=("8.8.8.8",))
    validator = URLValidator(
        settings,
        registry=ProviderRegistry.from_settings(settings),
        resolver=resolver,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        # Match host+path without query so secret tokens do not affect routing.
        bare = f"{request.url.scheme}://{request.url.host}{request.url.path}"
        key = (request.method.upper(), bare)
        routes = _routes()
        if key in routes:
            return routes[key](request)
        return httpx.Response(404)

    http = SafeHTTPClient(
        settings,
        validator=validator,
        resolver=resolver,
        transport=httpx.MockTransport(handler),
    )
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as ac,
        app.router.lifespan_context(app),
    ):
        app.state.url_validator = validator
        app.state.safe_http_client = http
        yield ac
    await http.aclose()


@pytest.mark.asyncio
async def test_probe_vk_success(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/media/probe",
        json={"url": "https://vk.com/video-123_456"},
        headers={"X-Request-ID": "probe-1"},
    )
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "probe-1"
    body = response.json()
    assert body["provider"]["id"] == "vk"
    assert body["finalUrl"]["hostname"] == "vk.com"
    assert body["http"]["status"] == 200
    assert body["http"]["contentType"] == "text/html"
    assert body["http"]["redirectCount"] == 0
    assert body["http"]["methodUsed"] == "HEAD"


@pytest.mark.asyncio
async def test_probe_rutube_success(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/media/probe",
        json={"url": "https://rutube.ru/video/abcdef0123456789/"},
    )
    assert response.status_code == 200
    assert response.json()["provider"]["id"] == "rutube"


@pytest.mark.asyncio
async def test_probe_vk_418_fallback_method_used(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/media/probe",
        json={"url": "https://vk.com/feed"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["http"]["methodUsed"] == "GET"
    assert body["http"]["status"] == 200
    assert "body" not in body


@pytest.mark.asyncio
async def test_probe_allowed_redirect(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/media/probe",
        json={"url": "https://vk.com/redir"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["finalUrl"]["path"] == "/final"
    assert body["http"]["redirectCount"] == 1


@pytest.mark.asyncio
async def test_probe_redirect_localhost(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/media/probe",
        json={"url": "https://vk.com/to-local"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "BLOCKED_DESTINATION"


@pytest.mark.asyncio
async def test_probe_redirect_loop(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/media/probe",
        json={"url": "https://vk.com/loop-a"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REDIRECT_LIMIT_EXCEEDED"


@pytest.mark.asyncio
async def test_probe_oversized_response(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/media/probe",
        json={"url": "https://vk.com/big"},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "RESPONSE_TOO_LARGE"


@pytest.mark.asyncio
async def test_probe_unsupported_mime(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/media/probe",
        json={"url": "https://vk.com/video-mp4"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_CONTENT_TYPE"


@pytest.mark.asyncio
async def test_probe_secret_query_not_logged(
    client: AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "probe-api-secret-token"
    with caplog.at_level(logging.INFO):
        response = await client.post(
            "/api/v1/media/probe",
            json={"url": f"https://vk.com/video-123_456?access_token={secret}"},
            headers={"X-Request-ID": "probe-secret"},
        )
    assert response.status_code == 200
    assert secret not in response.text
    blob = "\n".join(f"{r.getMessage()} {r.__dict__!s}" for r in caplog.records)
    assert secret not in blob
