"""FastAPI tests for media-job create/status endpoints (offline)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from fetchnow.api.main import create_app
from fetchnow.core.config import Settings
from fetchnow.jobs.credentials import (
    generate_access_token,
    hash_access_token,
    hash_request_fingerprint,
)
from fetchnow.jobs.errors import JobError, JobErrorCode
from fetchnow.jobs.models import MediaJob
from fetchnow.jobs.repository import MediaJobRepository
from fetchnow.jobs.service import JobCreateView, JobView, MediaJobService
from fetchnow.network.client import SafeHTTPClient
from fetchnow.resolution.defaults import build_wrapper_registry
from fetchnow.resolution.service import ResolutionService
from fetchnow.url.dns import FakeDnsResolver
from fetchnow.url.providers import ProviderRegistry
from fetchnow.url.validate import URLValidator

_DB = "postgresql+asyncpg://fetchnow:fetchnow@localhost:5432/fetchnow"


@pytest.fixture
def settings_disabled() -> Settings:
    return Settings(
        APP_NAME="FetchNow",
        APP_ENV="test",
        LOG_LEVEL="WARNING",
        DATABASE_URL=_DB,
        MEDIA_JOBS_ENABLED=False,
        PROVIDER_VK_ENABLED=True,
        PROVIDER_RUTUBE_ENABLED=True,
    )


@pytest.fixture
def settings_enabled() -> Settings:
    return Settings(
        APP_NAME="FetchNow",
        APP_ENV="test",
        LOG_LEVEL="WARNING",
        DATABASE_URL=_DB,
        MEDIA_JOBS_ENABLED=True,
        PROVIDER_VK_ENABLED=True,
        PROVIDER_RUTUBE_ENABLED=True,
    )


async def _build_client(settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(settings)
    dns = FakeDnsResolver(
        records={
            "vk.com": ["8.8.8.8"],
            "vkvideo.ru": ["8.8.8.8"],
            "rutube.ru": ["8.8.4.4"],
        },
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
        app.state.media_job_service = MediaJobService(settings)
        yield ac
    await http.aclose()


@pytest.fixture
async def disabled_client(
    settings_disabled: Settings,
) -> AsyncIterator[AsyncClient]:
    async for client in _build_client(settings_disabled):
        yield client


@pytest.fixture
async def enabled_client(
    settings_enabled: Settings,
) -> AsyncIterator[AsyncClient]:
    async for client in _build_client(settings_enabled):
        yield client


def _patch_ytdlp():
    return (
        patch(
            "fetchnow.media_inspection.ytdlp_adapter.YtDlpMetadataExtractor.extract",
            new_callable=AsyncMock,
        ),
        patch(
            "fetchnow.media_inspection.process.SubprocessProcessRunner.run",
            new_callable=AsyncMock,
        ),
    )


@pytest.mark.asyncio
async def test_disabled_jobs_returns_capacity_unavailable(
    disabled_client: AsyncClient,
) -> None:
    token = generate_access_token()
    extract_p, runner_p = _patch_ytdlp()
    with extract_p as extract_mock, runner_p as runner_mock:
        response = await disabled_client.post(
            "/api/v1/media/jobs",
            json={"url": "https://vk.com/video-1_2"},
            headers={"Authorization": f"Bearer {token}"},
        )
        extract_mock.assert_not_called()
        runner_mock.assert_not_called()
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "CAPACITY_UNAVAILABLE"
    assert token not in response.text


@pytest.mark.asyncio
async def test_create_validates_access_token_shape(
    enabled_client: AsyncClient,
) -> None:
    extract_p, runner_p = _patch_ytdlp()
    with extract_p as extract_mock, runner_p as runner_mock:
        response = await enabled_client.post(
            "/api/v1/media/jobs",
            json={"url": "https://vk.com/video-1_2"},
            headers={"Authorization": "Bearer too-short"},
        )
        extract_mock.assert_not_called()
        runner_mock.assert_not_called()
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_ACCESS_TOKEN"


@pytest.mark.asyncio
async def test_missing_bearer_on_get_returns_job_not_found(
    enabled_client: AsyncClient,
) -> None:
    extract_p, runner_p = _patch_ytdlp()
    with extract_p as extract_mock, runner_p as runner_mock:
        response = await enabled_client.get(f"/api/v1/media/jobs/{uuid.uuid4()}")
        extract_mock.assert_not_called()
        runner_mock.assert_not_called()
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "JOB_NOT_FOUND"


@pytest.mark.asyncio
async def test_api_does_not_call_ytdlp_on_mocked_create(
    enabled_client: AsyncClient,
) -> None:
    transport = enabled_client._transport
    assert isinstance(transport, ASGITransport)
    app = transport.app

    now = datetime.now(tz=UTC)
    token = generate_access_token()
    view = JobView(
        id=uuid.uuid4(),
        public_state="queued",
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=1),
        completed_at=None,
        result=None,
        error_code=None,
        created=True,
    )
    real = MediaJobService(
        Settings(APP_ENV="test", DATABASE_URL=_DB, MEDIA_JOBS_ENABLED=True)
    )
    mock_service = MagicMock(spec=MediaJobService)
    mock_service.create_job = AsyncMock(
        return_value=JobCreateView(job=view)
    )
    mock_service.get_job = AsyncMock(
        side_effect=JobError(JobErrorCode.JOB_NOT_FOUND)
    )
    mock_service.job_to_public_dict = real.job_to_public_dict
    app.state.media_job_service = mock_service

    extract_p, runner_p = _patch_ytdlp()
    with extract_p as extract_mock, runner_p as runner_mock:
        create = await enabled_client.post(
            "/api/v1/media/jobs",
            json={"url": "https://vk.com/video-1_2"},
            headers={"Authorization": f"Bearer {token}"},
        )
        get = await enabled_client.get(
            f"/api/v1/media/jobs/{view.id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        extract_mock.assert_not_called()
        runner_mock.assert_not_called()

    assert create.status_code == 202
    assert create.json()["id"] == str(view.id)
    assert "accessToken" not in create.json()
    assert create.headers["cache-control"] == "no-store"
    assert get.status_code == 404


@pytest.mark.asyncio
async def test_existing_job_retry_skips_resolution() -> None:
    token = generate_access_token()
    now = datetime.now(tz=UTC)
    job = MediaJob(
        id=uuid.uuid4(),
        schema_version=1,
        result_version=0,
        public_state="queued",
        provider_id="vk",
        canonical_provider_url="https://vk.com/video-1_2",
        media_id="-1_2",
        hostname="vk.com",
        path="/video-1_2",
        scheme="https",
        port=None,
        credential_hash=hash_access_token(token),
        request_fingerprint=hash_request_fingerprint(
            access_token=token,
            normalized_request="https://vk.com/video-1_2",
        ),
        result_metadata=None,
        public_error_code=None,
        attempt_count=0,
        max_attempts=3,
        available_at=now,
        lease_owner=None,
        lease_expires_at=None,
        fence_token=0,
        created_at=now,
        updated_at=now,
        started_at=None,
        completed_at=None,
        expires_at=now + timedelta(hours=1),
    )
    resolution = MagicMock()
    resolution.resolve = AsyncMock(side_effect=AssertionError("network called"))
    service = MediaJobService(
        Settings(APP_ENV="test", DATABASE_URL=_DB, MEDIA_JOBS_ENABLED=True)
    )
    with patch.object(
        MediaJobRepository,
        "get_by_credential_hash",
        new=AsyncMock(return_value=job),
    ):
        result = await service.create_job(
            raw_url="https://vk.com/video-1_2",
            access_token=token,
            resolution_service=resolution,
            session=MagicMock(),
        )
    assert result.job.id == job.id
    assert result.job.created is False
    resolution.resolve.assert_not_awaited()


_INSPECTED_METADATA = {
    "providerId": "vk",
    "canonicalProviderUrl": "https://vk.com/video-1_2",
    "mediaId": "-1_2",
    "title": None,
    "durationSeconds": 10,
    "formats": [],
    "muxingRequired": False,
    "extractionTool": "ytdlp",
    "extractionToolVersion": None,
}


def _service_job(
    *,
    token: str,
    public_state: str,
    result_metadata: dict[str, object] | None = None,
    public_error_code: str | None = None,
    expires_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> MediaJob:
    now = datetime.now(tz=UTC)
    return MediaJob(
        id=uuid.uuid4(),
        schema_version=1,
        result_version=1 if result_metadata is not None else 0,
        public_state=public_state,
        provider_id="vk",
        canonical_provider_url="https://vk.com/video-1_2",
        media_id="-1_2",
        hostname="vk.com",
        path="/video-1_2",
        scheme="https",
        port=None,
        credential_hash=hash_access_token(token),
        request_fingerprint=hash_request_fingerprint(
            access_token=token,
            normalized_request="https://vk.com/video-1_2",
        ),
        result_metadata=result_metadata,
        public_error_code=public_error_code,
        attempt_count=1,
        max_attempts=3,
        available_at=now,
        lease_owner=None,
        lease_expires_at=None,
        fence_token=1,
        created_at=now,
        updated_at=now,
        started_at=now,
        completed_at=completed_at,
        expires_at=expires_at or (now + timedelta(hours=1)),
    )


def _job_service() -> MediaJobService:
    return MediaJobService(
        Settings(APP_ENV="test", DATABASE_URL=_DB, MEDIA_JOBS_ENABLED=True)
    )


def test_to_view_expired_never_exposes_result_or_error() -> None:
    token = generate_access_token()
    job = _service_job(
        token=token,
        public_state="expired",
        result_metadata=_INSPECTED_METADATA,
        public_error_code=JobErrorCode.MEDIA_INSPECTION_FAILED.value,
        expires_at=datetime.now(tz=UTC) - timedelta(seconds=1),
        completed_at=datetime.now(tz=UTC),
    )
    view = _job_service()._to_view(job, created=False, include_result=True)
    assert view.result is None
    assert view.error_code is None
    public = _job_service().job_to_public_dict(view)
    assert public["result"] is None
    assert "errorCode" not in public or public.get("errorCode") is None


def test_to_view_inspected_returns_metadata_before_expiry() -> None:
    token = generate_access_token()
    job = _service_job(
        token=token,
        public_state="inspected",
        result_metadata=_INSPECTED_METADATA,
        completed_at=datetime.now(tz=UTC),
    )
    view = _job_service()._to_view(job, created=False, include_result=True)
    assert view.result is not None
    assert view.result.provider_id == "vk"
    public = _job_service().job_to_public_dict(view)
    assert public["result"]["providerId"] == "vk"


@pytest.mark.asyncio
async def test_get_job_expired_raises_without_returning_result() -> None:
    token = generate_access_token()
    job = _service_job(
        token=token,
        public_state="expired",
        result_metadata=None,
        expires_at=datetime.now(tz=UTC) - timedelta(minutes=1),
        completed_at=datetime.now(tz=UTC),
    )
    session = MagicMock()
    repo = MagicMock()
    repo.get_by_id = AsyncMock(return_value=job)
    repo.database_now = AsyncMock(return_value=datetime.now(tz=UTC))
    with (
        patch(
            "fetchnow.jobs.service.MediaJobRepository",
            return_value=repo,
        ),
        pytest.raises(JobError) as exc_info,
    ):
        await _job_service().get_job(
            job_id=job.id,
            access_token=token,
            session=session,
        )
    assert exc_info.value.code == JobErrorCode.JOB_EXPIRED


@pytest.mark.asyncio
async def test_get_job_inspected_returns_result_before_ttl() -> None:
    token = generate_access_token()
    job = _service_job(
        token=token,
        public_state="inspected",
        result_metadata=_INSPECTED_METADATA,
        completed_at=datetime.now(tz=UTC),
    )
    session = MagicMock()
    repo = MagicMock()
    repo.get_by_id = AsyncMock(return_value=job)
    repo.database_now = AsyncMock(return_value=datetime.now(tz=UTC))
    with patch(
        "fetchnow.jobs.service.MediaJobRepository",
        return_value=repo,
    ):
        view = await _job_service().get_job(
            job_id=job.id,
            access_token=token,
            session=session,
        )
    assert view.result is not None
    assert view.public_state == "inspected"


@pytest.mark.asyncio
async def test_get_job_wrong_token_stays_indistinguishable_not_found() -> None:
    owner = generate_access_token()
    other = generate_access_token()
    job = _service_job(token=owner, public_state="inspected")
    session = MagicMock()
    repo = MagicMock()
    repo.get_by_id = AsyncMock(return_value=job)
    with (
        patch(
            "fetchnow.jobs.service.MediaJobRepository",
            return_value=repo,
        ),
        pytest.raises(JobError) as exc_info,
    ):
        await _job_service().get_job(
            job_id=job.id,
            access_token=other,
            session=session,
        )
    assert exc_info.value.code == JobErrorCode.JOB_NOT_FOUND
    assert exc_info.value.http_status == 404


@pytest.mark.asyncio
async def test_get_job_unknown_id_stays_not_found() -> None:
    session = MagicMock()
    repo = MagicMock()
    repo.get_by_id = AsyncMock(return_value=None)
    with (
        patch(
            "fetchnow.jobs.service.MediaJobRepository",
            return_value=repo,
        ),
        pytest.raises(JobError) as exc_info,
    ):
        await _job_service().get_job(
            job_id=uuid.uuid4(),
            access_token=generate_access_token(),
            session=session,
        )
    assert exc_info.value.code == JobErrorCode.JOB_NOT_FOUND
    assert exc_info.value.http_status == 404
