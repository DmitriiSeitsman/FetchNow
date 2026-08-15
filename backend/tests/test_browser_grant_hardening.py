"""Hardening tests for PR14 browser grants (UUID, logging, read-only auth)."""

from __future__ import annotations

import logging
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from fetchnow.api.main import create_app
from fetchnow.core.config import Settings
from fetchnow.delivery.main import create_delivery_app
from fetchnow.delivery.service import DeliveryService
from fetchnow.downloads.browser_grant_tokens import generate_grant_token
from fetchnow.downloads.canonical_uuid import (
    CANONICAL_UUID4_RE,
    NGINX_CANONICAL_UUID4,
    parse_canonical_uuid4,
)
from fetchnow.downloads.errors import DownloadError, DownloadErrorCode
from fetchnow.downloads.grant_service import BrowserGrantService, IssuedBrowserGrant
from fetchnow.jobs.credentials import generate_access_token
from fetchnow.network.client import SafeHTTPClient
from fetchnow.resolution.defaults import build_wrapper_registry
from fetchnow.resolution.service import ResolutionService
from fetchnow.url.dns import FakeDnsResolver
from fetchnow.url.providers import ProviderRegistry
from fetchnow.url.validate import URLValidator

_DB = "postgresql+asyncpg://fetchnow:fetchnow@localhost:5432/fetchnow"


def test_canonical_uuid4_accepts_uuid4_only() -> None:
    good = str(uuid.uuid4())
    assert CANONICAL_UUID4_RE.fullmatch(good)
    assert parse_canonical_uuid4(good) == uuid.UUID(good)
    assert NGINX_CANONICAL_UUID4 == (
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "BBBBBBBB-BBBB-4CCC-8DDD-EEEEEEEEEEEE",
        "bbbbbbbb-bbbb-5ccc-8ddd-eeeeeeeeeeee",
        "bbbbbbbb-bbbb-4ccc-cddd-eeeeeeeeeeee",
        "bbbbbbbbbbbb4ccc8dddeeeeeeeeeeee",
        "not-a-uuid",
        "bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee/extra",
    ],
)
def test_canonical_uuid4_rejects_lookalikes(raw: str) -> None:
    with pytest.raises(DownloadError) as exc:
        parse_canonical_uuid4(raw)
    assert exc.value.code is DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND
    blob = f"{exc.value!s}{exc.value!r}{exc.value.message}"
    assert raw not in blob


@pytest.mark.asyncio
async def test_malformed_grant_uuid_never_queries_db_or_returns_422(
    tmp_path: Any,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings(
        APP_ENV="test",
        DATABASE_URL="postgresql+asyncpg://unused@127.0.0.1:5432/unused",
        MEDIA_DELIVERY_ENABLED=True,
        MEDIA_BROWSER_DELIVERY_ENABLED=True,
        MEDIA_DELIVERY_ROOT=str(root),
    )
    app = create_delivery_app(settings)

    class _FakeSession:
        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    queried = {"count": 0}

    def _factory() -> _FakeSession:
        queried["count"] += 1
        return _FakeSession()

    app.state.engine = MagicMock()
    app.state.session_factory = _factory
    app.state.settings = settings
    app.state.delivery_service = DeliveryService(settings, reader=MagicMock())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for path in (
            "/api/v1/media/browser-grants/NOT-A-UUID/content",
            "/api/v1/media/browser-grants/BBBBBBBB-BBBB-4CCC-8DDD-EEEEEEEEEEEE/content",
            "/api/v1/media/browser-grants/bbbbbbbb-bbbb-5ccc-8ddd-eeeeeeeeeeee/content",
            "/api/v1/media/browser-grants/bbbbbbbb-bbbb-4ccc-cddd-eeeeeeeeeeee/content",
        ):
            response = await client.get(path)
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "DOWNLOAD_JOB_NOT_FOUND"
            assert response.status_code != 422
    assert queried["count"] == 0


@pytest.mark.asyncio
async def test_query_string_rejected_before_side_effects(
    tmp_path: Any,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings(
        APP_ENV="test",
        DATABASE_URL="postgresql+asyncpg://unused@127.0.0.1:5432/unused",
        MEDIA_DELIVERY_ENABLED=True,
        MEDIA_BROWSER_DELIVERY_ENABLED=True,
        MEDIA_DELIVERY_ROOT=str(root),
        MEDIA_DELIVERY_CONCURRENCY=2,
    )
    app = create_delivery_app(settings)
    grant_id = str(uuid.uuid4())
    sentinel = "QUERY_SECRET_SENTINEL_9f3a"

    sessions = {"count": 0}
    acquires = {"count": 0}
    opens = {"count": 0}

    class _FakeSession:
        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    def _factory() -> _FakeSession:
        sessions["count"] += 1
        return _FakeSession()

    class _Tracking(DeliveryService):
        def open_artifact(self, *args: object, **kwargs: object) -> object:
            opens["count"] += 1
            return super().open_artifact(*args, **kwargs)  # type: ignore[misc]

    service = _Tracking(settings, reader=MagicMock())
    inner_acquire = service.semaphore.acquire

    async def _counting_acquire(*args: object, **kwargs: object) -> None:
        acquires["count"] += 1
        await inner_acquire(*args, **kwargs)

    service.semaphore.acquire = _counting_acquire  # type: ignore[method-assign]

    app.state.engine = MagicMock()
    app.state.session_factory = _factory
    app.state.settings = settings
    app.state.delivery_service = service

    target = logging.getLogger("fetchnow.delivery.routes")
    captured: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    handler = _Capture()
    target.addHandler(handler)
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            get_resp = await client.get(
                f"/api/v1/media/browser-grants/{grant_id}/content",
                params={"token": sentinel},
            )
            head_resp = await client.head(
                f"/api/v1/media/browser-grants/{grant_id}/content",
                params={"token": sentinel},
            )
    finally:
        target.removeHandler(handler)

    assert get_resp.status_code == 404
    assert head_resp.status_code == 404
    assert get_resp.status_code != 422
    assert get_resp.json()["error"]["code"] == "DOWNLOAD_JOB_NOT_FOUND"
    assert sessions["count"] == 0
    assert acquires["count"] == 0
    assert opens["count"] == 0
    assert sentinel not in get_resp.text
    assert sentinel not in head_resp.text
    assert sentinel not in " ".join(captured)
    assert all("token=" not in m for m in captured)


@pytest.mark.asyncio
async def test_grant_insert_failure_never_logs_secrets() -> None:
    settings = Settings(
        APP_ENV="test",
        LOG_LEVEL="INFO",
        DATABASE_URL=_DB,
        MEDIA_DOWNLOADS_ENABLED=True,
        MEDIA_BROWSER_DELIVERY_ENABLED=True,
        PROVIDER_VK_ENABLED=True,
        PROVIDER_RUTUBE_ENABLED=True,
    )
    sentinel_token = generate_grant_token()
    sentinel_hash = "deadbeef" * 8
    sentinel_bearer = generate_access_token()
    sentinel_uuid = str(uuid.uuid4())
    sentinel_sql = (
        f"INSERT INTO media_delivery_grants (token_hash) VALUES "
        f"(%({sentinel_hash})s) -- {sentinel_token} {sentinel_bearer} "
        f"{sentinel_uuid} https://evil.example/?token=x"
    )

    class _Boom(BrowserGrantService):
        async def issue(self, **_kwargs: Any) -> IssuedBrowserGrant:
            raise RuntimeError(sentinel_sql)

    app = create_app(settings)
    dns = FakeDnsResolver(default_addresses=("8.8.8.8",))
    providers = ProviderRegistry.from_settings(settings)
    validator = URLValidator(settings, registry=providers, resolver=dns)
    wrappers = build_wrapper_registry(settings, providers)
    http = SafeHTTPClient(settings, validator=validator, resolver=dns)
    resolution = ResolutionService(
        settings,
        provider_registry=providers,
        wrapper_registry=wrappers,
        validator=validator,
        http_client=http,
        dns_resolver=dns,
    )
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.commit = AsyncMock()
    transport = ASGITransport(app=app)
    target = logging.getLogger("fetchnow.api.browser_grants")
    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _Capture()
    target.addHandler(handler)
    try:
        async with (
            AsyncClient(transport=transport, base_url="http://test") as client,
            app.router.lifespan_context(app),
        ):
            app.state.dns_resolver = dns
            app.state.url_validator = validator
            app.state.safe_http_client = http
            app.state.provider_registry = providers
            app.state.wrapper_registry = wrappers
            app.state.resolution_service = resolution
            app.state.session_factory = MagicMock(return_value=session)
            app.state.browser_grant_service = _Boom(settings)
            response = await client.post(
                f"/api/v1/media/download-jobs/{uuid.uuid4()}/browser-grants",
                headers={"Authorization": f"Bearer {sentinel_bearer}"},
            )
    finally:
        target.removeHandler(handler)
    await http.aclose()
    assert response.status_code == 500
    body = response.text
    messages = [r.getMessage() for r in captured]
    # Include formatter output / args / exc_text if any handler attached them.
    extras = []
    for r in captured:
        extras.append(repr(r.__dict__))
        if r.exc_info:
            extras.append(str(r.exc_info))
        if r.exc_text:
            extras.append(r.exc_text)
    log_blob = " ".join(messages + extras)
    joined = f"{body} {log_blob} {response!r}"
    for secret in (
        sentinel_token,
        sentinel_hash,
        sentinel_bearer,
        sentinel_uuid,
        "evil.example",
        "INSERT INTO",
    ):
        assert secret not in joined
    assert any(
        m == "browser_delivery_grant_failed outcome=internal" for m in messages
    )
    assert "token_hash" not in log_blob
    assert "token_hash" not in body


def test_nginx_conf_uses_exact_uuid4_grammar() -> None:
    from pathlib import Path

    text = (
        Path(__file__).resolve().parents[2] / "deploy" / "nginx" / "nginx.conf"
    ).read_text(encoding="utf-8")
    assert NGINX_CANONICAL_UUID4 in text
    grant_loc = text.split("browser-grants/", 1)[1].split("location /api/", 1)[0]
    assert "[0-9a-fA-F-]{36}" not in grant_loc
    assert "4[0-9a-f]{3}" in grant_loc
    assert 'if ($args != "")' in grant_loc
    assert "return 404;" in grant_loc
    assert "proxy_pass http://$fetchnow_delivery$uri;" in grant_loc
    assert "$request_uri" not in grant_loc
    assert "log_format browser_grant_delivery" in text
    fmt = text.split("log_format browser_grant_delivery", 1)[1].split(";", 1)[0]
    assert '"$request"' not in fmt
    assert "$request_uri" not in fmt
    assert "$args" not in fmt
    assert "$http_referer" not in fmt
    assert "$http_cookie" not in fmt
    assert "$http_authorization" not in fmt
    assert "$uri" in fmt
    assert "$request_method" in fmt
    # Global main format must remain unchanged for other routes.
    assert '"$request"' in text.split("log_format main", 1)[1].split(";", 1)[0]


@pytest.mark.asyncio
async def test_authorize_browser_grant_sql_has_no_for_update() -> None:
    settings = Settings(
        APP_ENV="test",
        DATABASE_URL="postgresql+asyncpg://unused@127.0.0.1:5432/unused",
        MEDIA_DELIVERY_ENABLED=True,
        MEDIA_BROWSER_DELIVERY_ENABLED=True,
        MEDIA_DELIVERY_ROOT="/tmp/unused",
    )
    service = DeliveryService(settings, reader=MagicMock())
    statements: list[str] = []

    class _Session:
        async def scalar(self, stmt: object) -> object | None:
            statements.append(str(stmt))
            return None

    with pytest.raises(DownloadError) as exc:
        await service.authorize_browser_grant(
            grant_id=uuid.uuid4(),
            raw_token=generate_grant_token(),
            session=_Session(),  # type: ignore[arg-type]
        )
    assert exc.value.code is DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND
    assert statements
    joined = " ".join(statements).upper()
    assert "FOR UPDATE" not in joined
