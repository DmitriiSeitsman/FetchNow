"""FastAPI tests for media-download create/status endpoints (offline)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from fetchnow.api.main import create_app
from fetchnow.core.config import Settings
from fetchnow.downloads.errors import DownloadError, DownloadErrorCode
from fetchnow.downloads.models import MediaDownloadJob
from fetchnow.downloads.service import DownloadJobService, DownloadJobView
from fetchnow.jobs.credentials import generate_access_token
from fetchnow.network.client import SafeHTTPClient
from fetchnow.resolution.defaults import build_wrapper_registry
from fetchnow.resolution.service import ResolutionService
from fetchnow.url.dns import FakeDnsResolver
from fetchnow.url.providers import ProviderRegistry
from fetchnow.url.validate import URLValidator

_DB = "postgresql+asyncpg://fetchnow:fetchnow@localhost:5432/fetchnow"
_SECRET = "super-secret-access-token-value-xxxxxxxx"


async def _build_client(settings: Settings) -> AsyncIterator[AsyncClient]:
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
        app.state.download_job_service = DownloadJobService(settings)
        yield ac
    await http.aclose()


@pytest.fixture
async def disabled_client() -> AsyncIterator[AsyncClient]:
    settings = Settings(
        APP_ENV="test",
        LOG_LEVEL="WARNING",
        DATABASE_URL=_DB,
        MEDIA_DOWNLOADS_ENABLED=False,
        PROVIDER_VK_ENABLED=True,
        PROVIDER_RUTUBE_ENABLED=True,
    )
    async for client in _build_client(settings):
        yield client


@pytest.fixture
async def enabled_client() -> AsyncIterator[AsyncClient]:
    settings = Settings(
        APP_ENV="test",
        LOG_LEVEL="WARNING",
        DATABASE_URL=_DB,
        MEDIA_DOWNLOADS_ENABLED=True,
        PROVIDER_VK_ENABLED=True,
        PROVIDER_RUTUBE_ENABLED=True,
    )
    async for client in _build_client(settings):
        yield client


def _view(*, created: bool = True) -> DownloadJobView:
    now = datetime.now(tz=UTC)
    return DownloadJobView(
        id=uuid.uuid4(),
        media_job_id=uuid.uuid4(),
        public_state="queued",
        format_option_id="fmt_abc123",
        selected_format={
            "formatOptionId": "fmt_abc123",
            "container": "mp4",
            "qualityLabel": "p720",
            "freeTierEligible": True,
        },
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=1),
        completed_at=None,
        artifact_ready=False,
        error_code=None,
        created=created,
    )


@pytest.mark.asyncio
async def test_uuid_alone_without_bearer_is_404(
    enabled_client: AsyncClient,
) -> None:
    response = await enabled_client.get(
        f"/api/v1/media/download-jobs/{uuid.uuid4()}"
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DOWNLOAD_JOB_NOT_FOUND"
    assert response.headers.get("cache-control") == "no-store"


@pytest.mark.asyncio
async def test_disabled_returns_downloads_disabled(
    disabled_client: AsyncClient,
) -> None:
    token = generate_access_token()
    response = await disabled_client.post(
        f"/api/v1/media/jobs/{uuid.uuid4()}/downloads",
        json={"formatOptionId": "fmt_abc"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DOWNLOADS_DISABLED"
    assert response.headers.get("cache-control") == "no-store"
    assert token not in response.text


@pytest.mark.asyncio
async def test_create_response_has_no_path_token_url(
    enabled_client: AsyncClient,
) -> None:
    transport = enabled_client._transport
    assert isinstance(transport, ASGITransport)
    app = transport.app
    view = _view()
    token = generate_access_token()

    service = MagicMock()
    service.create = AsyncMock(return_value=view)
    service.to_public_dict = DownloadJobService(app.state.settings).to_public_dict
    app.state.download_job_service = service

    response = await enabled_client.post(
        f"/api/v1/media/jobs/{view.media_job_id}/downloads",
        json={"formatOptionId": "fmt_abc123"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 202
    assert response.headers.get("cache-control") == "no-store"
    body = response.json()
    blob = response.text.lower()
    assert "path" not in body
    assert "token" not in blob
    assert "http" not in blob
    assert "/var/" not in blob
    assert token not in response.text
    assert body["formatOptionId"] == "fmt_abc123"
    assert body["artifactReady"] is False


@pytest.mark.asyncio
async def test_format_ineligible_maps_422(
    enabled_client: AsyncClient,
) -> None:
    transport = enabled_client._transport
    assert isinstance(transport, ASGITransport)
    app = transport.app
    token = generate_access_token()

    service = MagicMock()
    service.create = AsyncMock(
        side_effect=DownloadError(
            DownloadErrorCode.FORMAT_NOT_ELIGIBLE,
            internal_reason="NOT_FREE_TIER",
        )
    )
    app.state.download_job_service = service

    response = await enabled_client.post(
        f"/api/v1/media/jobs/{uuid.uuid4()}/downloads",
        json={"formatOptionId": "fmt_hires"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "FORMAT_NOT_ELIGIBLE"
    assert response.headers.get("cache-control") == "no-store"
    assert _SECRET not in response.text


@pytest.mark.asyncio
async def test_malformed_uuid_has_no_store(
    enabled_client: AsyncClient,
) -> None:
    token = generate_access_token()
    response = await enabled_client.get(
        "/api/v1/media/download-jobs/not-a-uuid",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422
    assert response.headers.get("cache-control") == "no-store"


@pytest.mark.asyncio
async def test_validation_error_has_no_store(
    enabled_client: AsyncClient,
) -> None:
    token = generate_access_token()
    response = await enabled_client.post(
        f"/api/v1/media/jobs/{uuid.uuid4()}/downloads",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert response.headers.get("cache-control") == "no-store"


@pytest.mark.asyncio
async def test_auth_failure_has_no_store(
    enabled_client: AsyncClient,
) -> None:
    response = await enabled_client.get(
        f"/api/v1/media/download-jobs/{uuid.uuid4()}",
        headers={"Authorization": "Bearer"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DOWNLOAD_JOB_NOT_FOUND"
    assert response.headers.get("cache-control") == "no-store"


@pytest.mark.asyncio
async def test_get_expired_job_returns_expired_state(
    enabled_client: AsyncClient,
) -> None:
    """Authorized expired jobs return 200 with state=expired (not 410).

    Parent may still be wiring service.get to this contract; this asserts the
    API serialization once the service returns an expired view.
    """
    transport = enabled_client._transport
    assert isinstance(transport, ASGITransport)
    app = transport.app
    now = datetime.now(tz=UTC)
    view = DownloadJobView(
        id=uuid.uuid4(),
        media_job_id=uuid.uuid4(),
        public_state="expired",
        format_option_id="fmt_abc123",
        selected_format={
            "formatOptionId": "fmt_abc123",
            "container": "mp4",
            "qualityLabel": "p720",
            "freeTierEligible": True,
        },
        created_at=now - timedelta(hours=2),
        updated_at=now - timedelta(hours=1),
        expires_at=now - timedelta(minutes=1),
        completed_at=None,
        artifact_ready=False,
        error_code="DOWNLOAD_EXPIRED",
        created=False,
    )
    token = generate_access_token()
    service = MagicMock()
    service.get = AsyncMock(return_value=view)
    service.to_public_dict = DownloadJobService(app.state.settings).to_public_dict
    app.state.download_job_service = service

    response = await enabled_client.get(
        f"/api/v1/media/download-jobs/{view.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.headers.get("cache-control") == "no-store"
    body = response.json()
    assert body["state"] == "expired"
    assert body["artifactReady"] is False
    assert body.get("errorCode") == "DOWNLOAD_EXPIRED"
    assert token not in response.text


_VALID_SNAPSHOT: dict[str, object] = {
    "formatOptionId": "fmt_abc123",
    "container": "mp4",
    "width": 1280,
    "height": 720,
    "fps": 30.0,
    "hasVideo": True,
    "hasAudio": True,
    "category": "progressive",
    "videoCodec": "avc",
    "audioCodec": "aac",
    "approxBytes": 1000,
    "qualityLabel": "p720",
    "freeTierEligible": True,
}


def _row(snapshot: dict[str, object]) -> MediaDownloadJob:
    now = datetime.now(tz=UTC)
    return MediaDownloadJob(
        id=uuid.uuid4(),
        media_job_id=uuid.uuid4(),
        schema_version=1,
        public_state="queued",
        format_option_id="fmt_abc123",
        provider_id="vk",
        canonical_provider_url="https://vk.com/video-1_2",
        media_id="-1_2",
        hostname="vk.com",
        path="/video-1_2",
        scheme="https",
        port=None,
        selected_format_snapshot=snapshot,
        attempt_count=0,
        max_attempts=3,
        available_at=now,
        fence_token=0,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=1),
    )


@pytest.mark.asyncio
async def test_forged_persisted_snapshot_never_reaches_client(
    enabled_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forged JSONB keys fail closed and never appear in the response or repr."""
    transport = enabled_client._transport
    assert isinstance(transport, ASGITransport)
    app = transport.app
    forged = dict(_VALID_SNAPSHOT)
    forged["directUrl"] = "https://cdn.example.invalid/secret.mp4?sig=leak#frag"
    forged["providerFormatToken"] = "url720-secret-token"
    row = _row(forged)

    service = DownloadJobService(app.state.settings)
    app.state.download_job_service = service
    parent = MagicMock()
    parent.credential_hash = "parent-hash"
    parent.expires_at = row.expires_at

    monkeypatch.setattr(
        "fetchnow.downloads.service.MediaDownloadJobRepository.get_by_id",
        AsyncMock(return_value=row),
    )
    monkeypatch.setattr(
        "fetchnow.downloads.service.MediaDownloadJobRepository.database_now",
        AsyncMock(return_value=datetime.now(tz=UTC)),
    )
    monkeypatch.setattr(
        "fetchnow.downloads.service.MediaJobRepository.get_by_id",
        AsyncMock(return_value=parent),
    )
    monkeypatch.setattr(
        "fetchnow.downloads.service.tokens_match", lambda *_args: True
    )

    response = await enabled_client.get(
        f"/api/v1/media/download-jobs/{row.id}",
        headers={"Authorization": f"Bearer {generate_access_token()}"},
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert response.headers.get("cache-control") == "no-store"
    blob = response.text
    assert "directUrl" not in blob
    assert "cdn.example.invalid" not in blob
    assert "url720-secret-token" not in blob
    assert "sig=leak" not in blob
    assert "frag" not in blob
    assert "url720-secret-token" not in repr(row)
    assert "cdn.example.invalid" not in repr(row)


@pytest.mark.asyncio
async def test_valid_persisted_snapshot_is_re_encoded_not_echoed(
    enabled_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = enabled_client._transport
    assert isinstance(transport, ASGITransport)
    app = transport.app
    row = _row(dict(_VALID_SNAPSHOT))

    app.state.download_job_service = DownloadJobService(app.state.settings)
    parent = MagicMock()
    parent.credential_hash = "parent-hash"
    parent.expires_at = row.expires_at

    monkeypatch.setattr(
        "fetchnow.downloads.service.MediaDownloadJobRepository.get_by_id",
        AsyncMock(return_value=row),
    )
    monkeypatch.setattr(
        "fetchnow.downloads.service.MediaDownloadJobRepository.database_now",
        AsyncMock(return_value=datetime.now(tz=UTC)),
    )
    monkeypatch.setattr(
        "fetchnow.downloads.service.MediaJobRepository.get_by_id",
        AsyncMock(return_value=parent),
    )
    monkeypatch.setattr(
        "fetchnow.downloads.service.tokens_match", lambda *_args: True
    )

    response = await enabled_client.get(
        f"/api/v1/media/download-jobs/{row.id}",
        headers={"Authorization": f"Bearer {generate_access_token()}"},
    )

    assert response.status_code == 200
    assert set(response.json()["selectedFormat"]) == set(_VALID_SNAPSHOT)


@pytest.mark.asyncio
async def test_snapshot_option_id_mismatch_fails_closed(
    enabled_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = enabled_client._transport
    assert isinstance(transport, ASGITransport)
    app = transport.app
    drifted = dict(_VALID_SNAPSHOT)
    drifted["formatOptionId"] = "fmt_other"
    row = _row(drifted)

    app.state.download_job_service = DownloadJobService(app.state.settings)
    parent = MagicMock()
    parent.credential_hash = "parent-hash"
    parent.expires_at = row.expires_at

    monkeypatch.setattr(
        "fetchnow.downloads.service.MediaDownloadJobRepository.get_by_id",
        AsyncMock(return_value=row),
    )
    monkeypatch.setattr(
        "fetchnow.downloads.service.MediaDownloadJobRepository.database_now",
        AsyncMock(return_value=datetime.now(tz=UTC)),
    )
    monkeypatch.setattr(
        "fetchnow.downloads.service.MediaJobRepository.get_by_id",
        AsyncMock(return_value=parent),
    )
    monkeypatch.setattr(
        "fetchnow.downloads.service.tokens_match", lambda *_args: True
    )

    response = await enabled_client.get(
        f"/api/v1/media/download-jobs/{row.id}",
        headers={"Authorization": f"Bearer {generate_access_token()}"},
    )
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert response.headers.get("cache-control") == "no-store"
