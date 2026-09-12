"""Real PostgreSQL acceptance tests for successful-delivery quota accounting."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fetchnow.core.config import Settings
from fetchnow.db.session import create_engine, create_session_factory
from fetchnow.downloads.errors import DownloadError
from fetchnow.downloads.models import MediaDownloadJob
from fetchnow.downloads.repository import MediaDownloadJobRepository
from fetchnow.jobs.credentials import (
    generate_access_token,
    hash_access_token,
    hash_request_fingerprint,
)
from fetchnow.jobs.models import MediaJob
from fetchnow.jobs.repository import MediaJobRepository
from fetchnow.quota.delivery import (
    DELIVERY_ATTEMPT_LEASE_SECONDS,
    MAX_ACTIVE_ATTEMPTS,
    DeliveryAccountingAttempt,
    DeliveryAccountingResult,
    DeliveryQuotaAccounting,
)
from fetchnow.quota.errors import QuotaInvariantError
from fetchnow.quota.models import (
    AnonymousClient,
    FreeDownloadDeliveryRange,
    FreeDownloadQuotaEntry,
)
from fetchnow.quota.reconcile import QuotaReconciler
from fetchnow.quota.repository import QuotaRepository
from fetchnow.quota.service import QuotaService
from fetchnow.quota.tokens import generate_anonymous_token, hash_anonymous_token

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
        raise AssertionError(proc.stdout + proc.stderr)


@pytest.fixture(scope="module")
def database_url() -> str:
    return _require_db()


@pytest.fixture(scope="module")
def migrated_database(database_url: str) -> str:
    _run_alembic("upgrade", "head", url=database_url)
    return database_url


@pytest.fixture
async def engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(Settings(APP_ENV="test", DATABASE_URL=migrated_database))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


def _settings(url: str, *, compatibility: bool = False) -> Settings:
    return Settings(
        APP_ENV="test",
        DATABASE_URL=url,
        FREE_DOWNLOAD_QUOTA_ENABLED=True,
        FREE_DOWNLOAD_QUOTA_READY_COMPATIBILITY_MODE=compatibility,
        FREE_DOWNLOAD_LIMIT=3,
        MEDIA_DOWNLOADS_ENABLED=True,
        PROVIDER_VK_ENABLED=True,
    )


async def _cleanup(session: AsyncSession) -> None:
    for table in (
        "premium_entitlements",
        "payment_orders",
        "media_delivery_grants",
        "free_download_delivery_ranges",
        "free_download_quota_entries",
        "anonymous_clients",
        "media_download_jobs",
        "media_jobs",
    ):
        await session.execute(text(f"DELETE FROM {table}"))
    await session.commit()


async def _parent(session: AsyncSession) -> MediaJob:
    repo = MediaJobRepository(session)
    now = await repo.database_now()
    token = generate_access_token()
    row, _ = await repo.enqueue_or_get(
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
    return row


async def _identity(session: AsyncSession) -> AnonymousClient:
    quota = QuotaRepository(session)
    now = await quota.database_now()
    token_hash = hash_anonymous_token(generate_anonymous_token())
    assert token_hash is not None
    return await quota.create_client(
        token_hash=token_hash,
        now=now,
        ttl_seconds=31_536_000,
    )


async def _ready_reserved(
    session: AsyncSession,
    *,
    database_url: str,
    compatibility: bool = False,
    size: int = 10,
) -> tuple[MediaDownloadJob, FreeDownloadQuotaEntry]:
    parent = await _parent(session)
    client = await _identity(session)
    repo = MediaDownloadJobRepository(session)
    now = await repo.database_now()
    job, _ = await repo.enqueue_or_get(
        media_job_id=parent.id,
        format_option_id=f"fmt_{uuid.uuid4().hex[:12]}",
        provider_id=parent.provider_id,
        canonical_provider_url=parent.canonical_provider_url,
        media_id=parent.media_id,
        hostname=parent.hostname,
        path=parent.path,
        scheme=parent.scheme,
        port=parent.port,
        selected_format_snapshot={"formatOptionId": "fmt_test"},
        now=now,
        ttl_seconds=3600,
        max_attempts=3,
        parent_expires_at=parent.expires_at,
    )
    admission = await QuotaService(_settings(database_url)).admit_locked(
        identity_id=client.id,
        download_job_id=job.id,
        reservation_expires_at=job.expires_at,
        session=session,
    )
    assert admission.policy.tier == "free"
    claimed = await repo.claim_next(
        worker_id="delivery-test",
        lease_seconds=120,
        now=now,
        limit=1,
    )
    assert len(claimed) == 1
    artifact_id = uuid.uuid4()
    assert await repo.complete_ready(
        job_id=job.id,
        owner="delivery-test",
        fence=int(claimed[0].fence_token),
        artifact_id=artifact_id,
        artifact_bytes=size,
        artifact_content_type="video/mp4",
        artifact_container="mp4",
        now=now,
        consume_quota_on_ready=compatibility,
    )
    await session.flush()
    refreshed = await session.get(MediaDownloadJob, job.id)
    entry = await session.scalar(
        select(FreeDownloadQuotaEntry).where(
            FreeDownloadQuotaEntry.download_job_id == job.id
        )
    )
    assert refreshed is not None and entry is not None
    return refreshed, entry


async def _authorization(
    session: AsyncSession, job_id: uuid.UUID, *, tier: str = "free"
) -> SimpleNamespace:
    job = await session.get(MediaDownloadJob, job_id)
    assert job is not None
    return SimpleNamespace(job=job, policy=SimpleNamespace(tier=tier))


async def _begin(
    session_factory: async_sessionmaker[AsyncSession],
    accounting: DeliveryQuotaAccounting,
    *,
    job_id: uuid.UUID,
    start: int,
    end: int,
    tier: str = "free",
) -> DeliveryAccountingAttempt | None:
    async with session_factory() as session:
        auth = await _authorization(session, job_id, tier=tier)

        async def revalidate(fresh: AsyncSession) -> SimpleNamespace:
            return await _authorization(fresh, job_id, tier=tier)

        attempt = await accounting.begin(
            authorization=auth,
            start=start,
            end=end,
            session=session,
            revalidate=revalidate,
        )
        await session.commit()
        return attempt


def test_migration_0010_additive_roundtrip(database_url: str) -> None:
    _run_alembic("upgrade", "head", url=database_url)
    _run_alembic("downgrade", "0009_premium_entitlements", url=database_url)

    async def exists() -> bool:
        engine = create_engine(Settings(APP_ENV="test", DATABASE_URL=database_url))
        try:
            async with engine.connect() as connection:
                return bool(
                    await connection.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                            "WHERE table_name = 'free_download_delivery_ranges')"
                        )
                    )
                )
        finally:
            await engine.dispose()

    assert asyncio.run(exists()) is False
    _run_alembic("upgrade", "head", url=database_url)
    assert asyncio.run(exists()) is True


def test_migration_0010_preserves_existing_quota_rows_and_schema_contract(
    database_url: str,
) -> None:
    _run_alembic("upgrade", "head", url=database_url)

    async def seed_existing_rows() -> tuple[uuid.UUID, uuid.UUID]:
        engine = create_engine(Settings(APP_ENV="test", DATABASE_URL=database_url))
        factory = create_session_factory(engine)
        try:
            async with factory() as session:
                await _cleanup(session)
                _, reserved = await _ready_reserved(
                    session,
                    database_url=database_url,
                    compatibility=False,
                )
                _, consumed = await _ready_reserved(
                    session,
                    database_url=database_url,
                    compatibility=True,
                )
                await session.commit()
                return reserved.id, consumed.id
        finally:
            await engine.dispose()

    reserved_id, consumed_id = asyncio.run(seed_existing_rows())
    _run_alembic("downgrade", "0009_premium_entitlements", url=database_url)

    async def inspect_0009() -> None:
        engine = create_engine(Settings(APP_ENV="test", DATABASE_URL=database_url))
        try:
            async with engine.connect() as connection:
                states = dict(
                    (
                        await connection.execute(
                            text(
                                "SELECT id, state FROM free_download_quota_entries "
                                "WHERE id IN (:reserved_id, :consumed_id)"
                            ),
                            {
                                "reserved_id": reserved_id,
                                "consumed_id": consumed_id,
                            },
                        )
                    ).all()
                )
                assert states == {
                    reserved_id: "reserved",
                    consumed_id: "consumed",
                }
                assert not bool(
                    await connection.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                            "WHERE table_schema = 'public' "
                            "AND table_name = 'free_download_delivery_ranges')"
                        )
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(inspect_0009())
    _run_alembic("upgrade", "head", url=database_url)

    async def inspect_0010() -> None:
        engine = create_engine(Settings(APP_ENV="test", DATABASE_URL=database_url))
        try:
            async with engine.connect() as connection:
                states = dict(
                    (
                        await connection.execute(
                            text(
                                "SELECT id, state FROM free_download_quota_entries "
                                "WHERE id IN (:reserved_id, :consumed_id)"
                            ),
                            {
                                "reserved_id": reserved_id,
                                "consumed_id": consumed_id,
                            },
                        )
                    ).all()
                )
                assert states == {
                    reserved_id: "reserved",
                    consumed_id: "consumed",
                }
                columns = set(
                    (
                        await connection.scalars(
                            text(
                                "SELECT column_name FROM information_schema.columns "
                                "WHERE table_schema = 'public' "
                                "AND table_name = 'free_download_delivery_ranges'"
                            )
                        )
                    ).all()
                )
                assert columns == set(
                    FreeDownloadDeliveryRange.__table__.columns.keys()
                )
                constraints = set(
                    (
                        await connection.scalars(
                            text(
                                "SELECT conname FROM pg_constraint "
                                "WHERE conrelid = "
                                "'free_download_delivery_ranges'::regclass"
                            )
                        )
                    ).all()
                )
                assert {
                    "ck_free_delivery_ranges_state",
                    "ck_free_delivery_ranges_artifact_bytes_positive",
                    "ck_free_delivery_ranges_fence_nonneg",
                    "ck_free_delivery_ranges_request",
                    "ck_free_delivery_ranges_served",
                    "ck_free_delivery_ranges_lifecycle",
                } <= constraints
                indexes = set(
                    (
                        await connection.scalars(
                            text(
                                "SELECT indexname FROM pg_indexes "
                                "WHERE schemaname = 'public' "
                                "AND tablename = 'free_download_delivery_ranges'"
                            )
                        )
                    ).all()
                )
                assert {
                    "ix_free_delivery_ranges_entry_state_start",
                    "ix_free_delivery_ranges_active_lease",
                } <= indexes
                timestamp_types = dict(
                    (
                        await connection.execute(
                            text(
                                "SELECT column_name, data_type "
                                "FROM information_schema.columns "
                                "WHERE table_schema = 'public' "
                                "AND table_name = 'free_download_delivery_ranges' "
                                "AND column_name IN "
                                "('started_at', 'lease_expires_at', 'closed_at')"
                            )
                        )
                    ).all()
                )
                assert set(timestamp_types.values()) == {"timestamp with time zone"}
        finally:
            await engine.dispose()

    asyncio.run(inspect_0010())


@pytest.mark.asyncio
async def test_ready_consumption_obeys_compatibility_mode(
    session_factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    async with session_factory() as session:
        await _cleanup(session)
        _, delivery_entry = await _ready_reserved(
            session,
            database_url=database_url,
            compatibility=False,
        )
        assert delivery_entry.state == "reserved"
        await _cleanup(session)
        _, legacy_entry = await _ready_reserved(
            session,
            database_url=database_url,
            compatibility=True,
        )
        assert legacy_entry.state == "consumed"
        await session.commit()


@pytest.mark.asyncio
async def test_partial_resume_union_consumes_once_and_replay_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    async with session_factory() as session:
        await _cleanup(session)
        job, entry = await _ready_reserved(session, database_url=database_url)
        job_id, entry_id = job.id, entry.id
        await session.commit()
    accounting = DeliveryQuotaAccounting(compatibility_mode=False)
    first = await _begin(session_factory, accounting, job_id=job_id, start=0, end=7)
    assert first is not None
    async with session_factory() as session:
        result = await accounting.finalize(
            attempt_id=first.id,
            observed_end=4,
            session=session,
        )
        await session.commit()
    assert result.consumed is False

    second = await _begin(session_factory, accounting, job_id=job_id, start=4, end=10)
    assert second is not None
    async with session_factory() as session:
        result = await accounting.finalize(
            attempt_id=second.id,
            observed_end=10,
            session=session,
        )
        await session.commit()
    assert result.consumed is True
    assert (
        await _begin(session_factory, accounting, job_id=job_id, start=0, end=10)
        is None
    )
    async with session_factory() as session:
        stored = await session.get(FreeDownloadQuotaEntry, entry_id)
        ranges = list(
            (
                await session.scalars(
                    select(FreeDownloadDeliveryRange).where(
                        FreeDownloadDeliveryRange.quota_entry_id == entry_id
                    )
                )
            ).all()
        )
    assert stored is not None and stored.state == "consumed"
    assert [(row.request_start, row.served_end_exclusive) for row in ranges] == [
        (0, 10)
    ]


@pytest.mark.asyncio
async def test_concurrent_full_finalization_consumes_exactly_once(
    session_factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    async with session_factory() as session:
        await _cleanup(session)
        job, entry = await _ready_reserved(session, database_url=database_url)
        job_id, entry_id = job.id, entry.id
        await session.commit()
    accounting = DeliveryQuotaAccounting(compatibility_mode=False)
    attempts = [
        await _begin(session_factory, accounting, job_id=job_id, start=0, end=10)
        for _ in range(2)
    ]
    assert all(attempt is not None for attempt in attempts)

    async def finalize(attempt_id: uuid.UUID) -> DeliveryAccountingResult:
        async with session_factory() as session:
            result = await accounting.finalize(
                attempt_id=attempt_id,
                observed_end=10,
                session=session,
            )
            await session.commit()
            return result

    results = await asyncio.gather(
        *(finalize(attempt.id) for attempt in attempts if attempt is not None)
    )
    assert sum(result.consumed for result in results) == 1
    async with session_factory() as session:
        stored_entry = await session.get(FreeDownloadQuotaEntry, entry_id)
    assert stored_entry is not None and stored_entry.state == "consumed"


@pytest.mark.asyncio
async def test_concurrent_disjoint_ranges_union_without_deadlock(
    session_factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    async with session_factory() as session:
        await _cleanup(session)
        job, entry = await _ready_reserved(session, database_url=database_url)
        job_id, entry_id = job.id, entry.id
        await session.commit()
    accounting = DeliveryQuotaAccounting(compatibility_mode=False)
    attempts = await asyncio.gather(
        _begin(session_factory, accounting, job_id=job_id, start=0, end=5),
        _begin(session_factory, accounting, job_id=job_id, start=5, end=10),
    )
    assert all(attempt is not None for attempt in attempts)

    async def finalize(attempt: DeliveryAccountingAttempt) -> DeliveryAccountingResult:
        async with session_factory() as session:
            result = await accounting.finalize(
                attempt_id=attempt.id,
                observed_end=attempt.end,
                session=session,
            )
            await session.commit()
            return result

    results = await asyncio.wait_for(
        asyncio.gather(
            *(finalize(attempt) for attempt in attempts if attempt is not None)
        ),
        timeout=10,
    )
    assert sum(result.consumed for result in results) == 1
    async with session_factory() as session:
        stored = await session.get(FreeDownloadQuotaEntry, entry_id)
        ranges = list(
            (
                await session.scalars(
                    select(FreeDownloadDeliveryRange).where(
                        FreeDownloadDeliveryRange.quota_entry_id == entry_id
                    )
                )
            ).all()
        )
    assert stored is not None and stored.state == "consumed"
    assert [(row.request_start, row.served_end_exclusive) for row in ranges] == [
        (0, 10)
    ]


@pytest.mark.asyncio
async def test_active_attempt_cap_rejects_before_streaming(
    session_factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    async with session_factory() as session:
        await _cleanup(session)
        job, _ = await _ready_reserved(
            session, database_url=database_url, size=MAX_ACTIVE_ATTEMPTS + 1
        )
        job_id = job.id
        await session.commit()
    accounting = DeliveryQuotaAccounting(compatibility_mode=False)
    results = await asyncio.wait_for(
        asyncio.gather(
            *(
                _begin(
                    session_factory,
                    accounting,
                    job_id=job_id,
                    start=offset,
                    end=offset + 1,
                )
                for offset in range(MAX_ACTIVE_ATTEMPTS + 1)
            ),
            return_exceptions=True,
        ),
        timeout=10,
    )
    assert sum(isinstance(result, DeliveryAccountingAttempt) for result in results) == (
        MAX_ACTIVE_ATTEMPTS
    )
    failures = [result for result in results if isinstance(result, BaseException)]
    assert len(failures) == 1
    assert isinstance(failures[0], DownloadError)


@pytest.mark.asyncio
async def test_live_lease_protects_expired_artifact_then_stale_prefix_closes(
    session_factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    async with session_factory() as session:
        await _cleanup(session)
        job, entry = await _ready_reserved(session, database_url=database_url)
        job_id, entry_id = job.id, entry.id
        await session.commit()
    accounting = DeliveryQuotaAccounting(compatibility_mode=False)
    attempt = await _begin(session_factory, accounting, job_id=job_id, start=0, end=10)
    assert attempt is not None
    async with session_factory() as session:
        await accounting.checkpoint(
            attempt_id=attempt.id,
            observed_end=4,
            session=session,
        )
        now = await QuotaRepository(session).database_now()
        stored_job = await session.get(MediaDownloadJob, job_id)
        assert stored_job is not None
        stored_job.created_at = now - timedelta(hours=2)
        stored_job.expires_at = now - timedelta(seconds=1)
        await session.commit()
    async with session_factory() as session:
        repo = MediaDownloadJobRepository(session)
        now = await repo.database_now()
        assert await repo.expire_due_jobs(now) == []
        await session.commit()
        protected = await session.get(MediaDownloadJob, job_id)
        assert protected is not None and protected.artifact_id is not None

    async with session_factory() as session:
        now = await QuotaRepository(session).database_now()
        row = await session.get(FreeDownloadDeliveryRange, attempt.id)
        assert row is not None
        row.started_at = now - timedelta(seconds=DELIVERY_ATTEMPT_LEASE_SECONDS + 2)
        row.lease_expires_at = now - timedelta(seconds=1)
        await QuotaReconciler(_settings(database_url)).run(session=session)
        await session.commit()
    async with session_factory() as session:
        row = await session.get(FreeDownloadDeliveryRange, attempt.id)
        assert row is not None
        assert row.state == "closed"
        assert row.served_end_exclusive == 4
        stored_entry = await session.get(FreeDownloadQuotaEntry, entry_id)
        assert stored_entry is not None and stored_entry.state == "reserved"
        repo = MediaDownloadJobRepository(session)
        now = await repo.database_now()
        expired = await repo.expire_due_jobs(now)
        await session.commit()
    assert len(expired) == 1
    async with session_factory() as session:
        expired_entry = await session.get(FreeDownloadQuotaEntry, entry_id)
        assert expired_entry is not None and expired_entry.state == "expired"


@pytest.mark.asyncio
async def test_database_rejects_invalid_delivery_ranges(
    session_factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    async with session_factory() as session:
        await _cleanup(session)
        job, entry = await _ready_reserved(session, database_url=database_url)
        now = await QuotaRepository(session).database_now()
        assert job.artifact_id is not None
        invalid = FreeDownloadDeliveryRange(
            id=uuid.uuid4(),
            quota_entry_id=entry.id,
            artifact_id=job.artifact_id,
            fence_token=int(job.fence_token),
            artifact_bytes=10,
            request_start=8,
            request_end_exclusive=4,
            served_end_exclusive=8,
            state="active",
            started_at=now,
            lease_expires_at=now + timedelta(seconds=60),
            closed_at=None,
        )
        session.add(invalid)
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()


@pytest.mark.asyncio
async def test_database_rejects_invalid_delivery_range_lifecycle_combinations(
    session_factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    async with session_factory() as session:
        await _cleanup(session)
        job, entry = await _ready_reserved(session, database_url=database_url)
        now = await QuotaRepository(session).database_now()
        assert job.artifact_id is not None
        entry_id = entry.id
        artifact_id = job.artifact_id
        fence_token = int(job.fence_token)
        await session.commit()

    invalid_overrides = (
        {"state": "unknown"},
        {"fence_token": -1},
        {"artifact_bytes": 0},
        {"served_end_exclusive": 11},
        {"state": "active", "lease_expires_at": None},
        {"state": "active", "lease_expires_at": now},
        {"state": "active", "closed_at": now},
        {"state": "closed", "lease_expires_at": now + timedelta(seconds=60)},
        {"state": "closed", "lease_expires_at": None, "closed_at": None},
        {
            "state": "closed",
            "lease_expires_at": None,
            "closed_at": now - timedelta(seconds=1),
        },
    )
    for overrides in invalid_overrides:
        values = {
            "id": uuid.uuid4(),
            "quota_entry_id": entry_id,
            "artifact_id": artifact_id,
            "fence_token": fence_token,
            "artifact_bytes": 10,
            "request_start": 0,
            "request_end_exclusive": 10,
            "served_end_exclusive": 0,
            "state": "active",
            "started_at": now,
            "lease_expires_at": now + timedelta(seconds=60),
            "closed_at": None,
        }
        values.update(overrides)
        async with session_factory() as session:
            session.add(FreeDownloadDeliveryRange(**values))
            with pytest.raises(IntegrityError):
                await session.flush()
            await session.rollback()


@pytest.mark.asyncio
async def test_delivery_ranges_cascade_with_quota_entry(
    session_factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    async with session_factory() as session:
        await _cleanup(session)
        job, entry = await _ready_reserved(session, database_url=database_url)
        now = await QuotaRepository(session).database_now()
        assert job.artifact_id is not None
        session.add(
            FreeDownloadDeliveryRange(
                id=uuid.uuid4(),
                quota_entry_id=entry.id,
                artifact_id=job.artifact_id,
                fence_token=int(job.fence_token),
                artifact_bytes=10,
                request_start=0,
                request_end_exclusive=5,
                served_end_exclusive=5,
                state="closed",
                started_at=now,
                lease_expires_at=None,
                closed_at=now,
            )
        )
        await session.flush()
        await session.delete(entry)
        await session.flush()
        assert (
            await session.scalar(select(func.count(FreeDownloadDeliveryRange.id))) == 0
        )
        await session.rollback()


@pytest.mark.asyncio
async def test_premium_delivery_does_not_create_free_evidence(
    session_factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    async with session_factory() as session:
        await _cleanup(session)
        job, _ = await _ready_reserved(session, database_url=database_url)
        job_id = job.id
        await session.commit()
    accounting = DeliveryQuotaAccounting(compatibility_mode=False)
    assert (
        await _begin(
            session_factory,
            accounting,
            job_id=job_id,
            start=0,
            end=10,
            tier="premium",
        )
        is None
    )
    async with session_factory() as session:
        assert (
            await session.scalar(select(func.count(FreeDownloadDeliveryRange.id))) == 0
        )


@pytest.mark.asyncio
async def test_quota_disabled_legacy_job_streams_without_required_evidence(
    session_factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    async with session_factory() as session:
        await _cleanup(session)
        parent = await _parent(session)
        repo = MediaDownloadJobRepository(session)
        now = await repo.database_now()
        job, _ = await repo.enqueue_or_get(
            media_job_id=parent.id,
            format_option_id="fmt_quota_disabled",
            provider_id=parent.provider_id,
            canonical_provider_url=parent.canonical_provider_url,
            media_id=parent.media_id,
            hostname=parent.hostname,
            path=parent.path,
            scheme=parent.scheme,
            port=parent.port,
            selected_format_snapshot={"formatOptionId": "fmt_quota_disabled"},
            now=now,
            ttl_seconds=3600,
            max_attempts=3,
            parent_expires_at=parent.expires_at,
        )
        claimed = await repo.claim_next(
            worker_id="delivery-test",
            lease_seconds=120,
            now=now,
            limit=1,
        )
        assert len(claimed) == 1
        assert await repo.complete_ready(
            job_id=job.id,
            owner="delivery-test",
            fence=int(claimed[0].fence_token),
            artifact_id=uuid.uuid4(),
            artifact_bytes=10,
            artifact_content_type="video/mp4",
            artifact_container="mp4",
            now=now,
            consume_quota_on_ready=False,
        )
        job_id = job.id
        await session.commit()
    accounting = DeliveryQuotaAccounting(compatibility_mode=False)
    assert (
        await _begin(session_factory, accounting, job_id=job_id, start=0, end=10)
        is None
    )


@pytest.mark.asyncio
async def test_reconciler_consumes_complete_durable_coverage(
    session_factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    async with session_factory() as session:
        await _cleanup(session)
        job, entry = await _ready_reserved(session, database_url=database_url)
        now = await QuotaRepository(session).database_now()
        assert job.artifact_id is not None
        session.add(
            FreeDownloadDeliveryRange(
                id=uuid.uuid4(),
                quota_entry_id=entry.id,
                artifact_id=job.artifact_id,
                fence_token=int(job.fence_token),
                artifact_bytes=10,
                request_start=0,
                request_end_exclusive=10,
                served_end_exclusive=10,
                state="closed",
                started_at=now,
                lease_expires_at=None,
                closed_at=now,
            )
        )
        result = await QuotaReconciler(_settings(database_url)).run(session=session)
        await session.commit()
    assert result.consumed == 1
    assert entry.state == "consumed"


@pytest.mark.asyncio
async def test_reconciler_fails_closed_for_downloadable_terminal_reservation(
    session_factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    async with session_factory() as session:
        await _cleanup(session)
        _, entry = await _ready_reserved(session, database_url=database_url)
        now = await QuotaRepository(session).database_now()
        entry.state = "released"
        entry.released_at = now
        with pytest.raises(QuotaInvariantError):
            await QuotaReconciler(_settings(database_url)).run(session=session)
        await session.rollback()


@pytest.mark.asyncio
async def test_artifact_generation_mismatch_fails_closed(
    session_factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    async with session_factory() as session:
        await _cleanup(session)
        job, _ = await _ready_reserved(session, database_url=database_url)
        job_id = job.id
        await session.commit()
    accounting = DeliveryQuotaAccounting(compatibility_mode=False)
    attempt = await _begin(session_factory, accounting, job_id=job_id, start=0, end=5)
    assert attempt is not None
    async with session_factory() as session:
        await accounting.finalize(
            attempt_id=attempt.id,
            observed_end=3,
            session=session,
        )
        job = await session.get(MediaDownloadJob, job_id)
        assert job is not None
        job.artifact_id = uuid.uuid4()
        await session.commit()
    with pytest.raises(QuotaInvariantError):
        await _begin(session_factory, accounting, job_id=job_id, start=3, end=10)


@pytest.mark.asyncio
async def test_fragment_bound_compacts_adjacency_and_rejects_new_component(
    session_factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    async with session_factory() as session:
        await _cleanup(session)
        job, entry = await _ready_reserved(
            session,
            database_url=database_url,
            size=130,
        )
        now = await QuotaRepository(session).database_now()
        assert job.artifact_id is not None
        for start in range(0, 128, 2):
            session.add(
                FreeDownloadDeliveryRange(
                    id=uuid.uuid4(),
                    quota_entry_id=entry.id,
                    artifact_id=job.artifact_id,
                    fence_token=int(job.fence_token),
                    artifact_bytes=130,
                    request_start=start,
                    request_end_exclusive=start + 1,
                    served_end_exclusive=start + 1,
                    state="closed",
                    started_at=now,
                    lease_expires_at=None,
                    closed_at=now,
                )
            )
        job_id = job.id
        await session.commit()
    accounting = DeliveryQuotaAccounting(compatibility_mode=False)
    joining = await _begin(session_factory, accounting, job_id=job_id, start=1, end=2)
    assert joining is not None
    async with session_factory() as session:
        await accounting.finalize(
            attempt_id=joining.id,
            observed_end=1,
            session=session,
        )
        await session.commit()
    with pytest.raises(DownloadError):
        await _begin(
            session_factory,
            accounting,
            job_id=job_id,
            start=128,
            end=129,
        )


@pytest.mark.asyncio
async def test_fragment_bound_reserves_concurrent_active_components(
    session_factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    async with session_factory() as session:
        await _cleanup(session)
        job, entry = await _ready_reserved(
            session,
            database_url=database_url,
            size=160,
        )
        now = await QuotaRepository(session).database_now()
        assert job.artifact_id is not None
        for start in range(0, 126, 2):
            session.add(
                FreeDownloadDeliveryRange(
                    id=uuid.uuid4(),
                    quota_entry_id=entry.id,
                    artifact_id=job.artifact_id,
                    fence_token=int(job.fence_token),
                    artifact_bytes=160,
                    request_start=start,
                    request_end_exclusive=start + 1,
                    served_end_exclusive=start + 1,
                    state="closed",
                    started_at=now,
                    lease_expires_at=None,
                    closed_at=now,
                )
            )
        job_id = job.id
        await session.commit()

    accounting = DeliveryQuotaAccounting(compatibility_mode=False)
    results = await asyncio.wait_for(
        asyncio.gather(
            _begin(session_factory, accounting, job_id=job_id, start=126, end=127),
            _begin(session_factory, accounting, job_id=job_id, start=128, end=129),
            return_exceptions=True,
        ),
        timeout=10,
    )
    assert sum(isinstance(result, DeliveryAccountingAttempt) for result in results) == 1
    failures = [result for result in results if isinstance(result, BaseException)]
    assert len(failures) == 1
    assert isinstance(failures[0], DownloadError)
