"""PostgreSQL service-level authorization tests for DeliveryService.

Skipped unless FETCHNOW_TEST_DATABASE_URL is set (asyncpg URL).
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fetchnow.core.config import Settings
from fetchnow.db.session import create_engine, create_session_factory
from fetchnow.delivery.service import DeliveryService
from fetchnow.downloads.errors import DownloadError, DownloadErrorCode
from fetchnow.downloads.models import MediaDownloadJob
from fetchnow.downloads.repository import MediaDownloadJobRepository
from fetchnow.jobs.credentials import (
    generate_access_token,
    hash_access_token,
    hash_request_fingerprint,
)
from fetchnow.jobs.models import MediaJob
from fetchnow.jobs.repository import MediaJobRepository
from fetchnow.jobs.states import MediaJobState

_TEST_URL = os.environ.get("FETCHNOW_TEST_DATABASE_URL", "").strip()
_BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _require_db() -> str:
    if not _TEST_URL:
        pytest.skip("FETCHNOW_TEST_DATABASE_URL not set")
    return _TEST_URL


def _run_alembic(*args: str, url: str) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = url
    env["APP_ENV"] = "test"
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_BACKEND_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"alembic {' '.join(args)} failed ({proc.returncode}):\n"
            f"{proc.stdout}\n{proc.stderr}"
        )


@pytest.fixture(scope="module")
def database_url() -> str:
    return _require_db()


@pytest.fixture(scope="module")
def migrated_database(database_url: str) -> str:
    _run_alembic("upgrade", "head", url=database_url)
    return database_url


@pytest.fixture
async def engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    settings = Settings(APP_ENV="test", DATABASE_URL=migrated_database)
    eng = create_engine(settings)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
async def session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


async def _cleanup(session: AsyncSession) -> None:
    await session.execute(text("DELETE FROM media_download_jobs"))
    await session.execute(text("DELETE FROM media_jobs"))
    await session.commit()


def _settings(url: str) -> Settings:
    return Settings(
        APP_ENV="test",
        DATABASE_URL=url,
        MEDIA_DELIVERY_ENABLED=True,
        # Authorize-only tests inject a mock reader; path is unused.
        MEDIA_DELIVERY_ROOT="/tmp/fetchnow-delivery-unused",
    )


def _service(url: str) -> DeliveryService:
    return DeliveryService(_settings(url), reader=MagicMock())


async def _parent_with_token(session: AsyncSession) -> tuple[MediaJob, str]:
    repo = MediaJobRepository(session)
    now = await repo.database_now()
    token = generate_access_token()
    job, _ = await repo.enqueue_or_get(
        credential_hash=hash_access_token(token),
        request_fingerprint=hash_request_fingerprint(
            access_token=token,
            normalized_request="https://vk.com/video-1_2",
        ),
        provider_id="vk",
        canonical_provider_url="https://vk.com/video-1_2",
        media_id="-1_2",
        hostname="vk.com",
        path="/video-1_2",
        scheme="https",
        port=None,
        now=now,
        ttl_seconds=3600,
        max_attempts=3,
    )
    claimed = await repo.claim_next(
        worker_id="setup",
        lease_seconds=60,
        now=now,
        limit=1,
    )
    assert len(claimed) == 1
    await repo.complete(
        job_id=claimed[0].id,
        owner="setup",
        fence=int(claimed[0].fence_token),
        metadata_json={
            "providerId": "vk",
            "canonicalProviderUrl": "https://vk.com/video-1_2",
            "mediaId": "-1_2",
            "title": None,
            "durationSeconds": 10,
            "formats": [],
            "muxingRequired": False,
            "extractionTool": "ytdlp",
            "extractionToolVersion": None,
        },
        now=now,
    )
    await session.flush()
    loaded = await repo.get_by_id(job.id)
    assert loaded is not None
    assert loaded.public_state == MediaJobState.INSPECTED.value
    return loaded, token


async def _enqueue_download(
    session: AsyncSession,
    *,
    parent: MediaJob,
    format_option_id: str = "fmt_deliv",
) -> MediaDownloadJob:
    repo = MediaDownloadJobRepository(session)
    now = await repo.database_now()
    job, _ = await repo.enqueue_or_get(
        media_job_id=parent.id,
        format_option_id=format_option_id,
        provider_id=parent.provider_id,
        canonical_provider_url=parent.canonical_provider_url,
        media_id=parent.media_id,
        hostname=parent.hostname,
        path=parent.path,
        scheme=parent.scheme,
        port=parent.port,
        selected_format_snapshot={
            "formatOptionId": format_option_id,
            "container": "mp4",
            "width": 1280,
            "height": 720,
            "fps": 30,
            "hasVideo": True,
            "hasAudio": True,
            "category": "progressive",
            "videoCodec": "avc",
            "audioCodec": "aac",
            "approxBytes": 1000,
            "qualityLabel": "p720",
            "freeTierEligible": True,
        },
        now=now,
        ttl_seconds=3600,
        max_attempts=3,
        parent_expires_at=parent.expires_at,
    )
    return job


async def _claim_and_ready(
    session: AsyncSession,
    *,
    job: MediaDownloadJob,
    owner: str = "worker-a",
) -> MediaDownloadJob:
    repo = MediaDownloadJobRepository(session)
    now = await repo.database_now()
    claimed = await repo.claim_next(
        worker_id=owner,
        lease_seconds=60,
        now=now,
        limit=1,
    )
    assert len(claimed) == 1
    assert claimed[0].id == job.id
    applied = await repo.complete_ready(
        job_id=job.id,
        owner=owner,
        fence=int(claimed[0].fence_token),
        artifact_id=uuid.uuid4(),
        artifact_bytes=128,
        artifact_content_type="video/mp4",
        artifact_container="mp4",
        now=now,
    )
    assert applied is True
    await session.flush()
    loaded = await repo.get_by_id(job.id)
    assert loaded is not None
    return loaded


def _public_error(exc: DownloadError) -> tuple[int, str]:
    return exc.http_status, exc.code.value


@pytest.mark.asyncio
async def test_authorize_ready_with_valid_parent_token(
    session_factory: async_sessionmaker[AsyncSession],
    migrated_database: str,
) -> None:
    service = _service(migrated_database)
    async with session_factory() as session:
        await _cleanup(session)
        parent, token = await _parent_with_token(session)
        job = await _enqueue_download(session, parent=parent)
        ready = await _claim_and_ready(session, job=job)
        await session.commit()

    async with session_factory() as session:
        authz = await service.authorize(
            download_job_id=ready.id,
            access_token=token,
            session=session,
        )
        assert authz.job.id == ready.id
        assert authz.job.public_state == "ready"


@pytest.mark.asyncio
async def test_authorize_unknown_and_wrong_token_indistinguishable(
    session_factory: async_sessionmaker[AsyncSession],
    migrated_database: str,
) -> None:
    service = _service(migrated_database)
    async with session_factory() as session:
        await _cleanup(session)
        parent, token = await _parent_with_token(session)
        job = await _enqueue_download(session, parent=parent)
        ready = await _claim_and_ready(session, job=job)
        await session.commit()

    other = generate_access_token()
    async with session_factory() as session:
        with pytest.raises(DownloadError) as unknown:
            await service.authorize(
                download_job_id=uuid.uuid4(),
                access_token=token,
                session=session,
            )
        with pytest.raises(DownloadError) as wrong:
            await service.authorize(
                download_job_id=ready.id,
                access_token=other,
                session=session,
            )
    assert _public_error(unknown.value) == _public_error(wrong.value)
    assert unknown.value.code is DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND
    assert unknown.value.http_status == 404


@pytest.mark.asyncio
async def test_authorize_expired_parent_and_child(
    session_factory: async_sessionmaker[AsyncSession],
    migrated_database: str,
) -> None:
    service = _service(migrated_database)
    async with session_factory() as session:
        await _cleanup(session)
        parent, token = await _parent_with_token(session)
        job = await _enqueue_download(session, parent=parent)
        ready = await _claim_and_ready(session, job=job)
        await session.commit()
        parent_id = parent.id
        job_id = ready.id

    async with session_factory() as session:
        past = datetime.now(tz=UTC) - timedelta(hours=1)
        await session.execute(
            text("UPDATE media_jobs SET expires_at = :past WHERE id = :id"),
            {"past": past, "id": parent_id},
        )
        await session.commit()

    async with session_factory() as session:
        with pytest.raises(DownloadError) as exc:
            await service.authorize(
                download_job_id=job_id,
                access_token=token,
                session=session,
            )
        assert exc.value.code is DownloadErrorCode.DOWNLOAD_EXPIRED

    async with session_factory() as session:
        await _cleanup(session)
        parent, token = await _parent_with_token(session)
        job = await _enqueue_download(session, parent=parent)
        ready = await _claim_and_ready(session, job=job)
        await session.commit()
        job_id = ready.id

    async with session_factory() as session:
        past = datetime.now(tz=UTC) - timedelta(minutes=5)
        await session.execute(
            text(
                "UPDATE media_download_jobs SET expires_at = :past WHERE id = :id"
            ),
            {"past": past, "id": job_id},
        )
        await session.commit()

    async with session_factory() as session:
        with pytest.raises(DownloadError) as exc:
            await service.authorize(
                download_job_id=job_id,
                access_token=token,
                session=session,
            )
        assert exc.value.code is DownloadErrorCode.DOWNLOAD_EXPIRED


@pytest.mark.asyncio
async def test_authorize_non_ready_states(
    session_factory: async_sessionmaker[AsyncSession],
    migrated_database: str,
) -> None:
    service = _service(migrated_database)
    async with session_factory() as session:
        await _cleanup(session)
        parent, token = await _parent_with_token(session)
        queued = await _enqueue_download(session, parent=parent)
        await session.commit()
        queued_id = queued.id

    async with session_factory() as session:
        with pytest.raises(DownloadError) as exc:
            await service.authorize(
                download_job_id=queued_id,
                access_token=token,
                session=session,
            )
        assert exc.value.code is DownloadErrorCode.DOWNLOAD_NOT_READY

    async with session_factory() as session:
        await _cleanup(session)
        parent, token = await _parent_with_token(session)
        job = await _enqueue_download(session, parent=parent)
        repo = MediaDownloadJobRepository(session)
        now = await repo.database_now()
        claimed = await repo.claim_next(
            worker_id="w",
            lease_seconds=60,
            now=now,
            limit=1,
        )
        await session.commit()
        downloading_id = claimed[0].id

    async with session_factory() as session:
        with pytest.raises(DownloadError) as exc:
            await service.authorize(
                download_job_id=downloading_id,
                access_token=token,
                session=session,
            )
        assert exc.value.code is DownloadErrorCode.DOWNLOAD_NOT_READY

    async with session_factory() as session:
        await _cleanup(session)
        parent, token = await _parent_with_token(session)
        job = await _enqueue_download(session, parent=parent)
        repo = MediaDownloadJobRepository(session)
        now = await repo.database_now()
        claimed = await repo.claim_next(
            worker_id="w",
            lease_seconds=60,
            now=now,
            limit=1,
        )
        await repo.fail_permanent(
            job_id=claimed[0].id,
            owner="w",
            fence=int(claimed[0].fence_token),
            public_error_code="DOWNLOAD_FAILED",
            now=now,
        )
        await session.commit()
        failed_id = claimed[0].id

    async with session_factory() as session:
        with pytest.raises(DownloadError) as exc:
            await service.authorize(
                download_job_id=failed_id,
                access_token=token,
                session=session,
            )
        assert exc.value.code is DownloadErrorCode.DOWNLOAD_NOT_READY
        del job


@pytest.mark.asyncio
async def test_authorize_incomplete_ready_pointer(
    session_factory: async_sessionmaker[AsyncSession],
    migrated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB constraints forbid incomplete ready rows; inject after load."""
    service = _service(migrated_database)
    async with session_factory() as session:
        await _cleanup(session)
        parent, token = await _parent_with_token(session)
        job = await _enqueue_download(session, parent=parent)
        ready = await _claim_and_ready(session, job=job)
        await session.commit()
        job_id = ready.id

    original = MediaDownloadJobRepository.get_by_id

    async def _strip_pointer(
        self: MediaDownloadJobRepository, job_id: uuid.UUID
    ) -> MediaDownloadJob | None:
        loaded = await original(self, job_id)
        if loaded is not None:
            # Detach before mutating: DB CHECK forbids incomplete ready rows,
            # but authorize must still reject incoherent pointers in memory.
            self._session.expunge(loaded)
            loaded.completed_at = None
        return loaded

    monkeypatch.setattr(MediaDownloadJobRepository, "get_by_id", _strip_pointer)

    async with session_factory() as session:
        with pytest.raises(DownloadError) as exc:
            await service.authorize(
                download_job_id=job_id,
                access_token=token,
                session=session,
            )
        assert exc.value.code is DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE


@pytest.mark.asyncio
async def test_authorize_parent_ownership_mismatch(
    session_factory: async_sessionmaker[AsyncSession],
    migrated_database: str,
) -> None:
    service = _service(migrated_database)
    async with session_factory() as session:
        await _cleanup(session)
        parent_a, _token_a = await _parent_with_token(session)
        parent_b, token_b = await _parent_with_token(session)
        # Distinct media ids so both parents can exist.
        del parent_b
        job = await _enqueue_download(session, parent=parent_a)
        ready = await _claim_and_ready(session, job=job)
        await session.commit()
        job_id = ready.id

    async with session_factory() as session:
        with pytest.raises(DownloadError) as with_b:
            await service.authorize(
                download_job_id=job_id,
                access_token=token_b,
                session=session,
            )
        with pytest.raises(DownloadError) as unknown:
            await service.authorize(
                download_job_id=uuid.uuid4(),
                access_token=token_b,
                session=session,
            )
    assert _public_error(with_b.value) == _public_error(unknown.value)
    assert with_b.value.code is DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND


@pytest.mark.asyncio
async def test_malformed_token_performs_no_database_lookup(
    session_factory: async_sessionmaker[AsyncSession],
    migrated_database: str,
) -> None:
    service = _service(migrated_database)
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(
        side_effect=AssertionError("database must not be queried")
    )
    with pytest.raises(DownloadError) as exc:
        await service.authorize(
            download_job_id=uuid.uuid4(),
            access_token="not-a-valid-token",
            session=session,
        )
    assert exc.value.code is DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND
    session.execute.assert_not_called()
    del session_factory
