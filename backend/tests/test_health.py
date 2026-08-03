"""Health endpoint tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from fetchnow.api.main import create_app
from fetchnow.core.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        APP_NAME="FetchNow",
        APP_ENV="test",
        LOG_LEVEL="WARNING",
        DATABASE_URL="postgresql+asyncpg://fetchnow:fetchnow@localhost:5432/fetchnow",
    )


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as ac,
        app.router.lifespan_context(app),
    ):
        yield ac


@pytest.mark.asyncio
async def test_liveness(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert "X-Request-ID" in response.headers


@pytest.mark.asyncio
async def test_liveness_preserves_request_id(client: AsyncClient) -> None:
    response = await client.get(
        "/api/v1/health/live",
        headers={"X-Request-ID": "test-request-id"},
    )
    assert response.headers["X-Request-ID"] == "test-request-id"


@pytest.mark.asyncio
async def test_readiness_ok(client: AsyncClient) -> None:
    with patch(
        "fetchnow.api.v1.health.check_database",
        new_callable=AsyncMock,
        return_value=True,
    ):
        response = await client.get("/api/v1/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_readiness_unavailable(client: AsyncClient) -> None:
    with patch(
        "fetchnow.api.v1.health.check_database",
        new_callable=AsyncMock,
        return_value=False,
    ):
        response = await client.get("/api/v1/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "not_ready"
    assert "request_id" in body["error"]


@pytest.mark.asyncio
async def test_not_found_error_envelope(client: AsyncClient) -> None:
    response = await client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    body = response.json()
    assert "error" in body
    assert body["error"]["code"] == "http_404"
    assert "request_id" in body["error"]
