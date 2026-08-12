"""Malformed Bearer and pre-DB authorization gate tests."""

from __future__ import annotations

import logging
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from fetchnow.core.config import Settings
from fetchnow.delivery.main import create_delivery_app
from fetchnow.delivery.service import DeliveryService
from fetchnow.jobs.credentials import generate_access_token


def _app(*, enable_real_authorize: bool = False) -> Any:
    settings = Settings(
        APP_ENV="test",
        DATABASE_URL=(
            "postgresql+asyncpg://fetchnow:fetchnow@127.0.0.1:5432/fetchnow"
        ),
        MEDIA_DELIVERY_ENABLED=False,
    )
    app = create_delivery_app(settings)
    queried = {"opened": False}

    class _FakeSession:
        async def __aenter__(self) -> _FakeSession:
            queried["opened"] = True
            if not enable_real_authorize:
                raise AssertionError(
                    "database must not be opened for malformed tokens"
                )
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    class _Guard(DeliveryService):
        async def authorize(self, **kwargs: Any) -> Any:
            raise AssertionError("authorize must not run for malformed tokens")

    app.state.engine = MagicMock()
    app.state.session_factory = lambda: _FakeSession()
    app.state.settings = settings
    app.state.delivery_service = (
        DeliveryService(settings) if enable_real_authorize else _Guard(settings)
    )
    app.state._queried = queried
    return app


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "",
        "Bearer",
        "Bearer ",
        "Basic abc",
        "Bearer short",
        "Bearer " + ("a" * 42),
        "Bearer " + ("a" * 44),
        "Bearer " + ("*" * 43),
        "Bearer " + ("A" * 42) + "=",
        "Bearer " + ("/" * 43),
        "Bearer " + ("a" * 42) + "+",
        # Matches alphabet/length but unused padding bits → non-canonical.
        "Bearer " + ("A" * 42) + "B",
    ],
)
@pytest.mark.asyncio
async def test_malformed_bearer_is_404_without_db(
    authorization: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    app = _app()
    job_id = uuid.uuid4()
    transport = ASGITransport(app=app)
    headers: dict[str, str] = {}
    if authorization is not None:
        headers["Authorization"] = authorization
    with caplog.at_level(logging.ERROR):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/api/v1/media/download-jobs/{job_id}/content",
                headers=headers,
            )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DOWNLOAD_JOB_NOT_FOUND"
    assert app.state._queried["opened"] is False
    assert "media_delivery_failed" not in caplog.text
    if authorization and " " in authorization:
        token_part = authorization.split(None, 1)[-1].strip()
        if token_part:
            assert token_part not in response.text
            assert token_part not in caplog.text


@pytest.mark.asyncio
async def test_valid_token_when_disabled_returns_delivery_disabled() -> None:
    app = _app(enable_real_authorize=True)
    token = generate_access_token()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/api/v1/media/download-jobs/{uuid.uuid4()}/content",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DELIVERY_DISABLED"
    assert token not in response.text
    # Disabled path may open a session around authorize; that is intentional
    # for a syntactically valid token. Malformed cases never open one.
