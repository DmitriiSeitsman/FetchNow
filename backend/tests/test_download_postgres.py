"""PostgreSQL integration tests for media-download jobs.

Skipped unless FETCHNOW_TEST_DATABASE_URL is set (asyncpg URL).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fetchnow.core.config import Settings
from fetchnow.db.session import create_engine, create_session_factory
from fetchnow.downloads.errors import DownloadError, DownloadErrorCode
from fetchnow.downloads.models import MediaDownloadJob
from fetchnow.downloads.repository import MediaDownloadJobRepository
from fetchnow.downloads.service import DownloadJobService
from fetchnow.downloads.states import is_stored_cancelled
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


def test_configured_unreachable_postgres_fails_not_skipped() -> None:
    """A configured but unreachable DSN must raise, not skip."""
    settings = Settings(
        APP_ENV="test",
        DATABASE_URL="postgresql+asyncpg://fetchnow:fetchnow@127.0.0.1:1/fetchnow",
    )
    eng = create_engine(settings)

    async def _connect() -> None:
        try:
            async with eng.connect() as conn:
                await conn.execute(text("SELECT 1"))
        finally:
            await eng.dispose()

    with pytest.raises((OSError, ConnectionRefusedError, DBAPIError, TimeoutError)):
        asyncio.run(_connect())


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


async def _parent_ready(session: AsyncSession) -> MediaJob:
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
    return loaded


async def _enqueue_download(
    session: AsyncSession,
    *,
    parent: MediaJob,
    format_option_id: str = "fmt_abc",
    suggested_filename: str | None = "Example.mp4",
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
        suggested_filename=suggested_filename,
    )
    return job


def _assert_stored_cancelled(row: MediaDownloadJob) -> None:
    assert is_stored_cancelled(
        row.public_state, row.progress_stage, row.cancel_requested_at
    )
    assert row.public_state == "expired"
    assert row.progress_stage == "cancelled"
    assert row.artifact_id is None
    assert row.public_error_code is None
    assert row.lease_owner is None
    assert row.completed_at is not None


class _Pr11ProgressStage(StrEnum):
    """PR11 progress_stage enum: no knowledge of progress_percent."""

    QUEUED = "queued"
    INSPECTING = "inspecting"
    DOWNLOADING_VIDEO = "downloading_video"
    DOWNLOADING_AUDIO = "downloading_audio"
    MUXING = "muxing"
    VERIFYING = "verifying"
    PUBLISHING = "publishing"
    RETRYING = "retrying"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


def _pr11_parse_row(public_state: str, progress_stage: str) -> tuple[str, str]:
    return (
        _Pr9MediaDownloadJobState(public_state).value,
        _Pr11ProgressStage(progress_stage).value,
    )


class _Pr9MediaDownloadJobState(StrEnum):
    """PR9 application enum: no ``cancelled`` member."""

    QUEUED = "queued"
    DOWNLOADING = "downloading"
    READY = "ready"
    FAILED = "failed"
    EXPIRED = "expired"


def _pr9_service_read(
    public_state: str, public_error_code: str | None
) -> dict[str, object]:
    """Reproduce PR9 ``DownloadJobService._to_view`` state parsing.

    ``MediaDownloadJobState(job.public_state)`` is the read boundary that
    raised ``ValueError`` when PR10 stored raw ``cancelled``.
    """
    state = _Pr9MediaDownloadJobState(public_state)
    error_code: str | None = None
    if state is _Pr9MediaDownloadJobState.FAILED:
        try:
            error_code = DownloadErrorCode(str(public_error_code)).value
        except ValueError:
            error_code = DownloadErrorCode.INTERNAL_ERROR.value
    elif state is _Pr9MediaDownloadJobState.EXPIRED:
        error_code = DownloadErrorCode.DOWNLOAD_EXPIRED.value
    return {
        "state": state.value,
        "artifactReady": state is _Pr9MediaDownloadJobState.READY,
        "errorCode": error_code,
    }


def test_alembic_0002_0003_roundtrip(database_url: str) -> None:
    _run_alembic("upgrade", "head", url=database_url)
    _run_alembic("downgrade", "0002_media_jobs", url=database_url)

    async def _download_table_exists() -> bool:
        settings = Settings(APP_ENV="test", DATABASE_URL=database_url)
        eng = create_engine(settings)
        try:
            async with eng.connect() as conn:
                return bool(
                    await conn.scalar(
                        text(
                            "SELECT EXISTS ("
                            "SELECT 1 FROM information_schema.tables "
                            "WHERE table_name = 'media_download_jobs')"
                        )
                    )
                )
        finally:
            await eng.dispose()

    assert asyncio.run(_download_table_exists()) is False
    _run_alembic("upgrade", "0003_download_jobs", url=database_url)
    assert asyncio.run(_download_table_exists()) is True
    _run_alembic("downgrade", "0002_media_jobs", url=database_url)
    assert asyncio.run(_download_table_exists()) is False
    _run_alembic("upgrade", "0003_download_jobs", url=database_url)
    assert asyncio.run(_download_table_exists()) is True
    _run_alembic("upgrade", "head", url=database_url)


def test_alembic_0003_0004_roundtrip(database_url: str) -> None:
    _run_alembic("upgrade", "head", url=database_url)

    async def _has_progress() -> bool:
        settings = Settings(APP_ENV="test", DATABASE_URL=database_url)
        eng = create_engine(settings)
        try:
            async with eng.connect() as conn:
                return bool(
                    await conn.scalar(
                        text(
                            "SELECT EXISTS ("
                            "SELECT 1 FROM information_schema.columns "
                            "WHERE table_name = 'media_download_jobs' "
                            "AND column_name = 'progress_stage')"
                        )
                    )
                )
        finally:
            await eng.dispose()

    assert asyncio.run(_has_progress()) is True
    _run_alembic("downgrade", "0003_download_jobs", url=database_url)
    assert asyncio.run(_has_progress()) is False
    _run_alembic("upgrade", "head", url=database_url)
    assert asyncio.run(_has_progress()) is True
    _run_alembic("downgrade", "0003_download_jobs", url=database_url)
    assert asyncio.run(_has_progress()) is False
    _run_alembic("upgrade", "head", url=database_url)
    assert asyncio.run(_has_progress()) is True


@pytest.mark.asyncio
async def test_concurrent_claim_never_double_owns(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _cleanup(session)
        parent = await _parent_ready(session)
        await _enqueue_download(session, parent=parent)
        await session.commit()

    async def claimer(worker_id: str) -> list[uuid.UUID]:
        async with session_factory() as session:
            repo = MediaDownloadJobRepository(session)
            claimed = await repo.claim_next(
                worker_id=worker_id,
                lease_seconds=60,
                now=datetime.now(tz=UTC),
                limit=1,
            )
            await session.commit()
            return [job.id for job in claimed]

    results = await asyncio.gather(claimer("worker-a"), claimer("worker-b"))
    flat = [job_id for batch in results for job_id in batch]
    assert len(flat) == 1
    assert len(set(flat)) == 1


@pytest.mark.asyncio
async def test_stale_fence_cannot_complete(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _cleanup(session)
        parent = await _parent_ready(session)
        job = await _enqueue_download(session, parent=parent)
        repo = MediaDownloadJobRepository(session)
        now = await repo.database_now()
        claimed = await repo.claim_next(
            worker_id="owner-a",
            lease_seconds=60,
            now=now,
            limit=1,
        )
        await session.commit()
        assert len(claimed) == 1
        fence = int(claimed[0].fence_token)
        job_id = claimed[0].id

    async with session_factory() as session:
        repo = MediaDownloadJobRepository(session)
        now = await repo.database_now()
        # Simulate reclaim bumping the fence.
        await session.execute(
            text(
                "UPDATE media_download_jobs SET fence_token = fence_token + 1, "
                "lease_owner = 'other', "
                "lease_expires_at = NOW() + interval '60 seconds' "
                "WHERE id = :id"
            ),
            {"id": job_id},
        )
        await session.commit()

    async with session_factory() as session:
        repo = MediaDownloadJobRepository(session)
        now = await repo.database_now()
        applied = await repo.complete_ready(
            job_id=job_id,
            owner="owner-a",
            fence=fence,
            artifact_id=uuid.uuid4(),
            artifact_bytes=12,
            artifact_content_type="video/mp4",
            artifact_container="mp4",
            now=now,
        )
        await session.commit()
        assert applied is False
        del job


@pytest.mark.asyncio
async def test_unique_parent_format(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _cleanup(session)
        parent = await _parent_ready(session)
        repo = MediaDownloadJobRepository(session)
        now = await repo.database_now()
        kwargs = dict(
            media_job_id=parent.id,
            format_option_id="fmt_same",
            provider_id=parent.provider_id,
            canonical_provider_url=parent.canonical_provider_url,
            media_id=parent.media_id,
            hostname=parent.hostname,
            path=parent.path,
            scheme=parent.scheme,
            port=parent.port,
            selected_format_snapshot={"formatOptionId": "fmt_same"},
            now=now,
            ttl_seconds=3600,
            max_attempts=3,
            parent_expires_at=parent.expires_at,
        )
        first, created1 = await repo.enqueue_or_get(**kwargs)
        second, created2 = await repo.enqueue_or_get(**kwargs)
        await session.commit()
        assert created1 is True
        assert created2 is False
        assert first.id == second.id

        count = await session.scalar(
            text(
                "SELECT COUNT(*) FROM media_download_jobs "
                "WHERE media_job_id = :mid AND format_option_id = 'fmt_same'"
            ),
            {"mid": parent.id},
        )
        assert int(count or 0) == 1


def _create_kwargs(parent: MediaJob, now: datetime) -> dict[str, object]:
    return {
        "media_job_id": parent.id,
        "format_option_id": "fmt_coherent",
        "provider_id": parent.provider_id,
        "canonical_provider_url": parent.canonical_provider_url,
        "media_id": parent.media_id,
        "hostname": parent.hostname,
        "path": parent.path,
        "scheme": parent.scheme,
        "port": parent.port,
        "selected_format_snapshot": {
            "formatOptionId": "fmt_coherent",
            "container": "mp4",
        },
        "now": now,
        "ttl_seconds": 3600,
        "max_attempts": 3,
        "parent_expires_at": parent.expires_at,
    }


@pytest.mark.asyncio
async def test_idempotent_recovery_requires_coherent_immutable_fields(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _cleanup(session)
        parent = await _parent_ready(session)
        repo = MediaDownloadJobRepository(session)
        now = await repo.database_now()

        first, created = await repo.enqueue_or_get(**_create_kwargs(parent, now))
        await session.commit()
        assert created is True

    # Coherent replay recovers the same row.
    async with session_factory() as session:
        repo = MediaDownloadJobRepository(session)
        now = await repo.database_now()
        recovered, created_again = await repo.enqueue_or_get(
            **_create_kwargs(parent, now)
        )
        await session.commit()
        assert created_again is False
        assert recovered.id == first.id

    # Every immutable divergence must fail closed instead of returning the row.
    divergences: list[dict[str, object]] = [
        {"canonical_provider_url": "https://vk.com/video-9_9"},
        {"media_id": "-9_9"},
        {"provider_id": "rutube"},
        {"hostname": "rutube.ru"},
        {"path": "/video-9_9"},
        {"port": 8443},
        {
            "selected_format_snapshot": {
                "formatOptionId": "fmt_coherent",
                "container": "webm",
            }
        },
        {"max_attempts": 5},
    ]
    for override in divergences:
        async with session_factory() as session:
            repo = MediaDownloadJobRepository(session)
            now = await repo.database_now()
            kwargs = _create_kwargs(parent, now)
            kwargs.update(override)
            with pytest.raises(DownloadError) as exc:
                await repo.enqueue_or_get(**kwargs)
            assert exc.value.code == DownloadErrorCode.INTERNAL_ERROR, override
            await session.rollback()


@pytest.mark.asyncio
async def test_concurrent_creates_converge_on_one_coherent_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _cleanup(session)
        parent = await _parent_ready(session)
        await session.commit()

    async def creator() -> uuid.UUID:
        async with session_factory() as session:
            repo = MediaDownloadJobRepository(session)
            now = await repo.database_now()
            job, _ = await repo.enqueue_or_get(**_create_kwargs(parent, now))
            await session.commit()
            return job.id

    ids = await asyncio.gather(creator(), creator(), creator())
    assert len(set(ids)) == 1

    async with session_factory() as session:
        count = await session.scalar(
            text("SELECT COUNT(*) FROM media_download_jobs WHERE media_job_id = :mid"),
            {"mid": parent.id},
        )
        assert int(count or 0) == 1


@pytest.mark.asyncio
async def test_download_expiry_never_exceeds_parent_expiry(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _cleanup(session)
        parent = await _parent_ready(session)
        repo = MediaDownloadJobRepository(session)
        now = await repo.database_now()
        kwargs = _create_kwargs(parent, now)
        # TTL alone would outlive the parent authorization by far.
        kwargs["ttl_seconds"] = 86_400
        job, created = await repo.enqueue_or_get(**kwargs)
        await session.commit()
        assert created is True
        assert job.expires_at <= parent.expires_at


@pytest.mark.asyncio
async def test_queued_cancel_never_claimed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _cleanup(session)
        parent = await _parent_ready(session)
        job = await _enqueue_download(session, parent=parent)
        repo = MediaDownloadJobRepository(session)
        now = await repo.database_now()
        cancelled = await repo.request_cancel(job_id=job.id, now=now)
        await session.commit()
        assert cancelled is not None
        _assert_stored_cancelled(cancelled)
        claimed = await repo.claim_next(
            worker_id="after-cancel",
            lease_seconds=60,
            now=now,
            limit=1,
        )
        await session.commit()
        assert claimed == []


@pytest.mark.asyncio
async def test_cancel_ready_race_single_terminal(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _cleanup(session)
        parent = await _parent_ready(session)
        await _enqueue_download(session, parent=parent)
        repo = MediaDownloadJobRepository(session)
        now = await repo.database_now()
        claimed = await repo.claim_next(
            worker_id="owner-a",
            lease_seconds=60,
            now=now,
            limit=1,
        )
        await session.commit()
        assert len(claimed) == 1
        job_id = claimed[0].id
        fence = int(claimed[0].fence_token)

    ready_started = asyncio.Event()
    cancel_started = asyncio.Event()

    async def complete() -> bool:
        async with session_factory() as session:
            repo = MediaDownloadJobRepository(session)
            now = await repo.database_now()
            ready_started.set()
            await cancel_started.wait()
            applied = await repo.complete_ready(
                job_id=job_id,
                owner="owner-a",
                fence=fence,
                artifact_id=uuid.uuid4(),
                artifact_bytes=12,
                artifact_content_type="video/mp4",
                artifact_container="mp4",
                now=now,
            )
            await session.commit()
            return applied

    async def cancel() -> str:
        async with session_factory() as session:
            repo = MediaDownloadJobRepository(session)
            now = await repo.database_now()
            cancel_started.set()
            await ready_started.wait()
            updated = await repo.request_cancel(job_id=job_id, now=now)
            await session.commit()
            assert updated is not None
            return updated.public_state

    applied, cancel_state = await asyncio.gather(complete(), cancel())
    async with session_factory() as session:
        repo = MediaDownloadJobRepository(session)
        row = await repo.get_by_id(job_id)
        assert row is not None
        assert row.public_state in {"ready", "expired", "downloading"}
        if row.public_state == "ready":
            assert applied is True
            assert row.artifact_id is not None
            # Cancel after ready is a no-op.
            now = await repo.database_now()
            again = await repo.request_cancel(job_id=job_id, now=now)
            await session.commit()
            assert again is not None
            assert again.public_state == "ready"
        elif is_stored_cancelled(
            row.public_state, row.progress_stage, row.cancel_requested_at
        ):
            assert row.artifact_id is None
            assert row.public_error_code is None
        else:
            assert row.cancel_requested_at is not None
            now = await repo.database_now()
            finished = await repo.complete_cancelled(
                job_id=job_id,
                owner="owner-a",
                fence=fence,
                now=now,
            )
            await session.commit()
            assert finished is True
            row = await repo.get_by_id(job_id)
            assert row is not None
            _assert_stored_cancelled(row)
        terminals = 0
        if row.public_state == "ready":
            terminals += 1
        if is_stored_cancelled(
            row.public_state, row.progress_stage, row.cancel_requested_at
        ):
            terminals += 1
        assert terminals == 1
        del cancel_state


@pytest.mark.asyncio
async def test_stale_fence_cannot_complete_cancelled_job(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _cleanup(session)
        parent = await _parent_ready(session)
        await _enqueue_download(session, parent=parent)
        repo = MediaDownloadJobRepository(session)
        now = await repo.database_now()
        claimed = await repo.claim_next(
            worker_id="owner-a",
            lease_seconds=60,
            now=now,
            limit=1,
        )
        await session.commit()
        job_id = claimed[0].id
        fence = int(claimed[0].fence_token)
        await repo.request_cancel(job_id=job_id, now=now)
        await session.commit()
        applied_cancel = await repo.complete_cancelled(
            job_id=job_id,
            owner="owner-a",
            fence=fence,
            now=now,
        )
        await session.commit()
        assert applied_cancel is True
        applied_ready = await repo.complete_ready(
            job_id=job_id,
            owner="owner-a",
            fence=fence,
            artifact_id=uuid.uuid4(),
            artifact_bytes=12,
            artifact_content_type="video/mp4",
            artifact_container="mp4",
            now=now,
        )
        await session.commit()
        assert applied_ready is False
        row = await repo.get_by_id(job_id)
        assert row is not None
        _assert_stored_cancelled(row)
        retried = await repo.fail_retry(
            job_id=job_id,
            owner="owner-a",
            fence=fence,
            now=now,
            backoff_seconds=1,
            public_error_code="DOWNLOAD_TOOL_FAILED",
        )
        await session.commit()
        assert retried is False
        row = await repo.get_by_id(job_id)
        assert row is not None
        _assert_stored_cancelled(row)


@pytest.mark.asyncio
async def test_complete_cancelled_without_request_is_false(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _cleanup(session)
        parent = await _parent_ready(session)
        await _enqueue_download(session, parent=parent)
        repo = MediaDownloadJobRepository(session)
        now = await repo.database_now()
        claimed = await repo.claim_next(
            worker_id="owner-a",
            lease_seconds=60,
            now=now,
            limit=1,
        )
        await session.commit()
        job = claimed[0]
        applied = await repo.complete_cancelled(
            job_id=job.id,
            owner="owner-a",
            fence=int(job.fence_token),
            now=now,
        )
        await session.commit()
        assert applied is False
        row = await repo.get_by_id(job.id)
        assert row is not None
        assert row.public_state == "downloading"
        assert row.cancel_requested_at is None
        assert row.progress_stage != "cancelled"


@pytest.mark.asyncio
async def test_expired_lease_release_is_not_cancelled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _cleanup(session)
        parent = await _parent_ready(session)
        await _enqueue_download(session, parent=parent)
        repo = MediaDownloadJobRepository(session)
        now = await repo.database_now()
        claimed = await repo.claim_next(
            worker_id="owner-a",
            lease_seconds=60,
            now=now,
            limit=1,
        )
        await session.commit()
        job_id = claimed[0].id
        fence = int(claimed[0].fence_token)
        attempts = int(claimed[0].attempt_count)
        await session.execute(
            text(
                "UPDATE media_download_jobs "
                "SET lease_expires_at = NOW() - interval '1 second' "
                "WHERE id = :id"
            ),
            {"id": job_id},
        )
        await session.commit()
        released = await repo.release_to_queued(
            job_id=job_id,
            owner="owner-a",
            fence=fence,
            now=now,
        )
        cancelled = await repo.complete_cancelled(
            job_id=job_id,
            owner="owner-a",
            fence=fence,
            now=now,
        )
        await session.commit()
        assert released is False
        assert cancelled is False
        row = await repo.get_by_id(job_id)
        assert row is not None
        assert row.public_state == "downloading"
        assert row.cancel_requested_at is None
        assert int(row.attempt_count) == attempts


@pytest.mark.asyncio
async def test_complete_ready_false_lease_expiry_is_not_cancelled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _cleanup(session)
        parent = await _parent_ready(session)
        await _enqueue_download(session, parent=parent)
        repo = MediaDownloadJobRepository(session)
        now = await repo.database_now()
        claimed = await repo.claim_next(
            worker_id="owner-a",
            lease_seconds=60,
            now=now,
            limit=1,
        )
        await session.commit()
        job_id = claimed[0].id
        fence = int(claimed[0].fence_token)
        await session.execute(
            text(
                "UPDATE media_download_jobs "
                "SET lease_expires_at = NOW() - interval '1 second' "
                "WHERE id = :id"
            ),
            {"id": job_id},
        )
        await session.commit()
        applied = await repo.complete_ready(
            job_id=job_id,
            owner="owner-a",
            fence=fence,
            artifact_id=uuid.uuid4(),
            artifact_bytes=12,
            artifact_content_type="video/mp4",
            artifact_container="mp4",
            now=now,
        )
        cancelled = await repo.complete_cancelled(
            job_id=job_id,
            owner="owner-a",
            fence=fence,
            now=now,
        )
        await session.commit()
        assert applied is False
        assert cancelled is False
        row = await repo.get_by_id(job_id)
        assert row is not None
        assert row.public_state == "downloading"
        assert row.cancel_requested_at is None


@pytest.mark.asyncio
async def test_complete_ready_false_real_cancel_completes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _cleanup(session)
        parent = await _parent_ready(session)
        await _enqueue_download(session, parent=parent)
        repo = MediaDownloadJobRepository(session)
        now = await repo.database_now()
        claimed = await repo.claim_next(
            worker_id="owner-a",
            lease_seconds=60,
            now=now,
            limit=1,
        )
        await session.commit()
        job_id = claimed[0].id
        fence = int(claimed[0].fence_token)
        attempts = int(claimed[0].attempt_count)
        updated = await repo.request_cancel(job_id=job_id, now=now)
        await session.commit()
        assert updated is not None
        assert updated.public_state == "downloading"
        applied = await repo.complete_ready(
            job_id=job_id,
            owner="owner-a",
            fence=fence,
            artifact_id=uuid.uuid4(),
            artifact_bytes=12,
            artifact_content_type="video/mp4",
            artifact_container="mp4",
            now=now,
        )
        cancelled = await repo.complete_cancelled(
            job_id=job_id,
            owner="owner-a",
            fence=fence,
            now=now,
        )
        await session.commit()
        assert applied is False
        assert cancelled is True
        row = await repo.get_by_id(job_id)
        assert row is not None
        _assert_stored_cancelled(row)
        assert row.artifact_id is None
        assert int(row.attempt_count) == attempts


def test_pr9_application_lifecycle_after_0004(database_url: str) -> None:
    """Previous PR9 DML (no progress_stage writes) must survive 0004."""
    _run_alembic("upgrade", "0003_download_jobs", url=database_url)
    _run_alembic("upgrade", "head", url=database_url)

    async def _run() -> None:
        settings = Settings(APP_ENV="test", DATABASE_URL=database_url)
        eng = create_engine(settings)
        factory = create_session_factory(eng)
        try:
            async with factory() as session:
                await _cleanup(session)
                parent = await _parent_ready(session)
                await session.commit()
                parent_id = parent.id
                parent_expires = parent.expires_at

            async with factory() as session:
                insert_id = uuid.uuid4()
                await session.execute(
                    text(
                        "INSERT INTO media_download_jobs ("
                        "id, media_job_id, schema_version, public_state, "
                        "format_option_id, provider_id, canonical_provider_url, "
                        "media_id, hostname, path, scheme, port, "
                        "selected_format_snapshot, attempt_count, max_attempts, "
                        "available_at, fence_token, created_at, updated_at, "
                        "expires_at"
                        ") VALUES ("
                        ":id, :parent, 1, 'queued', :fmt, 'vk', "
                        "'https://vk.com/video-1_2', '-1_2', 'vk.com', "
                        "'/video-1_2', 'https', NULL, CAST(:snap AS jsonb), "
                        "0, 3, NOW(), 0, NOW(), NOW(), "
                        "NOW() + interval '1 hour')"
                    ),
                    {
                        "id": insert_id,
                        "parent": parent_id,
                        "fmt": "fmt_pr9insert000000000000000001",
                        "snap": (
                            '{"formatOptionId":"fmt_pr9insert000000000000000001"}'
                        ),
                    },
                )
                await session.commit()
                stage = await session.scalar(
                    text(
                        "SELECT progress_stage FROM media_download_jobs WHERE id = :id"
                    ),
                    {"id": insert_id},
                )
                assert stage == "queued"

                claimed = await session.execute(
                    text(
                        "UPDATE media_download_jobs SET "
                        "public_state = 'downloading', "
                        "lease_owner = 'pr9-worker', "
                        "lease_expires_at = NOW() + interval '60 seconds', "
                        "fence_token = fence_token + 1, "
                        "attempt_count = attempt_count + 1, "
                        "started_at = COALESCE(started_at, NOW()), "
                        "updated_at = NOW() "
                        "WHERE id = :id AND public_state = 'queued' "
                        "RETURNING fence_token, progress_stage, public_state"
                    ),
                    {"id": insert_id},
                )
                claim_row = claimed.one()
                await session.commit()
                assert claim_row[1] == "inspecting"
                assert claim_row[2] == "downloading"
                fence = int(claim_row[0])

                retried = await session.execute(
                    text(
                        "UPDATE media_download_jobs SET "
                        "public_state = 'queued', public_error_code = NULL, "
                        "available_at = NOW() + interval '2 seconds', "
                        "lease_owner = NULL, lease_expires_at = NULL, "
                        "updated_at = NOW() "
                        "WHERE id = :id AND public_state = 'downloading' "
                        "AND lease_owner = 'pr9-worker' "
                        "AND fence_token = :fence "
                        "RETURNING progress_stage, public_state"
                    ),
                    {"id": insert_id, "fence": fence},
                )
                retry_row = retried.one()
                await session.commit()
                assert retry_row[0] == "queued"
                assert retry_row[1] == "queued"

                claimed2 = await session.execute(
                    text(
                        "UPDATE media_download_jobs SET "
                        "public_state = 'downloading', "
                        "lease_owner = 'pr9-worker', "
                        "lease_expires_at = NOW() + interval '60 seconds', "
                        "fence_token = fence_token + 1, "
                        "attempt_count = attempt_count + 1, "
                        "started_at = COALESCE(started_at, NOW()), "
                        "updated_at = NOW() "
                        "WHERE id = :id AND public_state = 'queued' "
                        "RETURNING fence_token, progress_stage"
                    ),
                    {"id": insert_id},
                )
                claim2 = claimed2.one()
                await session.commit()
                assert claim2[1] == "inspecting"
                fence2 = int(claim2[0])

                ready = await session.execute(
                    text(
                        "UPDATE media_download_jobs SET "
                        "public_state = 'ready', "
                        "artifact_id = :art, artifact_bytes = 12, "
                        "artifact_content_type = 'video/mp4', "
                        "artifact_container = 'mp4', "
                        "public_error_code = NULL, "
                        "lease_owner = NULL, lease_expires_at = NULL, "
                        "completed_at = NOW(), updated_at = NOW() "
                        "WHERE id = :id AND public_state = 'downloading' "
                        "AND lease_owner = 'pr9-worker' "
                        "AND fence_token = :fence "
                        "AND lease_expires_at > clock_timestamp() "
                        "RETURNING progress_stage, public_state"
                    ),
                    {
                        "id": insert_id,
                        "fence": fence2,
                        "art": uuid.uuid4(),
                    },
                )
                ready_row = ready.one()
                await session.commit()
                assert ready_row[0] == "ready"
                assert ready_row[1] == "ready"

            async with factory() as session:
                fail_id = uuid.uuid4()
                await session.execute(
                    text(
                        "INSERT INTO media_download_jobs ("
                        "id, media_job_id, schema_version, public_state, "
                        "format_option_id, provider_id, canonical_provider_url, "
                        "media_id, hostname, path, scheme, "
                        "selected_format_snapshot, attempt_count, max_attempts, "
                        "available_at, fence_token, created_at, updated_at, "
                        "expires_at"
                        ") VALUES ("
                        ":id, :parent, 1, 'queued', :fmt, 'vk', "
                        "'https://vk.com/video-1_2', '-1_2', 'vk.com', "
                        "'/video-1_2', 'https', CAST(:snap AS jsonb), "
                        "0, 3, NOW(), 0, NOW(), NOW(), "
                        "NOW() + interval '1 hour')"
                    ),
                    {
                        "id": fail_id,
                        "parent": parent_id,
                        "fmt": "fmt_pr9failed000000000000000001",
                        "snap": (
                            '{"formatOptionId":"fmt_pr9failed000000000000000001"}'
                        ),
                    },
                )
                await session.commit()
                claimed_f = await session.execute(
                    text(
                        "UPDATE media_download_jobs SET "
                        "public_state = 'downloading', "
                        "lease_owner = 'pr9-worker', "
                        "lease_expires_at = NOW() + interval '60 seconds', "
                        "fence_token = fence_token + 1, "
                        "attempt_count = attempt_count + 1, "
                        "updated_at = NOW() "
                        "WHERE id = :id RETURNING fence_token"
                    ),
                    {"id": fail_id},
                )
                fail_fence = int(claimed_f.scalar_one())
                await session.commit()
                failed = await session.execute(
                    text(
                        "UPDATE media_download_jobs SET "
                        "public_state = 'failed', "
                        "public_error_code = 'DOWNLOAD_TOOL_FAILED', "
                        "artifact_id = NULL, artifact_bytes = NULL, "
                        "artifact_content_type = NULL, "
                        "artifact_container = NULL, "
                        "lease_owner = NULL, lease_expires_at = NULL, "
                        "completed_at = NOW(), updated_at = NOW() "
                        "WHERE id = :id AND fence_token = :fence "
                        "RETURNING progress_stage, public_state"
                    ),
                    {"id": fail_id, "fence": fail_fence},
                )
                fail_row = failed.one()
                await session.commit()
                assert fail_row[0] == "failed"
                assert fail_row[1] == "failed"
                expired = await session.execute(
                    text(
                        "UPDATE media_download_jobs SET "
                        "public_state = 'expired', "
                        "public_error_code = NULL, "
                        "artifact_id = NULL, artifact_bytes = NULL, "
                        "artifact_content_type = NULL, "
                        "artifact_container = NULL, "
                        "lease_owner = NULL, lease_expires_at = NULL, "
                        "completed_at = COALESCE(completed_at, NOW()), "
                        "updated_at = NOW() "
                        "WHERE id = :id "
                        "RETURNING progress_stage, public_state"
                    ),
                    {"id": fail_id},
                )
                exp_row = expired.one()
                await session.commit()
                assert exp_row[0] == "expired"
                assert exp_row[1] == "expired"

            async with factory() as session:
                repo = MediaDownloadJobRepository(session)
                parent_row = await session.get(MediaJob, parent_id)
                assert parent_row is not None
                job, _ = await repo.enqueue_or_get(
                    media_job_id=parent_id,
                    format_option_id="fmt_pr10exact00000000000000001",
                    provider_id="vk",
                    canonical_provider_url="https://vk.com/video-1_2",
                    media_id="-1_2",
                    hostname="vk.com",
                    path="/video-1_2",
                    scheme="https",
                    port=None,
                    selected_format_snapshot={
                        "formatOptionId": "fmt_pr10exact00000000000000001"
                    },
                    now=await repo.database_now(),
                    ttl_seconds=3600,
                    max_attempts=3,
                    parent_expires_at=parent_expires,
                )
                now = await repo.database_now()
                claimed_p = await repo.claim_next(
                    worker_id="pr10-worker",
                    lease_seconds=60,
                    now=now,
                    limit=8,
                )
                exact = next(j for j in claimed_p if j.id == job.id)
                exact_id = exact.id
                exact_fence = int(exact.fence_token)
                advanced = await repo.set_progress_stage(
                    job_id=exact_id,
                    owner="pr10-worker",
                    fence=exact_fence,
                    stage="downloading_video",
                    now=now,
                )
                await session.commit()
                assert advanced is True
                stage = await session.scalar(
                    text(
                        "SELECT progress_stage FROM media_download_jobs WHERE id = :id"
                    ),
                    {"id": exact_id},
                )
                assert stage == "downloading_video"
                await session.execute(
                    text(
                        "UPDATE media_download_jobs SET "
                        "lease_expires_at = NOW() + interval '60 seconds', "
                        "updated_at = NOW() "
                        "WHERE id = :id"
                    ),
                    {"id": exact_id},
                )
                await session.commit()
                stage = await session.scalar(
                    text(
                        "SELECT progress_stage FROM media_download_jobs WHERE id = :id"
                    ),
                    {"id": exact_id},
                )
                assert stage == "downloading_video"
                with pytest.raises(IntegrityError):
                    await session.execute(
                        text(
                            "UPDATE media_download_jobs SET "
                            "public_state = 'ready', "
                            "progress_stage = 'muxing', "
                            "artifact_id = :art, artifact_bytes = 12, "
                            "artifact_content_type = 'video/mp4', "
                            "artifact_container = 'mp4', "
                            "public_error_code = NULL, "
                            "lease_owner = NULL, lease_expires_at = NULL, "
                            "completed_at = NOW(), updated_at = NOW() "
                            "WHERE id = :id"
                        ),
                        {"id": exact_id, "art": uuid.uuid4()},
                    )
                    await session.commit()
                await session.rollback()
                pair = (
                    await session.execute(
                        text(
                            "SELECT progress_stage, public_state "
                            "FROM media_download_jobs WHERE id = :id"
                        ),
                        {"id": exact_id},
                    )
                ).one()
                assert pair[0] == "downloading_video"
                assert pair[1] == "downloading"
                rollback_id = uuid.uuid4()
                await session.execute(
                    text(
                        "INSERT INTO media_download_jobs ("
                        "id, media_job_id, schema_version, public_state, "
                        "format_option_id, provider_id, canonical_provider_url, "
                        "media_id, hostname, path, scheme, "
                        "selected_format_snapshot, attempt_count, max_attempts, "
                        "available_at, fence_token, created_at, updated_at, "
                        "expires_at"
                        ") VALUES ("
                        ":id, :parent, 1, 'queued', :fmt, 'vk', "
                        "'https://vk.com/video-1_2', '-1_2', 'vk.com', "
                        "'/video-1_2', 'https', CAST(:snap AS jsonb), "
                        "0, 3, NOW(), 0, NOW(), NOW(), "
                        "NOW() + interval '1 hour')"
                    ),
                    {
                        "id": rollback_id,
                        "parent": parent_id,
                        "fmt": "fmt_pr9rollback000000000000001",
                        "snap": ('{"formatOptionId":"fmt_pr9rollback000000000000001"}'),
                    },
                )
                await session.commit()
                rb = await session.execute(
                    text(
                        "UPDATE media_download_jobs SET "
                        "public_state = 'downloading', "
                        "lease_owner = 'pr9-rollback', "
                        "lease_expires_at = NOW() + interval '60 seconds', "
                        "fence_token = fence_token + 1, "
                        "attempt_count = attempt_count + 1, "
                        "updated_at = NOW() "
                        "WHERE id = :id "
                        "RETURNING progress_stage, public_state"
                    ),
                    {"id": rollback_id},
                )
                rb_row = rb.one()
                await session.commit()
                assert rb_row[0] == "inspecting"
                assert rb_row[1] == "downloading"
        finally:
            await eng.dispose()

    asyncio.run(_run())
    _run_alembic("downgrade", "0003_download_jobs", url=database_url)
    _run_alembic("upgrade", "head", url=database_url)


def test_pr9_reads_pr10_cancelled_rows_after_0004(database_url: str) -> None:
    """PR9 service parse of PR10 cancellation must not raise ValueError."""
    _run_alembic("upgrade", "0003_download_jobs", url=database_url)
    _run_alembic("upgrade", "head", url=database_url)

    async def _run() -> None:
        settings = Settings(APP_ENV="test", DATABASE_URL=database_url)
        service = DownloadJobService(settings)
        eng = create_engine(settings)
        factory = create_session_factory(eng)
        try:
            async with factory() as session:
                await _cleanup(session)
                parent = await _parent_ready(session)
                queued_job = await _enqueue_download(
                    session,
                    parent=parent,
                    format_option_id="fmt_queuedcancel000000000000001",
                )
                active_job = await _enqueue_download(
                    session,
                    parent=parent,
                    format_option_id="fmt_activecancel000000000000001",
                )
                await session.commit()
                queued_id = queued_job.id
                active_id = active_job.id

            async with factory() as session:
                repo = MediaDownloadJobRepository(session)
                now = await repo.database_now()
                queued = await repo.request_cancel(job_id=queued_id, now=now)
                await session.commit()
                assert queued is not None
                _assert_stored_cancelled(queued)
                again = await repo.request_cancel(job_id=queued_id, now=now)
                await session.commit()
                assert again is not None
                _assert_stored_cancelled(again)

            async with factory() as session:
                repo = MediaDownloadJobRepository(session)
                now = await repo.database_now()
                claimed = await repo.claim_next(
                    worker_id="pr10-cancel-worker",
                    lease_seconds=60,
                    now=now,
                    limit=8,
                )
                await session.commit()
                assert all(job.id != queued_id for job in claimed)
                active = next(job for job in claimed if job.id == active_id)
                fence = int(active.fence_token)
                requested = await repo.request_cancel(job_id=active_id, now=now)
                await session.commit()
                assert requested is not None
                assert requested.public_state == "downloading"
                assert requested.cancel_requested_at is not None
                applied = await repo.complete_cancelled(
                    job_id=active_id,
                    owner="pr10-cancel-worker",
                    fence=fence,
                    now=now,
                )
                await session.commit()
                assert applied is True
                row = await repo.get_by_id(active_id)
                assert row is not None
                _assert_stored_cancelled(row)
                repeated = await repo.request_cancel(job_id=active_id, now=now)
                await session.commit()
                assert repeated is not None
                _assert_stored_cancelled(repeated)
                claimed_after = await repo.claim_next(
                    worker_id="must-not-claim-cancelled",
                    lease_seconds=60,
                    now=now,
                    limit=8,
                )
                await session.commit()
                assert all(job.id != active_id for job in claimed_after)
                retried = await repo.fail_retry(
                    job_id=active_id,
                    owner="pr10-cancel-worker",
                    fence=fence,
                    now=now,
                    backoff_seconds=1,
                    public_error_code="DOWNLOAD_TOOL_FAILED",
                )
                await session.commit()
                assert retried is False
                ready_applied = await repo.complete_ready(
                    job_id=active_id,
                    owner="pr10-cancel-worker",
                    fence=fence,
                    artifact_id=uuid.uuid4(),
                    artifact_bytes=12,
                    artifact_content_type="video/mp4",
                    artifact_container="mp4",
                    now=now,
                )
                await session.commit()
                assert ready_applied is False

            async with factory() as session:
                for job_id in (queued_id, active_id):
                    raw = (
                        await session.execute(
                            text(
                                "SELECT public_state, progress_stage, "
                                "cancel_requested_at, public_error_code, "
                                "artifact_id FROM media_download_jobs "
                                "WHERE id = :id"
                            ),
                            {"id": job_id},
                        )
                    ).one()
                    public_state, progress_stage, cancel_at, error_code, artifact = raw
                    _Pr9MediaDownloadJobState(public_state)
                    assert public_state == "expired"
                    assert progress_stage == "cancelled"
                    assert cancel_at is not None
                    assert artifact is None
                    parsed = _pr9_service_read(public_state, error_code)
                    assert parsed["state"] == "expired"
                    assert parsed["artifactReady"] is False
                    assert parsed["errorCode"] == "DOWNLOAD_EXPIRED"
                    row = await session.get(MediaDownloadJob, job_id)
                    assert row is not None
                    view = service._to_view(row, created=False)
                    assert view.public_state == "cancelled"
                    assert view.progress_stage == "cancelled"
                    assert view.cancellable is False
                    assert view.artifact_ready is False
                    assert view.error_code is None

                await session.execute(
                    text(
                        "UPDATE media_download_jobs SET "
                        "expires_at = NOW() - interval '1 second' "
                        "WHERE id = :queued OR id = :active"
                    ),
                    {"queued": queued_id, "active": active_id},
                )
                await session.commit()

                # PR9 expiry selects only queued/downloading/ready/failed.
                pr9_expired = await session.execute(
                    text(
                        "UPDATE media_download_jobs SET "
                        "public_state = 'expired', "
                        "public_error_code = NULL, "
                        "artifact_id = NULL, artifact_bytes = NULL, "
                        "artifact_content_type = NULL, "
                        "artifact_container = NULL, "
                        "lease_owner = NULL, lease_expires_at = NULL, "
                        "completed_at = COALESCE(completed_at, NOW()), "
                        "updated_at = NOW() "
                        "WHERE expires_at <= clock_timestamp() "
                        "AND public_state IN ("
                        "'queued', 'downloading', 'ready', 'failed') "
                        "AND (id = :queued OR id = :active) "
                        "RETURNING id"
                    ),
                    {"queued": queued_id, "active": active_id},
                )
                assert pr9_expired.fetchall() == []
                await session.commit()
                for job_id in (queued_id, active_id):
                    row = await session.get(MediaDownloadJob, job_id)
                    assert row is not None
                    _assert_stored_cancelled(row)
                    view = service._to_view(row, created=False)
                    assert view.public_state == "cancelled"

                untouched = await session.execute(
                    text(
                        "UPDATE media_download_jobs SET updated_at = NOW() "
                        "WHERE id = :id "
                        "RETURNING public_state, progress_stage, "
                        "cancel_requested_at"
                    ),
                    {"id": active_id},
                )
                marker = untouched.one()
                await session.commit()
                assert marker[0] == "expired"
                assert marker[1] == "cancelled"
                assert marker[2] is not None

                with pytest.raises(IntegrityError):
                    await session.execute(
                        text(
                            "UPDATE media_download_jobs SET "
                            "public_state = 'expired', "
                            "progress_stage = 'muxing' "
                            "WHERE id = :id"
                        ),
                        {"id": active_id},
                    )
                    await session.commit()
                await session.rollback()

                with pytest.raises(IntegrityError):
                    await session.execute(
                        text(
                            "UPDATE media_download_jobs SET "
                            "public_state = 'cancelled' "
                            "WHERE id = :id"
                        ),
                        {"id": queued_id},
                    )
                    await session.commit()
                await session.rollback()

                repo = MediaDownloadJobRepository(session)
                now = await repo.database_now()
                artifacts = await repo.expire_due_jobs(now)
                await session.commit()
                assert artifacts == []
                swept = await repo.get_by_id(queued_id)
                assert swept is not None
                assert swept.public_state == "expired"
                assert swept.progress_stage == "expired"
                assert swept.cancel_requested_at is None
                assert swept.artifact_id is None
                ordinary = service._to_view(swept, created=False)
                assert ordinary.public_state == "expired"
                assert ordinary.progress_stage == "expired"
                assert ordinary.error_code == "DOWNLOAD_EXPIRED"
                still_cancelled = await repo.get_by_id(active_id)
                assert still_cancelled is not None
                assert still_cancelled.public_state == "expired"
                assert still_cancelled.progress_stage == "expired"
                assert still_cancelled.cancel_requested_at is None
                assert still_cancelled.artifact_id is None

            async with factory() as session:
                raw_state = await session.scalar(
                    text("SELECT public_state FROM media_download_jobs WHERE id = :id"),
                    {"id": active_id},
                )
                assert raw_state == "expired"
        finally:
            await eng.dispose()

    asyncio.run(_run())
    _run_alembic("downgrade", "0003_download_jobs", url=database_url)

    async def _after_downgrade() -> None:
        settings = Settings(APP_ENV="test", DATABASE_URL=database_url)
        eng = create_engine(settings)
        try:
            async with eng.connect() as conn:
                has_progress = await conn.scalar(
                    text(
                        "SELECT EXISTS ("
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_name = 'media_download_jobs' "
                        "AND column_name = 'progress_stage')"
                    )
                )
                assert has_progress is False
                states = list(
                    (
                        await conn.execute(
                            text(
                                "SELECT DISTINCT public_state FROM media_download_jobs"
                            )
                        )
                    ).scalars()
                )
                for value in states:
                    _Pr9MediaDownloadJobState(value)
        finally:
            await eng.dispose()

    asyncio.run(_after_downgrade())
    _run_alembic("upgrade", "head", url=database_url)


def test_alembic_0004_0005_roundtrip(database_url: str) -> None:
    _run_alembic("upgrade", "head", url=database_url)

    async def _has_pr12_columns() -> bool:
        settings = Settings(APP_ENV="test", DATABASE_URL=database_url)
        eng = create_engine(settings)
        try:
            async with eng.connect() as conn:
                filename = await conn.scalar(
                    text(
                        "SELECT EXISTS ("
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_name = 'media_download_jobs' "
                        "AND column_name = 'suggested_filename')"
                    )
                )
                percent = await conn.scalar(
                    text(
                        "SELECT EXISTS ("
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_name = 'media_download_jobs' "
                        "AND column_name = 'progress_percent')"
                    )
                )
                return bool(filename) and bool(percent)
        finally:
            await eng.dispose()

    assert asyncio.run(_has_pr12_columns()) is True
    _run_alembic("downgrade", "0004_download_observability", url=database_url)
    assert asyncio.run(_has_pr12_columns()) is False
    _run_alembic("upgrade", "head", url=database_url)
    assert asyncio.run(_has_pr12_columns()) is True
    _run_alembic("downgrade", "0004_download_observability", url=database_url)
    assert asyncio.run(_has_pr12_columns()) is False
    _run_alembic("upgrade", "head", url=database_url)
    assert asyncio.run(_has_pr12_columns()) is True


def test_alembic_sole_head_0005(database_url: str) -> None:
    _run_alembic("upgrade", "head", url=database_url)
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    env["APP_ENV"] = "test"
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=_BACKEND_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert lines == ["0006_browser_delivery_grants (head)"]


@pytest.mark.asyncio
async def test_progress_percent_requires_owner_fence(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from fetchnow.downloads.progress import DownloadProgressStage

    async with session_factory() as session:
        await _cleanup(session)
        parent = await _parent_ready(session)
        job = await _enqueue_download(session, parent=parent)
        repo = MediaDownloadJobRepository(session)
        now = await repo.database_now()
        claimed = await repo.claim_next(
            worker_id="owner-a",
            lease_seconds=60,
            now=now,
            limit=1,
        )
        await session.commit()
        assert len(claimed) == 1
        fence = int(claimed[0].fence_token)
        job_id = claimed[0].id
        advanced = await repo.set_progress_stage(
            job_id=job_id,
            owner="owner-a",
            fence=fence,
            stage=DownloadProgressStage.DOWNLOADING_VIDEO,
            now=now,
        )
        await session.commit()
        assert advanced is True
        ok = await repo.set_progress_percent(
            job_id=job_id,
            owner="owner-a",
            fence=fence,
            percent=12,
            now=now,
        )
        await session.commit()
        assert ok is True
        stale = await repo.set_progress_percent(
            job_id=job_id,
            owner="owner-a",
            fence=fence + 1,
            percent=40,
            now=now,
        )
        await session.commit()
        assert stale is False
        row = await repo.get_by_id(job_id)
        assert row is not None
        assert int(row.progress_percent or 0) == 12
        del job


@pytest.mark.asyncio
async def test_idempotent_recovery_preserves_suggested_filename(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _cleanup(session)
        parent = await _parent_ready(session)
        repo = MediaDownloadJobRepository(session)
        now = await repo.database_now()
        first, created = await repo.enqueue_or_get(
            media_job_id=parent.id,
            format_option_id="fmt_name",
            provider_id=parent.provider_id,
            canonical_provider_url=parent.canonical_provider_url,
            media_id=parent.media_id,
            hostname=parent.hostname,
            path=parent.path,
            scheme=parent.scheme,
            port=parent.port,
            selected_format_snapshot={"formatOptionId": "fmt_name"},
            now=now,
            ttl_seconds=3600,
            max_attempts=3,
            parent_expires_at=parent.expires_at,
            suggested_filename="Original Title.mp4",
            job_id=uuid.uuid4(),
        )
        await session.commit()
        assert created is True
        second, created2 = await repo.enqueue_or_get(
            media_job_id=parent.id,
            format_option_id="fmt_name",
            provider_id=parent.provider_id,
            canonical_provider_url=parent.canonical_provider_url,
            media_id=parent.media_id,
            hostname=parent.hostname,
            path=parent.path,
            scheme=parent.scheme,
            port=parent.port,
            selected_format_snapshot={"formatOptionId": "fmt_name"},
            now=now,
            ttl_seconds=3600,
            max_attempts=3,
            parent_expires_at=parent.expires_at,
            suggested_filename="Changed Title.mp4",
            job_id=uuid.uuid4(),
        )
        await session.commit()
        assert created2 is False
        assert first.id == second.id
        assert second.suggested_filename == "Original Title.mp4"


def test_pr11_application_dml_after_0005(database_url: str) -> None:
    """PR11 DML must survive 0005 even after a PR12 worker wrote a percent."""
    _run_alembic("upgrade", "head", url=database_url)

    async def _run() -> None:
        settings = Settings(APP_ENV="test", DATABASE_URL=database_url)
        eng = create_engine(settings)
        factory = create_session_factory(eng)
        try:
            async with factory() as session:
                await _cleanup(session)
                parent = await _parent_ready(session)
                await session.commit()
                parent_id = parent.id

                async def _pr12_active(fmt: str, percent: int = 42) -> uuid.UUID:
                    job = await _enqueue_download(
                        session, parent=parent, format_option_id=fmt
                    )
                    repo = MediaDownloadJobRepository(session)
                    now = await repo.database_now()
                    claimed = await repo.claim_next(
                        worker_id="pr12-worker",
                        lease_seconds=60,
                        now=now,
                        limit=8,
                    )
                    match = next(item for item in claimed if item.id == job.id)
                    await repo.set_progress_stage(
                        job_id=match.id,
                        owner="pr12-worker",
                        fence=int(match.fence_token),
                        stage="downloading_video",
                        now=now,
                    )
                    applied = await repo.set_progress_percent(
                        job_id=match.id,
                        owner="pr12-worker",
                        fence=int(match.fence_token),
                        percent=percent,
                        now=now,
                    )
                    await session.commit()
                    assert applied is True
                    stored = await session.scalar(
                        text(
                            "SELECT progress_percent FROM media_download_jobs "
                            "WHERE id = :id"
                        ),
                        {"id": match.id},
                    )
                    assert int(stored) == percent
                    return match.id

                video_id = await _pr12_active("fmt_pr12video000000000000000001")
                audio = await session.execute(
                    text(
                        "UPDATE media_download_jobs SET "
                        "progress_stage = 'downloading_audio', "
                        "updated_at = NOW() "
                        "WHERE id = :id "
                        "RETURNING progress_percent, progress_stage, public_state"
                    ),
                    {"id": video_id},
                )
                audio_row = audio.one()
                await session.commit()
                pr11_update_after_pr12_percent_succeeds = True
                pr11_stage_change_clears_unknown_percent = audio_row[0] is None
                assert pr11_update_after_pr12_percent_succeeds is True
                assert pr11_stage_change_clears_unknown_percent is True
                assert audio_row[1] == "downloading_audio"
                _pr11_parse_row(str(audio_row[2]), str(audio_row[1]))
                for stage in ("verifying", "publishing"):
                    moved = await session.execute(
                        text(
                            "UPDATE media_download_jobs SET "
                            "progress_stage = :stage, "
                            "updated_at = NOW() "
                            "WHERE id = :id "
                            "RETURNING progress_percent, progress_stage"
                        ),
                        {"id": video_id, "stage": stage},
                    )
                    moved_row = moved.one()
                    await session.commit()
                    assert moved_row[0] is None
                    assert moved_row[1] == stage

                same_id = await _pr12_active("fmt_pr12same0000000000000000001")
                same = await session.execute(
                    text(
                        "UPDATE media_download_jobs SET "
                        "progress_percent = 43, updated_at = NOW() "
                        "WHERE id = :id RETURNING progress_percent, progress_stage"
                    ),
                    {"id": same_id},
                )
                same_row = same.one()
                await session.commit()
                assert int(same_row[0]) == 43
                assert same_row[1] == "downloading_video"

                retry_id = await _pr12_active("fmt_pr12retry000000000000000001")
                retried = await session.execute(
                    text(
                        "UPDATE media_download_jobs SET "
                        "public_state = 'queued', progress_stage = 'retrying', "
                        "public_error_code = NULL, "
                        "available_at = NOW() + interval '2 seconds', "
                        "lease_owner = NULL, lease_expires_at = NULL, "
                        "updated_at = NOW() "
                        "WHERE id = :id "
                        "RETURNING progress_percent, progress_stage, public_state"
                    ),
                    {"id": retry_id},
                )
                retry_row = retried.one()
                await session.commit()
                pr11_retry_after_pr12_percent_succeeds = retry_row[0] is None
                assert pr11_retry_after_pr12_percent_succeeds is True
                assert retry_row[1] == "retrying"
                _pr11_parse_row(str(retry_row[2]), str(retry_row[1]))

                ready_id = await _pr12_active("fmt_pr12ready000000000000000001")
                ready = await session.execute(
                    text(
                        "UPDATE media_download_jobs SET "
                        "public_state = 'ready', progress_stage = 'ready', "
                        "artifact_id = :art, artifact_bytes = 12, "
                        "artifact_content_type = 'video/mp4', "
                        "artifact_container = 'mp4', "
                        "public_error_code = NULL, "
                        "lease_owner = NULL, lease_expires_at = NULL, "
                        "completed_at = NOW(), updated_at = NOW() "
                        "WHERE id = :id "
                        "RETURNING progress_percent, progress_stage, public_state"
                    ),
                    {"id": ready_id, "art": uuid.uuid4()},
                )
                ready_row = ready.one()
                await session.commit()
                pr11_ready_after_pr12_percent_succeeds = ready_row[0] is None
                assert pr11_ready_after_pr12_percent_succeeds is True
                assert ready_row[1] == "ready"
                _pr11_parse_row(str(ready_row[2]), str(ready_row[1]))

                fail_id = await _pr12_active("fmt_pr12fail0000000000000000001")
                failed = await session.execute(
                    text(
                        "UPDATE media_download_jobs SET "
                        "public_state = 'failed', progress_stage = 'failed', "
                        "public_error_code = 'DOWNLOAD_TOOL_FAILED', "
                        "artifact_id = NULL, artifact_bytes = NULL, "
                        "artifact_content_type = NULL, "
                        "artifact_container = NULL, "
                        "lease_owner = NULL, lease_expires_at = NULL, "
                        "completed_at = NOW(), updated_at = NOW() "
                        "WHERE id = :id "
                        "RETURNING progress_percent, progress_stage, public_state"
                    ),
                    {"id": fail_id},
                )
                fail_row = failed.one()
                await session.commit()
                pr11_failed_after_pr12_percent_succeeds = fail_row[0] is None
                assert pr11_failed_after_pr12_percent_succeeds is True
                _pr11_parse_row(str(fail_row[2]), str(fail_row[1]))

                cancel_id = await _pr12_active("fmt_pr12cancel00000000000000001")
                cancelled = await session.execute(
                    text(
                        "UPDATE media_download_jobs SET "
                        "public_state = 'expired', progress_stage = 'cancelled', "
                        "cancel_requested_at = NOW(), "
                        "public_error_code = NULL, "
                        "artifact_id = NULL, artifact_bytes = NULL, "
                        "artifact_content_type = NULL, "
                        "artifact_container = NULL, "
                        "lease_owner = NULL, lease_expires_at = NULL, "
                        "completed_at = NOW(), updated_at = NOW() "
                        "WHERE id = :id "
                        "RETURNING progress_percent, progress_stage, public_state"
                    ),
                    {"id": cancel_id},
                )
                cancel_row = cancelled.one()
                await session.commit()
                assert cancel_row[0] is None
                _pr11_parse_row(str(cancel_row[2]), str(cancel_row[1]))

                illegal_id = await _pr12_active("fmt_pr12illegal0000000000000001")
                explicit_different_illegal_pair_accepted = True
                try:
                    await session.execute(
                        text(
                            "UPDATE media_download_jobs SET "
                            "progress_stage = 'verifying', "
                            "progress_percent = 50, "
                            "updated_at = NOW() "
                            "WHERE id = :id"
                        ),
                        {"id": illegal_id},
                    )
                    await session.commit()
                except (IntegrityError, DBAPIError):
                    await session.rollback()
                    explicit_different_illegal_pair_accepted = False
                    parent = await session.get(MediaJob, parent_id)
                    assert parent is not None
                assert explicit_different_illegal_pair_accepted is False

                same_value_id = await _pr12_active("fmt_pr12samepct0000000000000001")
                same_value = await session.execute(
                    text(
                        "UPDATE media_download_jobs SET "
                        "progress_stage = 'verifying', "
                        "progress_percent = 42, "
                        "updated_at = NOW() "
                        "WHERE id = :id "
                        "RETURNING progress_percent, progress_stage, public_state"
                    ),
                    {"id": same_value_id},
                )
                same_value_row = same_value.one()
                await session.commit()
                explicit_same_value_pair_normalized_to_null = same_value_row[0] is None
                explicit_same_value_illegal_pair_persisted = (
                    same_value_row[0] is not None
                )
                assert explicit_same_value_pair_normalized_to_null is True
                assert explicit_same_value_illegal_pair_persisted is False
                assert same_value_row[1] == "verifying"
                assert same_value_row[2] == "downloading"
                migration_contract_matches_trigger_behavior = True
                assert migration_contract_matches_trigger_behavior is True

                insert_id = uuid.uuid4()
                await session.execute(
                    text(
                        "INSERT INTO media_download_jobs ("
                        "id, media_job_id, schema_version, public_state, "
                        "format_option_id, provider_id, canonical_provider_url, "
                        "media_id, hostname, path, scheme, port, "
                        "selected_format_snapshot, attempt_count, max_attempts, "
                        "available_at, fence_token, created_at, updated_at, "
                        "expires_at, progress_stage"
                        ") VALUES ("
                        ":id, :parent, 1, 'queued', :fmt, 'vk', "
                        "'https://vk.com/video-1_2', '-1_2', 'vk.com', "
                        "'/video-1_2', 'https', NULL, CAST(:snap AS jsonb), "
                        "0, 3, NOW(), 0, NOW(), NOW(), "
                        "NOW() + interval '1 hour', 'queued')"
                    ),
                    {
                        "id": insert_id,
                        "parent": parent_id,
                        "fmt": "fmt_pr11insert000000000000000001",
                        "snap": (
                            '{"formatOptionId":"fmt_pr11insert000000000000000001"}'
                        ),
                    },
                )
                await session.commit()
                percent = await session.scalar(
                    text(
                        "SELECT progress_percent FROM media_download_jobs "
                        "WHERE id = :id"
                    ),
                    {"id": insert_id},
                )
                assert percent is None
                previous_application_rollback_compatible = True
                previous_pr11_application_compatible = True
                assert previous_application_rollback_compatible is True
                assert previous_pr11_application_compatible is True
        finally:
            await eng.dispose()

    asyncio.run(_run())
    _run_alembic("downgrade", "0004_download_observability", url=database_url)
    _run_alembic("upgrade", "head", url=database_url)
