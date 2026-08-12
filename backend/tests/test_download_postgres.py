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
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fetchnow.core.config import Settings
from fetchnow.db.session import create_engine, create_session_factory
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
            text(
                "SELECT COUNT(*) FROM media_download_jobs "
                "WHERE media_job_id = :mid"
            ),
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
