"""Offline API tests for browser grant issuance and native content delivery."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from fetchnow.api.main import create_app
from fetchnow.core.config import Settings
from fetchnow.delivery.main import create_delivery_app
from fetchnow.delivery.reader import ArtifactReader
from fetchnow.delivery.service import DeliveryAuthorization, DeliveryService
from fetchnow.downloads.artifacts import MIN_ORPHAN_GRACE_SECONDS, ArtifactStore
from fetchnow.downloads.browser_grant_tokens import (
    COOKIE_NAME,
    generate_grant_token,
)
from fetchnow.downloads.errors import DownloadError, DownloadErrorCode
from fetchnow.downloads.grant_service import BrowserGrantService, IssuedBrowserGrant
from fetchnow.downloads.models import MediaDownloadJob
from fetchnow.jobs.credentials import generate_access_token
from fetchnow.network.client import SafeHTTPClient
from fetchnow.resolution.defaults import build_wrapper_registry
from fetchnow.resolution.service import ResolutionService
from fetchnow.url.dns import FakeDnsResolver
from fetchnow.url.providers import ProviderRegistry
from fetchnow.url.validate import URLValidator

_DB = "postgresql+asyncpg://fetchnow:fetchnow@localhost:5432/fetchnow"


def _api_settings(**overrides: Any) -> Settings:
    kwargs: dict[str, Any] = {
        "APP_ENV": "test",
        "LOG_LEVEL": "WARNING",
        "DATABASE_URL": _DB,
        "MEDIA_DOWNLOADS_ENABLED": True,
        "MEDIA_BROWSER_DELIVERY_ENABLED": True,
        "PROVIDER_VK_ENABLED": True,
        "PROVIDER_RUTUBE_ENABLED": True,
    }
    kwargs.update(overrides)
    return Settings(**kwargs)


async def _build_api_client(settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(settings)
    dns = FakeDnsResolver(
        records={"vk.com": ["8.8.8.8"]},
        default_addresses=("8.8.8.8",),
    )
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
    session_factory = MagicMock(return_value=session)

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
        app.state.session_factory = session_factory
        yield ac
    await http.aclose()


@pytest.mark.asyncio
async def test_issue_grant_sets_cookie_and_clean_path() -> None:
    settings = _api_settings()
    grant_id = uuid.uuid4()
    raw = generate_grant_token()
    expires = datetime.now(tz=UTC) + timedelta(minutes=5)
    issued = IssuedBrowserGrant(
        grant_id=grant_id,
        download_path=f"/api/v1/media/browser-grants/{grant_id}/content",
        expires_at=expires,
        raw_token=raw,
        cookie_max_age=300,
    )

    class _Stub(BrowserGrantService):
        async def issue(self, **_kwargs: Any) -> IssuedBrowserGrant:
            return issued

    app = create_app(settings)
    dns = FakeDnsResolver(
        records={"vk.com": ["8.8.8.8"]},
        default_addresses=("8.8.8.8",),
    )
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
    session_factory = MagicMock(return_value=session)

    transport = ASGITransport(app=app)
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
        app.state.session_factory = session_factory
        app.state.browser_grant_service = _Stub(settings)
        token = generate_access_token()
        response = await client.post(
            f"/api/v1/media/download-jobs/{uuid.uuid4()}/browser-grants",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 201
        body = response.json()
        assert set(body.keys()) == {"downloadPath", "expiresAt"}
        assert body["downloadPath"] == issued.download_path
        assert "token" not in body
        assert raw not in response.text
        assert token not in response.text
        cookie = response.headers.get("set-cookie", "")
        assert cookie.startswith(f"{COOKIE_NAME}={raw};")
        assert "HttpOnly" in cookie
        assert "Secure" in cookie
        assert "SameSite=Strict" in cookie
        assert f"Path={issued.download_path}" in cookie
        assert "Domain=" not in cookie
        assert response.headers.get("cache-control") == "no-store"
    await http.aclose()


@pytest.mark.asyncio
async def test_issue_missing_bearer_indistinguishable() -> None:
    settings = _api_settings()
    async for client in _build_api_client(settings):
        response = await client.post(
            f"/api/v1/media/download-jobs/{uuid.uuid4()}/browser-grants"
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "DOWNLOAD_JOB_NOT_FOUND"
        break


@pytest.mark.asyncio
async def test_issue_disabled_catalog() -> None:
    settings = _api_settings(MEDIA_BROWSER_DELIVERY_ENABLED=False)

    class _Stub(BrowserGrantService):
        async def issue(self, **_kwargs: Any) -> IssuedBrowserGrant:
            raise DownloadError(DownloadErrorCode.DELIVERY_DISABLED)

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
        app.state.browser_grant_service = _Stub(settings)
        response = await client.post(
            f"/api/v1/media/download-jobs/{uuid.uuid4()}/browser-grants",
            headers={"Authorization": f"Bearer {generate_access_token()}"},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "DELIVERY_DISABLED"
    await http.aclose()


def _prepare_root(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _publish(
    root: Path, payload: bytes = b"native-grant-bytes-012345"
) -> dict[str, Any]:
    store = ArtifactStore(
        root=str(root),
        max_bytes=1024 * 1024,
        orphan_grace_seconds=MIN_ORPHAN_GRACE_SECONDS,
    )
    job_id = uuid.uuid4()
    workspace = store.create_attempt_workspace(job_id=job_id, attempt=1, fence=2)
    (workspace.output / "artifact.mp4").write_bytes(payload)
    expires = datetime.now(tz=UTC) + timedelta(hours=1)
    published = store.publish(
        workspace=workspace,
        expected_container="mp4",
        job_id=job_id,
        format_option_id="fmt_x",
        expires_at=expires,
    )
    return {
        "job_id": job_id,
        "artifact_id": published.artifact_id,
        "expires": expires,
        "fence": 2,
        "bytes": len(payload),
        "content_type": published.content_type,
        "payload": payload,
    }


def _job_row(meta: dict[str, Any]) -> MediaDownloadJob:
    now = datetime.now(tz=UTC)
    return MediaDownloadJob(
        id=meta["job_id"],
        media_job_id=uuid.uuid4(),
        schema_version=1,
        public_state="ready",
        format_option_id="fmt_x",
        provider_id="vk",
        canonical_provider_url="https://vk.com/video-1_2",
        media_id="-1_2",
        hostname="vk.com",
        path="/video-1_2",
        scheme="https",
        port=None,
        selected_format_snapshot={"formatOptionId": "fmt_x"},
        attempt_count=1,
        max_attempts=3,
        available_at=now,
        fence_token=meta["fence"],
        artifact_id=meta["artifact_id"],
        artifact_bytes=meta["bytes"],
        artifact_content_type=meta["content_type"],
        artifact_container="mp4",
        suggested_filename="clip.mp4",
        public_error_code=None,
        created_at=now,
        updated_at=now,
        completed_at=now,
        expires_at=meta["expires"],
        progress_stage="ready",
    )


@pytest.fixture
async def grant_delivery_client(tmp_path: Path):
    root = _prepare_root(tmp_path / "root")
    meta = _publish(root)
    grant_id = uuid.uuid4()
    settings = Settings(
        APP_ENV="test",
        DATABASE_URL=_DB,
        MEDIA_DELIVERY_ENABLED=True,
        MEDIA_BROWSER_DELIVERY_ENABLED=True,
        MEDIA_DELIVERY_ROOT=str(root),
        MEDIA_DELIVERY_CHUNK_BYTES=4096,
        MEDIA_DELIVERY_CONCURRENCY=2,
        MEDIA_DELIVERY_RANGE_ENABLED=True,
    )
    app = create_delivery_app(settings)
    reader = ArtifactReader(str(root))
    raw_token = generate_grant_token()

    class _StubDelivery(DeliveryService):
        async def authorize_browser_grant(
            self, **_kwargs: Any
        ) -> DeliveryAuthorization:
            return DeliveryAuthorization(
                job=_job_row(meta), now=datetime.now(tz=UTC)
            )

    service: DeliveryService = _StubDelivery(settings, reader=reader)

    class _FakeSession:
        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    app.state.engine = MagicMock()
    app.state.session_factory = lambda: _FakeSession()
    app.state.settings = settings
    app.state.delivery_service = service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, meta, grant_id, raw_token, service


@pytest.mark.asyncio
async def test_native_get_with_cookie_streams(grant_delivery_client: Any) -> None:
    client, meta, grant_id, raw_token, _service = grant_delivery_client
    response = await client.get(
        f"/api/v1/media/browser-grants/{grant_id}/content",
        headers={"Cookie": f"{COOKIE_NAME}={raw_token}"},
    )
    assert response.status_code == 200
    assert response.content == meta["payload"]
    assert "attachment;" in response.headers["content-disposition"]
    assert "filename*=UTF-8''clip.mp4" in response.headers["content-disposition"]
    assert raw_token not in response.text
    assert str(meta["artifact_id"]) not in response.text


@pytest.mark.asyncio
async def test_native_uuid_alone_rejected(grant_delivery_client: Any) -> None:
    client, _meta, grant_id, _raw, _service = grant_delivery_client
    response = await client.get(f"/api/v1/media/browser-grants/{grant_id}/content")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DOWNLOAD_JOB_NOT_FOUND"
    assert b"native-grant-bytes" not in response.content
    assert "error" in response.text


@pytest.mark.asyncio
async def test_native_duplicate_cookie_rejected(grant_delivery_client: Any) -> None:
    client, _meta, grant_id, raw_token, _service = grant_delivery_client
    response = await client.get(
        f"/api/v1/media/browser-grants/{grant_id}/content",
        headers={
            "Cookie": f"{COOKIE_NAME}={raw_token}; {COOKIE_NAME}={raw_token}"
        },
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DOWNLOAD_JOB_NOT_FOUND"


@pytest.mark.asyncio
async def test_native_head_no_body(grant_delivery_client: Any) -> None:
    client, meta, grant_id, raw_token, _service = grant_delivery_client
    response = await client.head(
        f"/api/v1/media/browser-grants/{grant_id}/content",
        headers={"Cookie": f"{COOKIE_NAME}={raw_token}"},
    )
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-length"] == str(meta["bytes"])


@pytest.mark.asyncio
async def test_native_range_requires_grant(grant_delivery_client: Any) -> None:
    client, meta, grant_id, raw_token, _service = grant_delivery_client
    denied = await client.get(
        f"/api/v1/media/browser-grants/{grant_id}/content",
        headers={"Range": "bytes=0-3"},
    )
    assert denied.status_code == 404
    assert denied.content == b"" or "error" in denied.text

    ok = await client.get(
        f"/api/v1/media/browser-grants/{grant_id}/content",
        headers={
            "Cookie": f"{COOKIE_NAME}={raw_token}",
            "Range": "bytes=0-3",
        },
    )
    assert ok.status_code == 206
    assert ok.content == meta["payload"][:4]


@pytest.mark.asyncio
async def test_native_query_string_rejected(grant_delivery_client: Any) -> None:
    client, _meta, grant_id, raw_token, _service = grant_delivery_client
    response = await client.get(
        f"/api/v1/media/browser-grants/{grant_id}/content",
        params={"token": raw_token, "accessToken": "x"},
        headers={"Cookie": f"{COOKIE_NAME}={raw_token}"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DOWNLOAD_JOB_NOT_FOUND"
    assert "token" not in response.text
    assert raw_token not in response.text


@pytest.mark.asyncio
async def test_bearer_delivery_still_works(tmp_path: Path) -> None:
    """PR7 Bearer path must not regress when browser grants exist."""
    root = _prepare_root(tmp_path / "root")
    meta = _publish(root)
    settings = Settings(
        APP_ENV="test",
        DATABASE_URL=_DB,
        MEDIA_DELIVERY_ENABLED=True,
        MEDIA_BROWSER_DELIVERY_ENABLED=True,
        MEDIA_DELIVERY_ROOT=str(root),
        MEDIA_DELIVERY_CHUNK_BYTES=4096,
        MEDIA_DELIVERY_CONCURRENCY=2,
    )
    app = create_delivery_app(settings)
    reader = ArtifactReader(str(root))

    class _Stub(DeliveryService):
        async def authorize(self, **_kwargs: Any) -> DeliveryAuthorization:
            return DeliveryAuthorization(
                job=_job_row(meta), now=datetime.now(tz=UTC)
            )

    class _FakeSession:
        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    app.state.engine = MagicMock()
    app.state.session_factory = lambda: _FakeSession()
    app.state.settings = settings
    app.state.delivery_service = _Stub(settings, reader=reader)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/api/v1/media/download-jobs/{meta['job_id']}/content",
            headers={"Authorization": f"Bearer {generate_access_token()}"},
        )
    assert response.status_code == 200
    assert response.content == meta["payload"]
