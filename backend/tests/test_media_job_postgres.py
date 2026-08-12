"""PostgreSQL integration tests for media-job orchestration.

Skipped unless FETCHNOW_TEST_DATABASE_URL is set (asyncpg URL).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fetchnow.core.config import Settings
from fetchnow.db.session import create_engine, create_session_factory
from fetchnow.jobs.credentials import (
    generate_access_token,
    hash_access_token,
    hash_request_fingerprint,
)
from fetchnow.jobs.errors import JobError, JobErrorCode
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
    # Clear cached settings so alembic env picks up DATABASE_URL.
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


async def _cleanup_jobs(session: AsyncSession) -> None:
    await session.execute(text("DELETE FROM media_jobs"))
    await session.commit()


async def _enqueue_job(
    session: AsyncSession,
    *,
    max_attempts: int = 3,
) -> tuple[MediaJobRepository, MediaJob]:
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
        max_attempts=max_attempts,
    )
    return repo, job


def test_alembic_upgrade_downgrade_upgrade(database_url: str) -> None:
    _run_alembic("upgrade", "head", url=database_url)
    _run_alembic("downgrade", "0001_baseline", url=database_url)

    async def _table_exists() -> bool:
        settings = Settings(APP_ENV="test", DATABASE_URL=database_url)
        eng = create_engine(settings)
        try:
            async with eng.connect() as conn:
                return bool(
                    await conn.scalar(
                        text(
                            "SELECT EXISTS ("
                            "SELECT 1 FROM information_schema.tables "
                            "WHERE table_name = 'media_jobs')"
                        )
                    )
                )
        finally:
            await eng.dispose()

    assert asyncio.run(_table_exists()) is False
    _run_alembic("upgrade", "head", url=database_url)
    assert asyncio.run(_table_exists()) is True


@pytest.mark.asyncio
async def test_schema_indexes_and_unique(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        indexes = {
            row[0]
            for row in (
                await conn.execute(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE tablename = 'media_jobs'"
                    )
                )
            ).all()
        }
        assert "ix_media_jobs_claim" in indexes
        assert "ix_media_jobs_lease" in indexes
        assert "ix_media_jobs_expires_at" in indexes
        constraints = {
            row[0]
            for row in (
                await conn.execute(
                    text(
                        "SELECT constraint_name "
                        "FROM information_schema.table_constraints "
                        "WHERE table_name = 'media_jobs'"
                    )
                )
            ).all()
        }
        assert "uq_media_jobs_credential_hash" in constraints
        assert "ck_media_jobs_public_state" in constraints


@pytest.mark.asyncio
async def test_concurrent_claim_never_double_owns(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _cleanup_jobs(session)
        repo = MediaJobRepository(session)
        now = datetime.now(tz=UTC)
        token = generate_access_token()
        await repo.enqueue_or_get(
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
        await session.commit()

    async def claimer(worker_id: str) -> list[uuid.UUID]:
        async with session_factory() as session:
            repo = MediaJobRepository(session)
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
async def test_enqueue_idempotency_uniqueness(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _cleanup_jobs(session)
        repo = MediaJobRepository(session)
        now = datetime.now(tz=UTC)
        token = generate_access_token()
        cred = hash_access_token(token)
        fp = hash_request_fingerprint(
            access_token=token,
            normalized_request="https://vk.com/video-1_2",
        )
        kwargs = dict(
            credential_hash=cred,
            request_fingerprint=fp,
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
        first, created = await repo.enqueue_or_get(**kwargs)
        assert created is True
        second, created_again = await repo.enqueue_or_get(**kwargs)
        assert created_again is False
        assert first.id == second.id

        with pytest.raises(JobError) as exc_info:
            await repo.enqueue_or_get(
                **{
                    **kwargs,
                    "request_fingerprint": hash_request_fingerprint(
                        access_token=token,
                        normalized_request="https://vk.com/video-9_9",
                    ),
                    "media_id": "-9_9",
                    "canonical_provider_url": "https://vk.com/video-9_9",
                    "path": "/video-9_9",
                }
            )
        assert exc_info.value.code == JobErrorCode.IDEMPOTENCY_CONFLICT
        await session.commit()


@pytest.mark.asyncio
async def test_stale_fence_cannot_complete(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _cleanup_jobs(session)
        repo = MediaJobRepository(session)
        now = datetime.now(tz=UTC)
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
            worker_id="worker-a",
            lease_seconds=60,
            now=now,
            limit=1,
        )
        assert len(claimed) == 1
        fence = int(claimed[0].fence_token)
        await session.commit()

    async with session_factory() as session:
        repo = MediaJobRepository(session)
        job_row = await repo.get_by_id(job.id)
        assert job_row is not None
        job_row.fence_token = fence + 1
        job_row.lease_owner = "worker-b"
        await session.commit()

    async with session_factory() as session:
        repo = MediaJobRepository(session)
        applied = await repo.complete(
            job_id=job.id,
            owner="worker-a",
            fence=fence,
            metadata_json={
                "providerId": "vk",
                "canonicalProviderUrl": "https://vk.com/video-1_2",
                "mediaId": "-1_2",
                "title": None,
                "durationSeconds": 1,
                "formats": [],
                "extractionTool": "ytdlp",
                "extractionToolVersion": None,
                "muxingRequired": False,
            },
            now=datetime.now(tz=UTC),
        )
        assert applied is False
        await session.commit()
        current = await repo.get_by_id(job.id)
        assert current is not None
        assert current.public_state != MediaJobState.INSPECTED.value


@pytest.mark.asyncio
async def test_expired_job_not_claimed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _cleanup_jobs(session)
        repo = MediaJobRepository(session)
        now = datetime.now(tz=UTC)
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
            now=now - timedelta(hours=2),
            ttl_seconds=60,
            max_attempts=3,
        )
        assert job.expires_at <= now
        claimed = await repo.claim_next(
            worker_id="worker-a",
            lease_seconds=60,
            now=now,
            limit=1,
        )
        assert claimed == []
        await session.commit()


@pytest.mark.asyncio
async def test_expired_lease_cannot_complete_or_renew(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _cleanup_jobs(session)
        repo, job = await _enqueue_job(session)
        now = await repo.database_now()
        claimed = await repo.claim_next(
            worker_id="worker-a", lease_seconds=5, now=now, limit=1
        )
        assert len(claimed) == 1
        fence = int(claimed[0].fence_token)
        await session.commit()

    async with session_factory() as session:
        repo = MediaJobRepository(session)
        row = await repo.get_by_id(job.id)
        assert row is not None
        row.lease_expires_at = datetime.now(tz=UTC) - timedelta(seconds=1)
        await session.commit()

    async with session_factory() as session:
        repo = MediaJobRepository(session)
        expired_now = datetime.now(tz=UTC)
        renewed = await repo.renew_lease(
            job_id=job.id,
            owner="worker-a",
            fence=fence,
            lease_seconds=5,
            now=expired_now,
        )
        completed = await repo.complete(
            job_id=job.id,
            owner="worker-a",
            fence=fence,
            metadata_json={"providerId": "vk"},
            now=expired_now,
        )
        assert renewed is False
        assert completed is False
        await session.rollback()


@pytest.mark.asyncio
async def test_live_lease_renews_but_stale_fence_does_not(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _cleanup_jobs(session)
        repo, job = await _enqueue_job(session)
        now = await repo.database_now()
        claimed = await repo.claim_next(
            worker_id="worker-a", lease_seconds=30, now=now, limit=1
        )
        fence = int(claimed[0].fence_token)
        assert await repo.renew_lease(
            job_id=job.id,
            owner="worker-a",
            fence=fence,
            lease_seconds=30,
            now=now + timedelta(seconds=1),
        )
        assert not await repo.renew_lease(
            job_id=job.id,
            owner="worker-a",
            fence=fence - 1,
            lease_seconds=30,
            now=now + timedelta(seconds=1),
        )
        await session.rollback()


@pytest.mark.asyncio
async def test_crash_recovery_stops_at_max_attempts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _cleanup_jobs(session)
        repo, job = await _enqueue_job(session, max_attempts=1)
        now = await repo.database_now()
        claimed = await repo.claim_next(
            worker_id="worker-a", lease_seconds=5, now=now, limit=1
        )
        assert len(claimed) == 1
        await session.commit()

    async with session_factory() as session:
        repo = MediaJobRepository(session)
        row = await repo.get_by_id(job.id)
        assert row is not None
        row.lease_expires_at = datetime.now(tz=UTC) - timedelta(seconds=1)
        await session.commit()

    async with session_factory() as session:
        repo = MediaJobRepository(session)
        reclaimed = await repo.reclaim_expired_leases(
            datetime.now(tz=UTC)
        )
        assert reclaimed == 1
        current = await repo.get_by_id(job.id)
        assert current is not None
        assert current.public_state == MediaJobState.FAILED.value
        assert current.public_error_code == JobErrorCode.MEDIA_INSPECTION_FAILED.value
        assert await repo.claim_next(
            worker_id="worker-b",
            lease_seconds=5,
            now=datetime.now(tz=UTC),
            limit=1,
        ) == []
        await session.commit()
