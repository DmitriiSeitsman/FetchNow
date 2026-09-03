"""PostgreSQL integration and concurrency tests for PRD1E-B2.

Skipped unless FETCHNOW_TEST_DATABASE_URL is set.
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

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fetchnow.core.config import Settings
from fetchnow.db.session import create_engine, create_session_factory
from fetchnow.delivery.service import DeliveryService
from fetchnow.downloads.models import MediaDownloadJob
from fetchnow.downloads.repository import MediaDownloadJobRepository
from fetchnow.downloads.service import DownloadJobService
from fetchnow.downloads.snapshot_codec import decode_effective_policy_snapshot
from fetchnow.jobs.credentials import (
    generate_access_token,
    hash_access_token,
    hash_request_fingerprint,
)
from fetchnow.jobs.models import MediaJob
from fetchnow.jobs.repository import MediaJobRepository
from fetchnow.payments.models import PaymentOrder
from fetchnow.premium.models import PremiumEntitlement
from fetchnow.quota.errors import FreeQuotaExceededError
from fetchnow.quota.models import AnonymousClient, FreeDownloadQuotaEntry
from fetchnow.quota.policy import effective_download_policy
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


def _settings(url: str, *, enabled: bool = True) -> Settings:
    return Settings(
        APP_ENV="test",
        DATABASE_URL=url,
        FREE_DOWNLOAD_QUOTA_ENABLED=enabled,
        FREE_DOWNLOAD_LIMIT=3,
        FREE_DOWNLOAD_WINDOW_SECONDS=86_400,
        FREE_DELIVERY_RATE_LIMIT_ENABLED=True,
        FREE_DELIVERY_RATE_BYTES_PER_SECOND=524_288,
        MEDIA_DOWNLOADS_ENABLED=True,
        PROVIDER_VK_ENABLED=True,
    )


async def _cleanup(session: AsyncSession) -> None:
    await session.execute(text("DELETE FROM premium_entitlements"))
    await session.execute(text("DELETE FROM payment_orders"))
    await session.execute(text("DELETE FROM media_delivery_grants"))
    await session.execute(text("DELETE FROM free_download_quota_entries"))
    await session.execute(text("DELETE FROM anonymous_clients"))
    await session.execute(text("DELETE FROM media_download_jobs"))
    await session.execute(text("DELETE FROM media_jobs"))
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


async def _inspected_parent(session: AsyncSession) -> tuple[MediaJob, str]:
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
    claimed = await repo.claim_next(
        worker_id="policy-test", lease_seconds=60, now=now, limit=1
    )
    assert len(claimed) == 1
    formats = []
    for option, height in (("fmt_policy_p720", 720), ("fmt_policy_p1080", 1080)):
        formats.append(
            {
                "formatOptionId": option,
                "container": "mp4",
                "width": 1280 if height == 720 else 1920,
                "height": height,
                "fps": 30.0,
                "hasVideo": True,
                "hasAudio": True,
                "category": "progressive",
                "videoCodec": "avc",
                "audioCodec": "aac",
                "approxBytes": 1_000_000,
                "qualityLabel": f"p{height}",
                "freeTierEligible": True,
            }
        )
    assert await repo.complete(
        job_id=claimed[0].id,
        owner="policy-test",
        fence=int(claimed[0].fence_token),
        metadata_json={
            "providerId": "vk",
            "canonicalProviderUrl": "https://vk.com/video-1_2",
            "mediaId": "-1_2",
            "title": "Policy test",
            "durationSeconds": 60,
            "formats": formats,
            "muxingRequired": False,
            "extractionTool": "ytdlp",
            "extractionToolVersion": None,
        },
        now=now,
    )
    return row, token


async def _download(
    session: AsyncSession, *, parent: MediaJob, option: str
) -> tuple[MediaDownloadJob, bool]:
    repo = MediaDownloadJobRepository(session)
    now = await repo.database_now()
    return await repo.enqueue_or_get(
        media_job_id=parent.id,
        format_option_id=option,
        provider_id=parent.provider_id,
        canonical_provider_url=parent.canonical_provider_url,
        media_id=parent.media_id,
        hostname=parent.hostname,
        path=parent.path,
        scheme=parent.scheme,
        port=parent.port,
        selected_format_snapshot={"formatOptionId": option},
        now=now,
        ttl_seconds=3600,
        max_attempts=3,
        parent_expires_at=parent.expires_at,
    )


async def _identity(session: AsyncSession) -> AnonymousClient:
    repo = QuotaRepository(session)
    now = await repo.database_now()
    token_hash = hash_anonymous_token(generate_anonymous_token())
    assert token_hash is not None
    return await repo.create_client(
        token_hash=token_hash, now=now, ttl_seconds=31_536_000
    )


async def _paid_order(
    session: AsyncSession, *, client_id: uuid.UUID
) -> PaymentOrder:
    now = await QuotaRepository(session).database_now()
    order = PaymentOrder(
        id=uuid.uuid4(),
        public_id=uuid.uuid4(),
        anonymous_client_id=client_id,
        provider="robokassa",
        product_code="premium_24h",
        amount_minor=10_000,
        currency="RUB",
        entitlement_duration_seconds=86_400,
        status="paid",
        is_test=True,
        receipt_json='{"items":[]}',
        creation_idempotency_hash=uuid.uuid4().bytes + uuid.uuid4().bytes,
        created_at=now - timedelta(minutes=2),
        updated_at=now,
        pending_at=now - timedelta(minutes=1),
        paid_at=now,
        provider_callback_at=now,
        expires_at=now + timedelta(hours=1),
        provider_operation_key=None,
    )
    session.add(order)
    await session.flush()
    return order


async def _active_entitlement(
    session: AsyncSession, *, client_id: uuid.UUID
) -> PremiumEntitlement:
    order = await _paid_order(session, client_id=client_id)
    now = await QuotaRepository(session).database_now()
    entitlement = PremiumEntitlement(
        id=uuid.uuid4(),
        public_id=uuid.uuid4(),
        anonymous_client_id=client_id,
        source_payment_order_id=order.id,
        product_code="premium_24h",
        starts_at=now,
        expires_at=now + timedelta(hours=24),
        created_at=now,
    )
    session.add(entitlement)
    await session.flush()
    return entitlement


async def _seed_consumed(
    session: AsyncSession,
    *,
    parent: MediaJob,
    client: AnonymousClient,
    count: int,
) -> None:
    quota = QuotaRepository(session)
    now = await quota.database_now()
    for index in range(count):
        consumed_at = now - timedelta(minutes=index)
        job, _ = await _download(
            session, parent=parent, option=f"fmt_seed_{index:02d}"
        )
        entry = await quota.reserve_locked(
            client_id=client.id,
            download_job_id=job.id,
            reserved_at=consumed_at - timedelta(seconds=1),
            reservation_expires_at=job.expires_at,
        )
        quota.consume_locked(entry, now=consumed_at)
    await session.flush()


def test_migration_0007_additive_roundtrip(database_url: str) -> None:
    _run_alembic("upgrade", "head", url=database_url)
    _run_alembic("downgrade", "0006_browser_delivery_grants", url=database_url)

    async def tables_exist() -> bool:
        engine = create_engine(Settings(APP_ENV="test", DATABASE_URL=database_url))
        try:
            async with engine.connect() as conn:
                return bool(
                    await conn.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                            "WHERE table_name = 'anonymous_clients')"
                        )
                    )
                )
        finally:
            await engine.dispose()

    assert asyncio.run(tables_exist()) is False
    _run_alembic("upgrade", "head", url=database_url)
    assert asyncio.run(tables_exist()) is True


@pytest.mark.asyncio
async def test_same_cookie_identity_is_stable_forged_rotates_and_browsers_isolate(
    session_factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    settings = _settings(database_url)
    service = QuotaService(settings)
    async with session_factory() as session:
        await _cleanup(session)
        first = await service.bootstrap_identity(cookie_header=None, session=session)
        await session.commit()
    assert first.raw_token is not None
    async with session_factory() as session:
        same = await service.bootstrap_identity(
            cookie_header=f"__Host-fetchnow_client={first.raw_token}",
            session=session,
        )
        forged = await service.bootstrap_identity(
            cookie_header=f"__Host-fetchnow_client={generate_anonymous_token()}",
            session=session,
        )
        second_browser = await service.bootstrap_identity(
            cookie_header=None, session=session
        )
        await session.commit()
    assert same.id == first.id
    assert same.expires_at == first.expires_at
    assert same.raw_token is None
    assert forged.id != first.id
    assert forged.raw_token is not None
    assert second_browser.id not in {first.id, forged.id}


@pytest.mark.asyncio
async def test_reconciliation_prunes_expired_unused_absolute_identity(
    session_factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    settings = _settings(database_url)
    async with session_factory() as session:
        await _cleanup(session)
        client = await _identity(session)
        await session.execute(
            text(
                "UPDATE anonymous_clients "
                "SET created_at = clock_timestamp() - interval '2 seconds', "
                "expires_at = clock_timestamp() - interval '1 second' "
                "WHERE id = :client_id"
            ),
            {"client_id": client.id},
        )
        result = await QuotaReconciler(settings).run(session=session)
        await session.commit()
    assert result.clients_deleted == 1
    async with session_factory() as session:
        assert await session.get(AnonymousClient, client.id) is None


async def _concurrent_admission(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    parent: MediaJob,
    client_id: uuid.UUID,
    option: str,
) -> str:
    try:
        async with session_factory() as session:
            job, created = await _download(session, parent=parent, option=option)
            if created:
                await QuotaService(settings).admit_locked(
                    identity_id=client_id,
                    download_job_id=job.id,
                    reservation_expires_at=job.expires_at,
                    session=session,
                )
            await session.commit()
            return "admitted" if created else "existing"
    except FreeQuotaExceededError:
        return "rejected"


@pytest.mark.asyncio
async def test_two_consumed_five_concurrent_distinct_admit_exactly_one(
    session_factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    settings = _settings(database_url)
    async with session_factory() as session:
        await _cleanup(session)
        parent = await _parent(session)
        client = await _identity(session)
        await _seed_consumed(session, parent=parent, client=client, count=2)
        await session.commit()
    results = await asyncio.gather(
        *(
            _concurrent_admission(
                session_factory,
                settings,
                parent=parent,
                client_id=client.id,
                option=f"fmt_distinct_{index}",
            )
            for index in range(5)
        )
    )
    assert results.count("admitted") == 1
    assert results.count("rejected") == 4


@pytest.mark.asyncio
async def test_three_consumed_concurrent_distinct_admit_zero(
    session_factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    settings = _settings(database_url)
    async with session_factory() as session:
        await _cleanup(session)
        parent = await _parent(session)
        client = await _identity(session)
        await _seed_consumed(session, parent=parent, client=client, count=3)
        await session.commit()
    results = await asyncio.gather(
        *(
            _concurrent_admission(
                session_factory,
                settings,
                parent=parent,
                client_id=client.id,
                option=f"fmt_full_{index}",
            )
            for index in range(3)
        )
    )
    assert results == ["rejected", "rejected", "rejected"]


@pytest.mark.asyncio
async def test_premium_policy_full_lifecycle_and_snapshot_boundaries(
    session_factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    settings = _settings(database_url)
    async with session_factory() as session:
        await _cleanup(session)
        parent = await _parent(session)
        client = await _identity(session)
        order = await _paid_order(session, client_id=client.id)
        payment_only = await QuotaService(settings).status(
            identity_id=client.id, session=session
        )
        assert payment_only.tier == "free"

        now = await QuotaRepository(session).database_now()
        session.add(
            PremiumEntitlement(
                id=uuid.uuid4(),
                public_id=uuid.uuid4(),
                anonymous_client_id=client.id,
                source_payment_order_id=order.id,
                product_code="premium_24h",
                starts_at=now + timedelta(hours=1),
                expires_at=now + timedelta(hours=2),
                created_at=now,
            )
        )
        await session.flush()
        future = await QuotaService(settings).status(
            identity_id=client.id, session=session
        )
        assert future.tier == "free"

        job, _ = await _download(session, parent=parent, option="fmt_policy_failure")

        class FailingPremiumService:
            async def status_for_client(self, **_kwargs: object) -> object:
                raise RuntimeError("entitlement lookup failed")

        failing = QuotaService(
            settings,
            premium_service=FailingPremiumService(),  # type: ignore[arg-type]
        )
        with pytest.raises(RuntimeError, match="entitlement lookup failed"):
            await failing.admit_locked(
                identity_id=client.id,
                download_job_id=job.id,
                reservation_expires_at=job.expires_at,
                session=session,
            )
        assert await session.scalar(
            select(func.count(FreeDownloadQuotaEntry.id))
        ) == 0
        await _cleanup(session)
        parent, access_token = await _inspected_parent(session)
        client = await _identity(session)
        entitlement = await _active_entitlement(session, client_id=client.id)
        service = DownloadJobService(settings)

        premium_view = await service.create(
            media_job_id=parent.id,
            format_option_id="fmt_policy_p720",
            access_token=access_token,
            anonymous_client_id=client.id,
            session=session,
        )
        premium_job = await session.get(MediaDownloadJob, premium_view.id)
        assert premium_job is not None
        premium_policy = decode_effective_policy_snapshot(
            premium_job.selected_format_snapshot
        )
        assert premium_policy is not None
        assert premium_policy.tier == "premium"
        assert premium_policy.delivery_rate_bytes_per_second is None
        assert service.to_public_dict(premium_view)[
            "deliveryRateBytesPerSecond"
        ] is None

        now = await QuotaRepository(session).database_now()
        entitlement.starts_at = now - timedelta(hours=2)
        entitlement.expires_at = now - timedelta(hours=1)
        await session.flush()
        free_view = await service.create(
            media_job_id=parent.id,
            format_option_id="fmt_policy_p1080",
            access_token=access_token,
            anonymous_client_id=client.id,
            session=session,
        )
        free_job = await session.get(MediaDownloadJob, free_view.id)
        assert free_job is not None
        free_policy = decode_effective_policy_snapshot(
            free_job.selected_format_snapshot
        )
        assert free_policy is not None
        assert free_policy.tier == "free"
        assert free_policy.delivery_rate_bytes_per_second == 524_288
        assert service.to_public_dict(free_view)[
            "deliveryRateBytesPerSecond"
        ] == 524_288

        delivery = DeliveryService(settings)
        assert delivery._policy_for_job(  # noqa: SLF001 - snapshot boundary
            premium_job
        ).delivery_rate_bytes_per_second is None
        assert delivery._policy_for_job(  # noqa: SLF001 - snapshot boundary
            free_job
        ).delivery_rate_bytes_per_second == 524_288
        assert await session.scalar(
            select(func.count(FreeDownloadQuotaEntry.id))
        ) == 1
        await _cleanup(session)
        parent = await _parent(session)
        client = await _identity(session)
        other = await _identity(session)
        await _seed_consumed(session, parent=parent, client=client, count=2)
        entitlement = await _active_entitlement(session, client_id=client.id)
        now = await QuotaRepository(session).database_now()
        # Seeded accounting jobs must not be claimed as the Premium work below.
        await session.execute(
            update(MediaDownloadJob)
            .where(
                MediaDownloadJob.id.in_(
                    select(FreeDownloadQuotaEntry.download_job_id)
                )
            )
            .values(available_at=now + timedelta(hours=2))
        )
        premium_job_ids: list[uuid.UUID] = []
        for index in range(21):
            job, created = await _download(
                session, parent=parent, option=f"fmt_premium_{index:02d}"
            )
            assert created is True
            admission = await QuotaService(settings).admit_locked(
                identity_id=client.id,
                download_job_id=job.id,
                reservation_expires_at=job.expires_at,
                session=session,
            )
            assert admission.policy.tier == "premium"
            premium_job_ids.append(job.id)
        assert await session.scalar(
            select(func.count(FreeDownloadQuotaEntry.id))
        ) == 2

        repo = MediaDownloadJobRepository(session)
        claimed = await repo.claim_next(
            worker_id="premium-worker", lease_seconds=120, now=now, limit=21
        )
        assert {job.id for job in claimed} == set(premium_job_ids)
        failed_job, *successful_jobs = claimed
        assert await repo.fail_permanent(
            job_id=failed_job.id,
            owner="premium-worker",
            fence=int(failed_job.fence_token),
            public_error_code="DOWNLOAD_TOOL_FAILED",
            now=now,
        )
        for job in successful_jobs:
            assert await repo.complete_ready(
                job_id=job.id,
                owner="premium-worker",
                fence=int(job.fence_token),
                artifact_id=uuid.uuid4(),
                artifact_bytes=10,
                artifact_content_type="video/mp4",
                artifact_container="mp4",
                now=now,
            )
        assert await session.scalar(
            select(func.count(FreeDownloadQuotaEntry.id))
        ) == 2

        premium_status = await QuotaService(settings).status(
            identity_id=client.id, session=session
        )
        assert premium_status.tier == "premium"
        assert premium_status.download_limit is None
        assert premium_status.downloads_used is None
        assert premium_status.premium_expires_at == entitlement.expires_at

        other_status = await QuotaService(settings).status(
            identity_id=other.id, session=session
        )
        assert other_status.tier == "free"
        assert other_status.downloads_used == 0

        entitlement.starts_at = now - timedelta(hours=2)
        entitlement.expires_at = now - timedelta(hours=1)
        await session.flush()
        free_again = await QuotaService(settings).status(
            identity_id=client.id, session=session
        )
        assert free_again.tier == "free"
        assert free_again.downloads_used == 2
        assert free_again.downloads_remaining == 1

        await session.execute(
            update(FreeDownloadQuotaEntry)
            .where(FreeDownloadQuotaEntry.anonymous_client_id == client.id)
            .values(
                reserved_at=now - timedelta(days=2, seconds=1),
                consumed_at=now - timedelta(days=2),
            )
        )
        aged_out = await QuotaService(settings).status(
            identity_id=client.id, session=session
        )
        assert aged_out.downloads_used == 0
        assert aged_out.downloads_remaining == 3
        await _cleanup(session)


@pytest.mark.asyncio
async def test_concurrent_premium_admissions_are_not_free_quota_limited(
    session_factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    settings = _settings(database_url)
    async with session_factory() as session:
        await _cleanup(session)
        parent = await _parent(session)
        client = await _identity(session)
        await _seed_consumed(session, parent=parent, client=client, count=3)
        await _active_entitlement(session, client_id=client.id)
        await session.commit()
    results = await asyncio.gather(
        *(
            _concurrent_admission(
                session_factory,
                settings,
                parent=parent,
                client_id=client.id,
                option=f"fmt_premium_parallel_{index}",
            )
            for index in range(5)
        )
    )
    assert results == ["admitted"] * 5
    async with session_factory() as session:
        entries = list(
            (
                await session.scalars(
                    select(FreeDownloadQuotaEntry).where(
                        FreeDownloadQuotaEntry.anonymous_client_id == client.id
                    )
                )
            ).all()
        )
        assert len(entries) == 3
        await _cleanup(session)


@pytest.mark.asyncio
async def test_concurrent_same_idempotent_download_creates_one_reservation(
    session_factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    settings = _settings(database_url)
    async with session_factory() as session:
        await _cleanup(session)
        parent = await _parent(session)
        client = await _identity(session)
        await session.commit()
    results = await asyncio.gather(
        *(
            _concurrent_admission(
                session_factory,
                settings,
                parent=parent,
                client_id=client.id,
                option="fmt_same",
            )
            for _ in range(5)
        )
    )
    assert results.count("admitted") == 1
    assert results.count("existing") == 4
    async with session_factory() as session:
        entries = list((await session.scalars(select(FreeDownloadQuotaEntry))).all())
    assert len(entries) == 1


@pytest.mark.asyncio
async def test_reset_at_only_promised_when_consumed_usage_exhausts_limit(
    session_factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    settings = _settings(database_url)
    policy = effective_download_policy(settings)
    async with session_factory() as session:
        await _cleanup(session)
        parent = await _parent(session)
        client = await _identity(session)
        await _seed_consumed(session, parent=parent, client=client, count=2)
        reserved_job, _ = await _download(
            session, parent=parent, option="fmt_reserved"
        )
        quota = QuotaRepository(session)
        now = await quota.database_now()
        await quota.reserve_locked(
            client_id=client.id,
            download_job_id=reserved_job.id,
            reserved_at=now,
            reservation_expires_at=reserved_job.expires_at,
        )
        await quota.lock_client(client.id)
        status = await quota.status_locked(
            client_id=client.id, now=now, policy=policy
        )
        await session.commit()
    assert (status.downloads_used, status.downloads_reserved) == (2, 1)
    assert status.downloads_remaining == 0
    assert status.reset_at is None

    async with session_factory() as session:
        await _cleanup(session)
        parent = await _parent(session)
        client = await _identity(session)
        await _seed_consumed(session, parent=parent, client=client, count=3)
        quota = QuotaRepository(session)
        now = await quota.database_now()
        await quota.lock_client(client.id)
        status = await quota.status_locked(
            client_id=client.id, now=now, policy=policy
        )
        await session.commit()
    assert (status.downloads_used, status.downloads_reserved) == (3, 0)
    assert status.downloads_remaining == 0
    assert status.reset_at is not None


@pytest.mark.asyncio
async def test_reset_at_remains_reliable_after_limit_is_reduced(
    session_factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    settings = _settings(database_url)
    policy = effective_download_policy(settings)
    async with session_factory() as session:
        await _cleanup(session)
        parent = await _parent(session)
        client = await _identity(session)
        await _seed_consumed(session, parent=parent, client=client, count=4)
        quota = QuotaRepository(session)
        now = await quota.database_now()
        await quota.lock_client(client.id)
        status = await quota.status_locked(
            client_id=client.id, now=now, policy=policy
        )
        consumed = list(
            (
                await session.scalars(
                    select(FreeDownloadQuotaEntry.consumed_at)
                    .where(FreeDownloadQuotaEntry.state == "consumed")
                    .order_by(FreeDownloadQuotaEntry.consumed_at.asc())
                )
            ).all()
        )
        await session.commit()
    assert status.downloads_used == 4
    assert status.reset_at == consumed[1] + timedelta(
        seconds=policy.window_seconds
    )


async def _reserved_claim(
    session: AsyncSession,
    *,
    parent: MediaJob,
    client: AnonymousClient,
    settings: Settings,
) -> MediaDownloadJob:
    job, _ = await _download(session, parent=parent, option="fmt_race")
    await QuotaService(settings).admit_locked(
        identity_id=client.id,
        download_job_id=job.id,
        reservation_expires_at=job.expires_at,
        session=session,
    )
    repo = MediaDownloadJobRepository(session)
    now = await repo.database_now()
    claimed = await repo.claim_next(
        worker_id="worker", lease_seconds=120, now=now, limit=1
    )
    assert len(claimed) == 1
    return claimed[0]


@pytest.mark.asyncio
async def test_ready_vs_cancel_race_has_one_coherent_terminal_quota_state(
    session_factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    settings = _settings(database_url)
    async with session_factory() as session:
        await _cleanup(session)
        parent = await _parent(session)
        client = await _identity(session)
        claimed = await _reserved_claim(
            session, parent=parent, client=client, settings=settings
        )
        fence = int(claimed.fence_token)
        await session.commit()

    async def ready() -> bool:
        async with session_factory() as session:
            repo = MediaDownloadJobRepository(session)
            applied = await repo.complete_ready(
                job_id=claimed.id,
                owner="worker",
                fence=fence,
                artifact_id=uuid.uuid4(),
                artifact_bytes=10,
                artifact_content_type="video/mp4",
                artifact_container="mp4",
                now=await repo.database_now(),
            )
            await session.commit()
            return applied

    async def cancel() -> bool:
        async with session_factory() as session:
            repo = MediaDownloadJobRepository(session)
            now = await repo.database_now()
            await repo.request_cancel(job_id=claimed.id, now=now)
            applied = await repo.complete_cancelled(
                job_id=claimed.id, owner="worker", fence=fence, now=now
            )
            await session.commit()
            return applied

    await asyncio.gather(ready(), cancel())
    async with session_factory() as session:
        job = await session.get(MediaDownloadJob, claimed.id)
        entry = await session.scalar(
            select(FreeDownloadQuotaEntry).where(
                FreeDownloadQuotaEntry.download_job_id == claimed.id
            )
        )
    assert job is not None and entry is not None
    assert (job.public_state, entry.state) in {
        ("ready", "consumed"),
        ("expired", "released"),
    }


@pytest.mark.asyncio
async def test_reconcile_race_and_disabled_flag_still_consume_existing_reservation(
    session_factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    enabled = _settings(database_url, enabled=True)
    disabled = _settings(database_url, enabled=False)
    async with session_factory() as session:
        await _cleanup(session)
        parent = await _parent(session)
        client = await _identity(session)
        claimed = await _reserved_claim(
            session, parent=parent, client=client, settings=enabled
        )
        fence = int(claimed.fence_token)
        await session.commit()

    async def finalize() -> bool:
        async with session_factory() as session:
            repo = MediaDownloadJobRepository(session)
            applied = await repo.complete_ready(
                job_id=claimed.id,
                owner="worker",
                fence=fence,
                artifact_id=uuid.uuid4(),
                artifact_bytes=10,
                artifact_content_type="video/mp4",
                artifact_container="mp4",
                now=await repo.database_now(),
            )
            await session.commit()
            return applied

    async def reconcile() -> None:
        async with session_factory() as session:
            await QuotaReconciler(disabled).run(session=session)
            await session.commit()

    finalized, _ = await asyncio.gather(finalize(), reconcile())
    assert finalized is True
    async with session_factory() as session:
        entry = await session.scalar(
            select(FreeDownloadQuotaEntry).where(
                FreeDownloadQuotaEntry.download_job_id == claimed.id
            )
        )
    assert entry is not None
    assert entry.state == "consumed"
    assert entry.consumed_at is not None


@pytest.mark.asyncio
async def test_flag_disabled_after_reservation_still_releases_terminal_failure(
    session_factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    enabled = _settings(database_url, enabled=True)
    disabled = _settings(database_url, enabled=False)
    assert disabled.free_download_quota_enabled is False
    async with session_factory() as session:
        await _cleanup(session)
        parent = await _parent(session)
        client = await _identity(session)
        claimed = await _reserved_claim(
            session, parent=parent, client=client, settings=enabled
        )
        fence = int(claimed.fence_token)
        await session.commit()

    async with session_factory() as session:
        repo = MediaDownloadJobRepository(session)
        applied = await repo.fail_permanent(
            job_id=claimed.id,
            owner="worker",
            fence=fence,
            public_error_code="DOWNLOAD_TOOL_FAILED",
            now=await repo.database_now(),
        )
        await session.commit()
    assert applied is True
    async with session_factory() as session:
        job = await session.get(MediaDownloadJob, claimed.id)
        entry = await session.scalar(
            select(FreeDownloadQuotaEntry).where(
                FreeDownloadQuotaEntry.download_job_id == claimed.id
            )
        )
    assert job is not None and job.public_state == "failed"
    assert entry is not None and entry.state == "released"
