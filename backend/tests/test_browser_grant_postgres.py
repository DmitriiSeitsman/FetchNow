"""PostgreSQL tests for browser delivery grants (PR14).

Skipped unless FETCHNOW_TEST_DATABASE_URL is set (asyncpg URL).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fetchnow.core.config import Settings
from fetchnow.db.session import create_engine, create_session_factory
from fetchnow.delivery.service import DeliveryService
from fetchnow.downloads.browser_grant_tokens import (
    generate_grant_token,
    hash_grant_token,
)
from fetchnow.downloads.errors import DownloadError, DownloadErrorCode
from fetchnow.downloads.grant_repository import BrowserGrantRepository
from fetchnow.downloads.grant_service import BrowserGrantService
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
    await session.execute(text("DELETE FROM media_delivery_grants"))
    await session.execute(text("DELETE FROM media_download_jobs"))
    await session.execute(text("DELETE FROM media_jobs"))
    await session.commit()


def _settings(url: str, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "APP_ENV": "test",
        "DATABASE_URL": url,
        "MEDIA_DOWNLOADS_ENABLED": True,
        "MEDIA_BROWSER_DELIVERY_ENABLED": True,
        "MEDIA_BROWSER_GRANT_TTL_SECONDS": 300,
        "MEDIA_BROWSER_GRANT_MAX_ACTIVE": 2,
        "MEDIA_DELIVERY_ENABLED": True,
        "MEDIA_DELIVERY_ROOT": "/tmp/fetchnow-delivery-test",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


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


async def _ready_download(
    session: AsyncSession, *, parent: MediaJob, format_option_id: str = "fmt_grant1"
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
        suggested_filename="clip.mp4",
    )
    claimed = await repo.claim_next(
        worker_id="worker-a",
        lease_seconds=60,
        now=now,
        limit=1,
    )
    assert len(claimed) == 1
    artifact_id = uuid.uuid4()
    applied = await repo.complete_ready(
        job_id=claimed[0].id,
        owner="worker-a",
        fence=int(claimed[0].fence_token),
        artifact_id=artifact_id,
        artifact_bytes=128,
        artifact_container="mp4",
        artifact_content_type="video/mp4",
        now=now,
    )
    assert applied is True
    await session.flush()
    loaded = await repo.get_by_id(claimed[0].id)
    assert loaded is not None
    assert loaded.public_state == "ready"
    return loaded


@pytest.mark.asyncio
async def test_migration_chain_and_roundtrip(database_url: str) -> None:
    _run_alembic("upgrade", "head", url=database_url)
    _run_alembic("downgrade", "0005_download_file_details", url=database_url)
    _run_alembic("upgrade", "0006_browser_delivery_grants", url=database_url)
    _run_alembic("downgrade", "0005_download_file_details", url=database_url)
    _run_alembic("upgrade", "head", url=database_url)


@pytest.mark.asyncio
async def test_issue_persists_hash_not_raw(
    migrated_database: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    settings = _settings(migrated_database)
    async with session_factory() as session:
        await _cleanup(session)
        parent, token = await _parent_with_token(session)
        job = await _ready_download(session, parent=parent)
        service = BrowserGrantService(settings)
        issued = await service.issue(
            download_job_id=job.id,
            access_token=token,
            session=session,
        )
        await session.commit()
        public = service.to_public_dict(issued)
        assert issued.raw_token not in public.values()
        row = (
            await session.execute(
                text(
                    "SELECT token_hash, artifact_id, fence_token FROM "
                    "media_delivery_grants WHERE id = :id"
                ),
                {"id": issued.grant_id},
            )
        ).one()
        assert bytes(row.token_hash) == hash_grant_token(issued.raw_token)
        assert row.artifact_id == job.artifact_id
        assert int(row.fence_token) == int(job.fence_token)


@pytest.mark.asyncio
async def test_concurrent_issuance_respects_cap(
    migrated_database: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    settings = _settings(migrated_database, MEDIA_BROWSER_GRANT_MAX_ACTIVE=2)
    async with session_factory() as session:
        await _cleanup(session)
        parent, token = await _parent_with_token(session)
        job = await _ready_download(session, parent=parent)
        job_id = job.id
        await session.commit()

    async def _issue_one() -> uuid.UUID:
        async with session_factory() as session:
            service = BrowserGrantService(settings)
            issued = await service.issue(
                download_job_id=job_id,
                access_token=token,
                session=session,
            )
            await session.commit()
            return issued.grant_id

    ids = await asyncio.gather(*[_issue_one() for _ in range(5)])
    assert len(set(ids)) == 5
    async with session_factory() as session:
        grants = BrowserGrantRepository(session)
        now = await grants.database_now()
        active = await grants.list_active_for_job(download_job_id=job_id, now=now)
        assert len(active) <= 2


@pytest.mark.asyncio
async def test_authorize_rejects_stale_fence_and_replaced_artifact(
    migrated_database: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    settings = _settings(migrated_database)
    async with session_factory() as session:
        await _cleanup(session)
        parent, token = await _parent_with_token(session)
        job = await _ready_download(session, parent=parent)
        service = BrowserGrantService(settings)
        issued = await service.issue(
            download_job_id=job.id,
            access_token=token,
            session=session,
        )
        await session.commit()

        job.fence_token = int(job.fence_token) + 1
        await session.commit()
        delivery = DeliveryService(settings, reader=MagicMock())
        with pytest.raises(DownloadError) as exc:
            await delivery.authorize_browser_grant(
                grant_id=issued.grant_id,
                raw_token=issued.raw_token,
                session=session,
            )
        assert exc.value.code is DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND

        job.fence_token = int(job.fence_token) - 1
        job.artifact_id = uuid.uuid4()
        await session.commit()
        with pytest.raises(DownloadError) as exc2:
            await delivery.authorize_browser_grant(
                grant_id=issued.grant_id,
                raw_token=issued.raw_token,
                session=session,
            )
        assert exc2.value.code is DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND


@pytest.mark.asyncio
async def test_grant_ttl_clamped_and_cascade(
    migrated_database: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    settings = _settings(migrated_database, MEDIA_BROWSER_GRANT_TTL_SECONDS=3600)
    async with session_factory() as session:
        await _cleanup(session)
        parent, token = await _parent_with_token(session)
        job = await _ready_download(session, parent=parent)
        now = await MediaDownloadJobRepository(session).database_now()
        job.expires_at = now + timedelta(seconds=90)
        await session.commit()

        service = BrowserGrantService(settings)
        issued = await service.issue(
            download_job_id=job.id,
            access_token=token,
            session=session,
        )
        await session.commit()
        assert issued.expires_at <= job.expires_at

        # Force expiry while keeping expires_at > created_at CHECK.
        await session.execute(
            text(
                "UPDATE media_delivery_grants "
                "SET created_at = created_at - interval '10 seconds', "
                "expires_at = created_at - interval '5 seconds' "
                "WHERE id = :id"
            ),
            {"id": issued.grant_id},
        )
        await session.commit()
        delivery = DeliveryService(settings, reader=MagicMock())
        with pytest.raises(DownloadError) as exc:
            await delivery.authorize_browser_grant(
                grant_id=issued.grant_id,
                raw_token=issued.raw_token,
                session=session,
            )
        assert exc.value.code is DownloadErrorCode.DOWNLOAD_EXPIRED

        await session.execute(
            text("DELETE FROM media_download_jobs WHERE id = :id"),
            {"id": job.id},
        )
        await session.commit()
        count = await session.scalar(
            text("SELECT count(*) FROM media_delivery_grants WHERE id = :id"),
            {"id": issued.grant_id},
        )
        assert int(count or 0) == 0


@pytest.mark.asyncio
async def test_token_hash_unique(
    migrated_database: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    settings = _settings(migrated_database)
    async with session_factory() as session:
        await _cleanup(session)
        parent, _token = await _parent_with_token(session)
        job = await _ready_download(session, parent=parent)
        raw = generate_grant_token()
        digest = hash_grant_token(raw)
        grants = BrowserGrantRepository(session)
        now = await grants.database_now()
        await grants.insert_grant(
            grant_id=uuid.uuid4(),
            download_job_id=job.id,
            token_hash=digest,
            artifact_id=job.artifact_id,  # type: ignore[arg-type]
            fence_token=int(job.fence_token),
            created_at=now,
            expires_at=now + timedelta(minutes=5),
        )
        await session.commit()
        with pytest.raises(IntegrityError):
            await grants.insert_grant(
                grant_id=uuid.uuid4(),
                download_job_id=job.id,
                token_hash=digest,
                artifact_id=job.artifact_id,  # type: ignore[arg-type]
                fence_token=int(job.fence_token),
                created_at=now,
                expires_at=now + timedelta(minutes=5),
            )
            await session.commit()
        await session.rollback()
        del settings


@pytest.mark.asyncio
async def test_concurrent_authorize_does_not_serialize_on_grant_row(
    migrated_database: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """GET/HEAD authorization must not take FOR UPDATE on the grant row."""
    settings = _settings(migrated_database)
    async with session_factory() as session:
        await _cleanup(session)
        parent, token = await _parent_with_token(session)
        job = await _ready_download(session, parent=parent)
        service = BrowserGrantService(settings)
        issued = await service.issue(
            download_job_id=job.id,
            access_token=token,
            session=session,
        )
        await session.commit()
        grant_id = issued.grant_id
        raw = issued.raw_token

    # Hold FOR UPDATE on the grant in a separate connection.
    lock_session = session_factory()
    await lock_session.__aenter__()
    await lock_session.execute(
        text("SELECT id FROM media_delivery_grants WHERE id = :id FOR UPDATE"),
        {"id": grant_id},
    )

    delivery = DeliveryService(settings, reader=MagicMock())

    async def _authorize() -> None:
        async with session_factory() as reader:
            await delivery.authorize_browser_grant(
                grant_id=grant_id,
                raw_token=raw,
                session=reader,
            )

    try:
        await asyncio.wait_for(
            asyncio.gather(_authorize(), _authorize(), _authorize()),
            timeout=3.0,
        )
    finally:
        await lock_session.rollback()
        await lock_session.__aexit__(None, None, None)
