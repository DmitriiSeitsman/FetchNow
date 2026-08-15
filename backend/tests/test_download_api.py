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
    job_id = uuid.uuid4()
    return DownloadJobView(
        id=job_id,
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
        progress_stage="queued",
        attempt_count=0,
        max_attempts=3,
        next_attempt_at=None,
        cancellable=True,
        progress_percent=None,
        artifact_bytes=None,
        suggested_filename="Example.mp4",
        created=created,
    )


@pytest.mark.asyncio
async def test_uuid_alone_without_bearer_is_404(
    enabled_client: AsyncClient,
) -> None:
    response = await enabled_client.get(f"/api/v1/media/download-jobs/{uuid.uuid4()}")
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
    assert body["progressStage"] == "queued"
    assert body["cancellable"] is True
    assert body["attempt"] == 0
    assert body["maxAttempts"] == 3
    assert body["progressPercent"] is None
    assert body["artifactBytes"] is None
    assert body["suggestedFilename"] == "Example.mp4"
    assert "failureClass" not in body
    assert "failure_class" not in body


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
async def test_provider_capability_disabled_maps_422(
    enabled_client: AsyncClient,
) -> None:
    transport = enabled_client._transport
    assert isinstance(transport, ASGITransport)
    app = transport.app
    token = generate_access_token()

    service = MagicMock()
    service.create = AsyncMock(
        side_effect=DownloadError(
            DownloadErrorCode.PROVIDER_CAPABILITY_DISABLED,
            internal_reason="CAPABILITY_OPERATION_DISABLED",
        )
    )
    app.state.download_job_service = service

    response = await enabled_client.post(
        f"/api/v1/media/jobs/{uuid.uuid4()}/downloads",
        json={"formatOptionId": "fmt_abc123"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PROVIDER_CAPABILITY_DISABLED"
    assert response.headers.get("cache-control") == "no-store"
    assert token not in response.text


@pytest.mark.asyncio
async def test_muxing_unavailable_maps_422(
    enabled_client: AsyncClient,
) -> None:
    transport = enabled_client._transport
    assert isinstance(transport, ASGITransport)
    app = transport.app
    token = generate_access_token()

    service = MagicMock()
    service.create = AsyncMock(
        side_effect=DownloadError(
            DownloadErrorCode.MUXING_UNAVAILABLE,
            internal_reason="MUXING_REQUIRED",
        )
    )
    app.state.download_job_service = service

    response = await enabled_client.post(
        f"/api/v1/media/jobs/{uuid.uuid4()}/downloads",
        json={"formatOptionId": "fmt_split"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MUXING_UNAVAILABLE"
    assert "ffmpeg" not in response.text.lower()
    assert token not in response.text


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
        progress_stage="expired",
        attempt_count=0,
        max_attempts=3,
        next_attempt_at=None,
        cancellable=False,
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
        progress_stage="queued",
        cancel_requested_at=None,
    )


def test_service_projects_stored_cancelled_encoding() -> None:
    settings = Settings(
        APP_ENV="test",
        LOG_LEVEL="WARNING",
        DATABASE_URL=_DB,
        MEDIA_DOWNLOADS_ENABLED=True,
        PROVIDER_VK_ENABLED=True,
        PROVIDER_RUTUBE_ENABLED=True,
    )
    now = datetime.now(tz=UTC)
    row = _row(dict(_VALID_SNAPSHOT))
    row.public_state = "expired"
    row.progress_stage = "cancelled"
    row.cancel_requested_at = now
    row.completed_at = now
    view = DownloadJobService(settings)._to_view(row, created=False)
    assert view.public_state == "cancelled"
    assert view.progress_stage == "cancelled"
    assert view.cancellable is False
    assert view.artifact_ready is False
    assert view.error_code is None
    public = DownloadJobService(settings).to_public_dict(view)
    assert public["state"] == "cancelled"
    assert public["progressStage"] == "cancelled"
    assert public["cancellable"] is False
    assert public["artifactReady"] is False
    assert public["errorCode"] is None

    ordinary = _row(dict(_VALID_SNAPSHOT))
    ordinary.public_state = "expired"
    ordinary.progress_stage = "expired"
    ordinary.cancel_requested_at = None
    expired = DownloadJobService(settings)._to_view(ordinary, created=False)
    assert expired.public_state == "expired"
    assert expired.error_code == "DOWNLOAD_EXPIRED"


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
    monkeypatch.setattr("fetchnow.downloads.service.tokens_match", lambda *_args: True)

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
    monkeypatch.setattr("fetchnow.downloads.service.tokens_match", lambda *_args: True)

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
    monkeypatch.setattr("fetchnow.downloads.service.tokens_match", lambda *_args: True)

    response = await enabled_client.get(
        f"/api/v1/media/download-jobs/{row.id}",
        headers={"Authorization": f"Bearer {generate_access_token()}"},
    )
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert response.headers.get("cache-control") == "no-store"


@pytest.mark.asyncio
async def test_uuid_alone_cannot_cancel(enabled_client: AsyncClient) -> None:
    response = await enabled_client.post(
        f"/api/v1/media/download-jobs/{uuid.uuid4()}/cancel"
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DOWNLOAD_JOB_NOT_FOUND"
    assert response.headers.get("cache-control") == "no-store"


@pytest.mark.asyncio
async def test_malformed_bearer_cancel_is_not_found(
    enabled_client: AsyncClient,
) -> None:
    response = await enabled_client.post(
        f"/api/v1/media/download-jobs/{uuid.uuid4()}/cancel",
        headers={"Authorization": "Bearer"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DOWNLOAD_JOB_NOT_FOUND"


@pytest.mark.asyncio
async def test_unknown_and_unauthorized_cancel_are_indistinguishable(
    enabled_client: AsyncClient,
) -> None:
    token = generate_access_token()
    transport = enabled_client._transport
    assert isinstance(transport, ASGITransport)
    app = transport.app
    service = MagicMock()
    service.cancel = AsyncMock(
        side_effect=[
            DownloadError(
                DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND,
                internal_reason="MISSING",
            ),
            DownloadError(
                DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND,
                internal_reason="UNAUTHORIZED",
            ),
        ]
    )
    app.state.download_job_service = service
    unknown = await enabled_client.post(
        f"/api/v1/media/download-jobs/{uuid.uuid4()}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    unauthorized = await enabled_client.post(
        f"/api/v1/media/download-jobs/{uuid.uuid4()}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert unknown.status_code == unauthorized.status_code == 404
    assert unknown.json()["error"]["code"] == unauthorized.json()["error"]["code"]
    assert unknown.json()["error"]["code"] == "DOWNLOAD_JOB_NOT_FOUND"
    assert unknown.json()["error"]["message"] == unauthorized.json()["error"]["message"]
    assert "MISSING" not in unknown.text
    assert "UNAUTHORIZED" not in unauthorized.text
    assert token not in unknown.text
    assert token not in unauthorized.text


@pytest.mark.asyncio
async def test_cancel_is_idempotent_and_does_not_run_tools(
    enabled_client: AsyncClient,
) -> None:
    transport = enabled_client._transport
    assert isinstance(transport, ASGITransport)
    app = transport.app
    queued = _view(created=False)
    view = DownloadJobView(
        id=queued.id,
        media_job_id=queued.media_job_id,
        public_state="cancelled",
        format_option_id=queued.format_option_id,
        selected_format=queued.selected_format,
        created_at=queued.created_at,
        updated_at=queued.updated_at,
        expires_at=queued.expires_at,
        completed_at=queued.created_at,
        artifact_ready=False,
        error_code=None,
        progress_stage="cancelled",
        attempt_count=0,
        max_attempts=3,
        next_attempt_at=None,
        cancellable=False,
        created=False,
    )
    token = generate_access_token()
    service = MagicMock()
    service.cancel = AsyncMock(return_value=view)
    service.to_public_dict = DownloadJobService(app.state.settings).to_public_dict
    app.state.download_job_service = service
    first = await enabled_client.post(
        f"/api/v1/media/download-jobs/{view.id}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    second = await enabled_client.post(
        f"/api/v1/media/download-jobs/{view.id}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["state"] == "cancelled"
    assert second.json()["state"] == "cancelled"
    assert first.json()["cancellable"] is False
    assert "failureClass" not in first.json()
    assert service.cancel.await_count == 2
    blob = first.text.lower()
    assert "yt-dlp" not in blob
    assert "ffmpeg" not in blob
    assert token not in first.text
