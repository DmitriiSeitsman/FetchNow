"""API tests for POST /api/v1/media/validate."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from fetchnow.api.main import create_app
from fetchnow.core.config import Settings
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
        PROVIDER_VK_ENABLED=True,
        PROVIDER_RUTUBE_ENABLED=True,
    )


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(settings)
    resolver = FakeDnsResolver(default_addresses=("8.8.8.8",))
    validator = URLValidator(
        settings,
        registry=ProviderRegistry.from_settings(settings),
        resolver=resolver,
    )
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as ac,
        app.router.lifespan_context(app),
    ):
        app.state.url_validator = validator
        yield ac


@pytest.mark.asyncio
async def test_validate_vk_success(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/media/validate",
        json={"url": "https://vk.com/video-123_456"},
        headers={"X-Request-ID": "req-validate-1"},
    )
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req-validate-1"
    body = response.json()
    assert body["provider"]["id"] == "vk"
    assert body["provider"]["displayName"] == "VK"
    assert body["url"]["hostname"] == "vk.com"
    assert body["url"]["scheme"] == "https"
    assert body["url"]["port"] is None
    assert "canonical" in body["url"]
    assert "?" not in body["url"]["canonical"]


@pytest.mark.asyncio
async def test_validate_rutube_success(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/media/validate",
        json={"url": "https://rutube.ru/video/abcdef0123456789/"},
    )
    assert response.status_code == 200
    assert response.json()["provider"]["id"] == "rutube"


@pytest.mark.asyncio
async def test_validate_unknown_provider_envelope(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/media/validate",
        json={"url": "https://example.com/video.mp4"},
        headers={"X-Request-ID": "req-unknown"},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "UNSUPPORTED_PROVIDER"
    assert body["error"]["request_id"] == "req-unknown"
    assert "example.com" not in body["error"]["message"]


@pytest.mark.asyncio
async def test_validate_localhost(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/media/validate",
        json={"url": "https://localhost/video"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "BLOCKED_DESTINATION"


@pytest.mark.asyncio
async def test_validate_deceptive_hostname(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/media/validate",
        json={"url": "https://vk.com.attacker.example/video"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_PROVIDER"


@pytest.mark.asyncio
async def test_validate_credentials(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/media/validate",
        json={"url": "https://user:pass@vk.com/video-1"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_URL"


@pytest.mark.asyncio
async def test_validate_secret_query_not_logged(
    client: AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "logging-secret-token-9f3a"
    with caplog.at_level(logging.INFO):
        response = await client.post(
            "/api/v1/media/validate",
            json={"url": f"https://vk.com/video-1?access_token={secret}"},
            headers={"X-Request-ID": "req-log-1"},
        )
    assert response.status_code == 200
    assert secret not in response.text
    blob = "\n".join(
        f"{record.getMessage()} {record.__dict__!s}" for record in caplog.records
    )
    assert secret not in blob
    assert f"access_token={secret}" not in blob
