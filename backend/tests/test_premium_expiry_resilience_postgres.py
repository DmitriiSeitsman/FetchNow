"""PostgreSQL acceptance for Premium expiry coherence and worker resilience."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fetchnow.core.config import Settings
from fetchnow.db.session import create_engine, create_session_factory
from fetchnow.downloads.grant_models import MediaDeliveryGrant
from fetchnow.downloads.models import MediaDownloadJob
from fetchnow.downloads.premium_coherence import is_coherent_promoted_premium_job
from fetchnow.downloads.repository import MediaDownloadJobRepository
from fetchnow.downloads.service import DownloadJobService
from fetchnow.downloads.snapshot_codec import (
    attach_effective_policy_snapshot,
    decode_effective_policy_snapshot,
)
from fetchnow.downloads.states import MediaDownloadJobState
from fetchnow.jobs.credentials import (
    generate_access_token,
    hash_access_token,
    hash_request_fingerprint,
)
from fetchnow.jobs.models import MediaJob
from fetchnow.jobs.repository import MediaJobRepository
from fetchnow.jobs.worker_loop import MediaJobWorkerRunner
from fetchnow.payments.models import PaymentOrder
from fetchnow.premium.models import PremiumEntitlement
from fetchnow.quota.delivery import DELIVERY_ATTEMPT_LEASE_SECONDS
from fetchnow.quota.models import (
    AnonymousClient,
    FreeDownloadDeliveryRange,
    FreeDownloadQuotaEntry,
)
from fetchnow.quota.policy import effective_download_policy
from fetchnow.quota.repository import QuotaRepository
from fetchnow.quota.tokens import generate_anonymous_token, hash_anonymous_token

_TEST_URL = os.environ.get("FETCHNOW_TEST_DATABASE_URL", "").strip()
_BACKEND_ROOT = Path(__file__).resolve().parents[1]

_FORMAT = {
    "formatOptionId": "fmt_expiry_resilience",
    "container": "mp4",
    "width": 1280,
    "height": 720,
    "fps": 30,
    "hasVideo": True,
    "hasAudio": True,
    "category": "progressive",
    "videoCodec": "avc",
    "audioCodec": "aac",
    "approxBytes": 1_048_576,
    "qualityLabel": "p720",
    "freeTierEligible": True,
    "mediaKind": "normal_video",
    "requiresPremium": False,
    "bitrateKbps": None,
}


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


def _settings(url: str) -> Settings:
    return Settings(
        APP_ENV="test",
        DATABASE_URL=url,
        FREE_DOWNLOAD_QUOTA_ENABLED=True,
        FREE_DOWNLOAD_LIMIT=3,
        FREE_DOWNLOAD_WINDOW_SECONDS=86_400,
        FREE_DELIVERY_RATE_LIMIT_ENABLED=True,
        FREE_DELIVERY_RATE_BYTES_PER_SECOND=524_288,
        FREE_DOWNLOAD_QUOTA_READY_COMPATIBILITY_MODE=False,
        MEDIA_DOWNLOADS_ENABLED=True,
        MEDIA_BROWSER_DELIVERY_ENABLED=True,
        MEDIA_BROWSER_GRANT_TTL_SECONDS=300,
        MEDIA_BROWSER_GRANT_MAX_ACTIVE=2,
        MEDIA_DELIVERY_ENABLED=True,
        MEDIA_DELIVERY_ROOT="/tmp/fetchnow-delivery-test",
        PROVIDER_VK_ENABLED=True,
        ROBOKASSA_MODE="test",
        ROBOKASSA_MERCHANT_LOGIN="demo",
        ROBOKASSA_SIGNATURE_ALGORITHM="sha256",
        ROBOKASSA_TEST_PASSWORD1="test-password-one",
        ROBOKASSA_TEST_PASSWORD2="test-password-two",
        ROBOKASSA_TEST_AMOUNT_MINOR=100,
        ROBOKASSA_RECEIPT_TAX="none",
        ROBOKASSA_RECEIPT_PAYMENT_METHOD="full_payment",
        PREMIUM_TEST_CHECKOUT_VISIBLE=True,
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


async def _parent_with_token(
    session: AsyncSession, *, suffix: str = "1"
) -> tuple[MediaJob, str]:
    repo = MediaJobRepository(session)
    now = await repo.database_now()
    token = generate_access_token()
    path = f"/video-{suffix}_2"
    row, _ = await repo.enqueue_or_get(
        credential_hash=hash_access_token(token),
        request_fingerprint=hash_request_fingerprint(
            access_token=token,
            normalized_request=f"https://vk.com{path}",
        ),
        provider_id="vk",
        canonical_provider_url=f"https://vk.com{path}",
        media_id=f"-{suffix}_2",
        hostname="vk.com",
        path=path,
        scheme="https",
        port=None,
        now=now,
        ttl_seconds=3600,
        max_attempts=3,
    )
    await session.flush()
    return row, token


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


async def _ready_free(
    session: AsyncSession,
    *,
    settings: Settings,
    parent: MediaJob,
    client: AnonymousClient,
    artifact_bytes: int = 1_048_576,
    format_option_id: str | None = None,
) -> MediaDownloadJob:
    repo = MediaDownloadJobRepository(session)
    now = await repo.database_now()
    fmt = dict(_FORMAT)
    if format_option_id is not None:
        fmt["formatOptionId"] = format_option_id
    policy = effective_download_policy(settings)
    snapshot = attach_effective_policy_snapshot(fmt, policy)
    job, created = await repo.enqueue_or_get(
        media_job_id=parent.id,
        format_option_id=str(fmt["formatOptionId"]),
        provider_id=parent.provider_id,
        canonical_provider_url=parent.canonical_provider_url,
        media_id=parent.media_id,
        hostname=parent.hostname,
        path=parent.path,
        scheme=parent.scheme,
        port=parent.port,
        selected_format_snapshot=snapshot,
        now=now,
        ttl_seconds=3600,
        max_attempts=3,
        parent_expires_at=parent.expires_at,
        suggested_filename="clip.mp4",
    )
    assert created is True
    quota = QuotaRepository(session)
    await quota.reserve_locked(
        client_id=client.id,
        download_job_id=job.id,
        reserved_at=now,
        reservation_expires_at=job.expires_at,
    )
    claimed = await repo.claim_next(
        worker_id="expiry-worker", lease_seconds=60, now=now, limit=1
    )
    assert len(claimed) == 1
    applied = await repo.complete_ready(
        job_id=claimed[0].id,
        owner="expiry-worker",
        fence=int(claimed[0].fence_token),
        artifact_id=uuid.uuid4(),
        artifact_bytes=artifact_bytes,
        artifact_container="mp4",
        artifact_content_type="video/mp4",
        now=now,
        consume_quota_on_ready=False,
    )
    assert applied is True
    await session.flush()
    loaded = await repo.get_by_id(claimed[0].id)
    assert loaded is not None
    return loaded


async def _grant_entitlement(
    session: AsyncSession, *, client_id: uuid.UUID
) -> PremiumEntitlement:
    now = await QuotaRepository(session).database_now()
    order = PaymentOrder(
        id=uuid.uuid4(),
        public_id=uuid.uuid4(),
        anonymous_client_id=client_id,
        provider="robokassa",
        product_code="premium_24h",
        amount_minor=100,
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
    row = PremiumEntitlement(
        id=uuid.uuid4(),
        public_id=uuid.uuid4(),
        anonymous_client_id=client_id,
        source_payment_order_id=order.id,
        product_code="premium_24h",
        starts_at=now,
        expires_at=now + timedelta(hours=24),
        created_at=now,
    )
    session.add(row)
    await session.flush()
    return row


async def _make_due(session: AsyncSession, job: MediaDownloadJob) -> None:
    now = await QuotaRepository(session).database_now()
    job.created_at = now - timedelta(hours=2)
    job.expires_at = now - timedelta(seconds=5)
    await session.flush()


def _quota_fp(entry: FreeDownloadQuotaEntry) -> tuple[object, ...]:
    return (
        entry.state,
        entry.released_at,
        entry.consumed_at,
        entry.reserved_at,
        entry.reservation_expires_at,
        entry.anonymous_client_id,
    )


@pytest.mark.asyncio
async def test_promoted_premium_released_expires_normally(
    session_factory: async_sessionmaker[AsyncSession], migrated_database: str
) -> None:
    settings = _settings(migrated_database)
    async with session_factory() as session:
        await _cleanup(session)
        parent, access = await _parent_with_token(session, suffix="10")
        client = await _identity(session)
        job = await _ready_free(
            session, settings=settings, parent=parent, client=client
        )
        await _grant_entitlement(session, client_id=client.id)
        await session.commit()
        job_id, client_id = job.id, client.id

    async with session_factory() as session:
        view = await DownloadJobService(settings).upgrade_to_premium(
            download_job_id=job_id,
            access_token=access,
            anonymous_client_id=client_id,
            session=session,
        )
        assert view.created is True
        promoted = await session.get(MediaDownloadJob, job_id)
        assert promoted is not None
        entry = await session.scalar(
            select(FreeDownloadQuotaEntry).where(
                FreeDownloadQuotaEntry.download_job_id == job_id
            )
        )
        assert entry is not None and entry.state == "released"
        assert entry.consumed_at is None
        before = _quota_fp(entry)
        policy = decode_effective_policy_snapshot(promoted.selected_format_snapshot)
        assert policy is not None and policy.tier == "premium"
        assert is_coherent_promoted_premium_job(
            promoted, expected_identity_id=client_id
        )
        fence_before = int(promoted.fence_token)
        artifact_before = promoted.artifact_id
        await _make_due(session, promoted)
        now = await QuotaRepository(session).database_now()
        assert artifact_before is not None
        session.add(
            MediaDeliveryGrant(
                id=uuid.uuid4(),
                download_job_id=job_id,
                token_hash=os.urandom(32),
                artifact_id=artifact_before,
                fence_token=fence_before,
                expires_at=now + timedelta(hours=1),
                created_at=now,
                revoked_at=None,
            )
        )
        await session.commit()

    async with session_factory() as session:
        repo = MediaDownloadJobRepository(session)
        now = await repo.database_now()
        artifacts = await repo.expire_due_jobs(now)
        await session.commit()
        assert artifacts == [artifact_before]
        loaded = await session.get(MediaDownloadJob, job_id)
        assert loaded is not None
        assert loaded.public_state == MediaDownloadJobState.EXPIRED.value
        assert loaded.artifact_id is None
        assert int(loaded.fence_token) == fence_before + 1
        entry = await session.scalar(
            select(FreeDownloadQuotaEntry).where(
                FreeDownloadQuotaEntry.download_job_id == job_id
            )
        )
        assert entry is not None
        assert _quota_fp(entry) == before
        grant = await session.scalar(
            select(MediaDeliveryGrant).where(
                MediaDeliveryGrant.download_job_id == job_id
            )
        )
        assert grant is not None and grant.revoked_at is not None


@pytest.mark.asyncio
async def test_consumed_quota_after_promote_stays_consumed_on_expiry(
    session_factory: async_sessionmaker[AsyncSession], migrated_database: str
) -> None:
    settings = _settings(migrated_database)
    size = 100
    async with session_factory() as session:
        await _cleanup(session)
        parent, access = await _parent_with_token(session, suffix="11")
        client = await _identity(session)
        job = await _ready_free(
            session,
            settings=settings,
            parent=parent,
            client=client,
            artifact_bytes=size,
        )
        entry = await session.scalar(
            select(FreeDownloadQuotaEntry).where(
                FreeDownloadQuotaEntry.download_job_id == job.id
            )
        )
        assert entry is not None
        now = await QuotaRepository(session).database_now()
        assert job.artifact_id is not None
        session.add(
            FreeDownloadDeliveryRange(
                id=uuid.uuid4(),
                quota_entry_id=entry.id,
                artifact_id=job.artifact_id,
                fence_token=int(job.fence_token),
                artifact_bytes=size,
                request_start=0,
                request_end_exclusive=size,
                served_end_exclusive=size,
                state="closed",
                started_at=now,
                lease_expires_at=None,
                closed_at=now,
            )
        )
        await _grant_entitlement(session, client_id=client.id)
        await session.commit()
        job_id, client_id = job.id, client.id

    async with session_factory() as session:
        view = await DownloadJobService(settings).upgrade_to_premium(
            download_job_id=job_id,
            access_token=access,
            anonymous_client_id=client_id,
            session=session,
        )
        assert view.created is True
        entry = await session.scalar(
            select(FreeDownloadQuotaEntry).where(
                FreeDownloadQuotaEntry.download_job_id == job_id
            )
        )
        assert entry is not None and entry.state == "consumed"
        consumed_at = entry.consumed_at
        promoted = await session.get(MediaDownloadJob, job_id)
        assert promoted is not None
        await _make_due(session, promoted)
        await session.commit()

    async with session_factory() as session:
        repo = MediaDownloadJobRepository(session)
        now = await repo.database_now()
        await repo.expire_due_jobs(now)
        await session.commit()
        entry = await session.scalar(
            select(FreeDownloadQuotaEntry).where(
                FreeDownloadQuotaEntry.download_job_id == job_id
            )
        )
        assert entry is not None
        assert entry.state == "consumed"
        assert entry.consumed_at == consumed_at
        assert entry.released_at is None


@pytest.mark.asyncio
async def test_malformed_premium_snapshot_does_not_bypass_free_invariant(
    session_factory: async_sessionmaker[AsyncSession], migrated_database: str
) -> None:
    settings = _settings(migrated_database)
    async with session_factory() as session:
        await _cleanup(session)
        parent, _access = await _parent_with_token(session, suffix="12")
        client = await _identity(session)
        job = await _ready_free(
            session, settings=settings, parent=parent, client=client
        )
        entry = await session.scalar(
            select(FreeDownloadQuotaEntry).where(
                FreeDownloadQuotaEntry.download_job_id == job.id
            )
        )
        assert entry is not None
        now = await QuotaRepository(session).database_now()
        QuotaRepository(session).release_locked(entry, now=now)
        # Spoof a non-decodable "premium" marker without valid policy keys.
        job.selected_format_snapshot = {
            **dict(job.selected_format_snapshot),
            "effectiveDownloadPolicy": {
                "tier": "premium",
                "downloadLimit": None,
                "quotaWindowSeconds": None,
                "deliveryRateBytesPerSecond": None,
                "premiumExpiresAt": "not-a-timestamp",
                "allowCombined": True,
                "allowAudioOnly": True,
                "allowVideoOnly": True,
                "authorizedIdentityId": str(client.id),
            },
        }
        assert not is_coherent_promoted_premium_job(
            job, expected_identity_id=client.id
        )
        await _make_due(session, job)
        artifact = job.artifact_id
        fence = int(job.fence_token)
        before = _quota_fp(entry)
        await session.commit()
        job_id = job.id

    async with session_factory() as session:
        repo = MediaDownloadJobRepository(session)
        now = await repo.database_now()
        arts = await repo.expire_due_jobs(now)
        await session.commit()
        assert arts == [artifact]
        loaded = await session.get(MediaDownloadJob, job_id)
        assert loaded is not None
        assert loaded.public_state == MediaDownloadJobState.EXPIRED.value
        assert loaded.artifact_id is None
        assert int(loaded.fence_token) == fence + 1
        entry = await session.scalar(
            select(FreeDownloadQuotaEntry).where(
                FreeDownloadQuotaEntry.download_job_id == job_id
            )
        )
        assert entry is not None
        assert _quota_fp(entry) == before

    # Identity mismatch: decodable Premium snapshot bound to a different client.
    async with session_factory() as session:
        await _cleanup(session)
        parent, _access = await _parent_with_token(session, suffix="13")
        client = await _identity(session)
        job = await _ready_free(
            session, settings=settings, parent=parent, client=client
        )
        entry = await session.scalar(
            select(FreeDownloadQuotaEntry).where(
                FreeDownloadQuotaEntry.download_job_id == job.id
            )
        )
        assert entry is not None
        now = await QuotaRepository(session).database_now()
        QuotaRepository(session).release_locked(entry, now=now)
        foreign = uuid.uuid4()
        job.selected_format_snapshot = {
            **dict(job.selected_format_snapshot),
            "effectiveDownloadPolicy": {
                "tier": "premium",
                "downloadLimit": None,
                "quotaWindowSeconds": None,
                "deliveryRateBytesPerSecond": None,
                "premiumExpiresAt": (now + timedelta(hours=1)).isoformat(),
                "allowCombined": True,
                "allowAudioOnly": True,
                "allowVideoOnly": True,
                "authorizedIdentityId": str(foreign),
            },
        }
        assert not is_coherent_promoted_premium_job(
            job, expected_identity_id=client.id
        )
        await _make_due(session, job)
        artifact = job.artifact_id
        before = _quota_fp(entry)
        await session.commit()
        job_id = job.id

    async with session_factory() as session:
        repo = MediaDownloadJobRepository(session)
        now = await repo.database_now()
        arts = await repo.expire_due_jobs(now)
        await session.commit()
        assert arts == [artifact]
        loaded = await session.get(MediaDownloadJob, job_id)
        assert loaded is not None
        assert loaded.public_state == MediaDownloadJobState.EXPIRED.value
        entry = await session.scalar(
            select(FreeDownloadQuotaEntry).where(
                FreeDownloadQuotaEntry.download_job_id == job_id
            )
        )
        assert entry is not None
        assert _quota_fp(entry) == before


@pytest.mark.asyncio
async def test_incoherent_free_job_quarantined_sibling_still_expires(
    session_factory: async_sessionmaker[AsyncSession], migrated_database: str
) -> None:
    settings = _settings(migrated_database)
    async with session_factory() as session:
        await _cleanup(session)
        bad_parent, _ = await _parent_with_token(session, suffix="20")
        good_parent, _ = await _parent_with_token(session, suffix="21")
        bad_client = await _identity(session)
        good_client = await _identity(session)
        bad = await _ready_free(
            session,
            settings=settings,
            parent=bad_parent,
            client=bad_client,
            format_option_id="fmt_bad",
        )
        good = await _ready_free(
            session,
            settings=settings,
            parent=good_parent,
            client=good_client,
            format_option_id="fmt_good",
        )
        bad_entry = await session.scalar(
            select(FreeDownloadQuotaEntry).where(
                FreeDownloadQuotaEntry.download_job_id == bad.id
            )
        )
        assert bad_entry is not None
        now = await QuotaRepository(session).database_now()
        QuotaRepository(session).release_locked(bad_entry, now=now)
        await _make_due(session, bad)
        await _make_due(session, good)
        bad_id, good_id = bad.id, good.id
        good_artifact = good.artifact_id
        await session.commit()

    async with session_factory() as session:
        repo = MediaDownloadJobRepository(session)
        now = await repo.database_now()
        arts = await repo.expire_due_jobs(now)
        await session.commit()
        bad_loaded = await session.get(MediaDownloadJob, bad_id)
        good_loaded = await session.get(MediaDownloadJob, good_id)
        assert bad_loaded is not None and good_loaded is not None
        assert bad_loaded.public_state == MediaDownloadJobState.EXPIRED.value
        assert good_loaded.public_state == MediaDownloadJobState.EXPIRED.value
        assert good_artifact in arts
        bad_entry = await session.scalar(
            select(FreeDownloadQuotaEntry).where(
                FreeDownloadQuotaEntry.download_job_id == bad_id
            )
        )
        assert bad_entry is not None and bad_entry.state == "released"


@pytest.mark.asyncio
async def test_live_lease_defers_quarantine_then_expires_after_close(
    session_factory: async_sessionmaker[AsyncSession], migrated_database: str
) -> None:
    settings = _settings(migrated_database)
    async with session_factory() as session:
        await _cleanup(session)
        parent, _ = await _parent_with_token(session, suffix="30")
        client = await _identity(session)
        job = await _ready_free(
            session, settings=settings, parent=parent, client=client, artifact_bytes=50
        )
        entry = await session.scalar(
            select(FreeDownloadQuotaEntry).where(
                FreeDownloadQuotaEntry.download_job_id == job.id
            )
        )
        assert entry is not None
        now = await QuotaRepository(session).database_now()
        assert job.artifact_id is not None
        session.add(
            FreeDownloadDeliveryRange(
                id=uuid.uuid4(),
                quota_entry_id=entry.id,
                artifact_id=job.artifact_id,
                fence_token=int(job.fence_token),
                artifact_bytes=50,
                request_start=0,
                request_end_exclusive=10,
                served_end_exclusive=4,
                state="active",
                started_at=now,
                lease_expires_at=now
                + timedelta(seconds=DELIVERY_ATTEMPT_LEASE_SECONDS),
                closed_at=None,
            )
        )
        QuotaRepository(session).release_locked(entry, now=now)
        await _make_due(session, job)
        job_id = job.id
        artifact = job.artifact_id
        await session.commit()

    async with session_factory() as session:
        repo = MediaDownloadJobRepository(session)
        now = await repo.database_now()
        assert await repo.expire_due_jobs(now) == []
        await session.commit()
        protected = await session.get(MediaDownloadJob, job_id)
        assert protected is not None and protected.artifact_id == artifact

    async with session_factory() as session:
        now = await QuotaRepository(session).database_now()
        row = await session.scalar(
            select(FreeDownloadDeliveryRange).where(
                FreeDownloadDeliveryRange.quota_entry_id
                == select(FreeDownloadQuotaEntry.id)
                .where(FreeDownloadQuotaEntry.download_job_id == job_id)
                .scalar_subquery()
            )
        )
        assert row is not None
        row.lease_expires_at = now - timedelta(seconds=1)
        row.state = "closed"
        row.closed_at = now
        row.lease_expires_at = None
        await session.commit()

    async with session_factory() as session:
        repo = MediaDownloadJobRepository(session)
        now = await repo.database_now()
        arts = await repo.expire_due_jobs(now)
        await session.commit()
        assert arts == [artifact]
        loaded = await session.get(MediaDownloadJob, job_id)
        assert loaded is not None
        assert loaded.public_state == MediaDownloadJobState.EXPIRED.value


@pytest.mark.asyncio
async def test_concurrent_promote_and_expiry_no_deadlock(
    session_factory: async_sessionmaker[AsyncSession], migrated_database: str
) -> None:
    settings = _settings(migrated_database)
    async with session_factory() as session:
        await _cleanup(session)
        parent, access = await _parent_with_token(session, suffix="40")
        client = await _identity(session)
        job = await _ready_free(
            session, settings=settings, parent=parent, client=client
        )
        await _grant_entitlement(session, client_id=client.id)
        job_id, client_id = job.id, client.id
        await session.commit()

    async def _promote() -> str:
        async with session_factory() as session:
            try:
                await DownloadJobService(settings).upgrade_to_premium(
                    download_job_id=job_id,
                    access_token=access,
                    anonymous_client_id=client_id,
                    session=session,
                )
                await session.commit()
                return "promoted"
            except Exception:
                await session.rollback()
                return "promote_failed"

    async def _expire() -> str:
        await asyncio.sleep(0.02)
        async with session_factory() as session:
            loaded = await session.get(MediaDownloadJob, job_id)
            assert loaded is not None
            await _make_due(session, loaded)
            await session.commit()
        async with session_factory() as session:
            repo = MediaDownloadJobRepository(session)
            now = await repo.database_now()
            await repo.expire_due_jobs(now)
            await session.commit()
            return "expired"

    outcomes = await asyncio.wait_for(
        asyncio.gather(_promote(), _expire()), timeout=10.0
    )
    assert "expired" in outcomes
    async with session_factory() as session:
        loaded = await session.get(MediaDownloadJob, job_id)
        assert loaded is not None
        assert loaded.public_state in {
            MediaDownloadJobState.READY.value,
            MediaDownloadJobState.EXPIRED.value,
        }
        if loaded.public_state == MediaDownloadJobState.READY.value:
            policy = decode_effective_policy_snapshot(loaded.selected_format_snapshot)
            assert policy is not None and policy.tier == "premium"
        # Either promote-then-expire, expire-then-promote-fail, or promote-only
        # before due window — all must complete without deadlock.


@pytest.mark.asyncio
async def test_hygiene_fault_does_not_skip_inspection_claim() -> None:
    settings = Settings(
        APP_ENV="test",
        LOG_LEVEL="WARNING",
        DATABASE_URL="postgresql+asyncpg://fetchnow:fetchnow@127.0.0.1:5432/fetchnow",
        MEDIA_JOBS_ENABLED=True,
        MEDIA_INSPECTION_ENABLED=True,
        MEDIA_INSPECTION_YTDLP_PATH="/usr/bin/yt-dlp",
        MEDIA_DOWNLOADS_ENABLED=True,
        MEDIA_BROWSER_DELIVERY_ENABLED=False,
        WORKER_CONCURRENCY=1,
        MEDIA_DOWNLOAD_CONCURRENCY=1,
    )
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.commit = AsyncMock()
    session_factory = MagicMock(return_value=session)

    insp_repo = MagicMock()
    insp_repo.database_now = AsyncMock(return_value=__import__("datetime").datetime.now(
        tz=__import__("datetime").UTC
    ))
    insp_repo.reclaim_expired_leases = AsyncMock(return_value=0)
    insp_repo.expire_due_jobs = AsyncMock(return_value=0)
    claimed_job = MagicMock()
    claimed_job.id = uuid.uuid4()
    claimed_job.fence_token = 1
    claimed_job.provider_id = "vk"
    claimed_job.canonical_provider_url = "https://vk.com/video-1_2"
    claimed_job.media_id = "-1_2"
    claimed_job.hostname = "vk.com"
    claimed_job.path = "/video-1_2"
    claimed_job.scheme = "https"
    claimed_job.port = None
    claimed_job.attempt_count = 1
    insp_repo.claim_next = AsyncMock(return_value=[claimed_job])

    dl_repo = MagicMock()
    dl_repo.database_now = AsyncMock(return_value=insp_repo.database_now.return_value)
    dl_repo.reclaim_expired_leases = AsyncMock(return_value=0)
    dl_repo.expire_due_jobs = AsyncMock(
        side_effect=RuntimeError("simulated hygiene fault")
    )
    dl_repo.list_active_fences = AsyncMock(return_value=frozenset())
    dl_repo.list_referenced_artifact_ids = AsyncMock(return_value=frozenset())
    dl_repo.claim_next = AsyncMock(return_value=[])

    claimed = asyncio.Event()

    async def _set_claimed(_snap: object) -> None:
        claimed.set()

    runner = MediaJobWorkerRunner(
        settings,
        engine=MagicMock(dispose=AsyncMock()),
        session_factory=session_factory,
        http_client=MagicMock(aclose=AsyncMock()),
    )
    runner._run_claimed = AsyncMock(side_effect=_set_claimed)  # type: ignore[method-assign]
    runner._ensure_download_executor = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(
            artifact_store=MagicMock(
                delete_artifact=MagicMock(return_value=True),
                reconcile=MagicMock(return_value=0),
            )
        )
    )

    with (
        patch(
            "fetchnow.jobs.worker_loop.MediaJobRepository",
            return_value=insp_repo,
        ),
        patch(
            "fetchnow.jobs.worker_loop.MediaDownloadJobRepository",
            return_value=dl_repo,
        ),
        patch(
            "fetchnow.jobs.worker_loop.QuotaReconciler",
            return_value=MagicMock(run=AsyncMock(return_value=MagicMock(
                consumed=0, released=0, expired=0, entries_deleted=0, clients_deleted=0
            ))),
        ),
    ):
        await runner._poll_once()
        await asyncio.wait_for(claimed.wait(), timeout=1.0)

    dl_repo.expire_due_jobs.assert_awaited()
    insp_repo.claim_next.assert_awaited()
    session.commit.assert_awaited()


@pytest.mark.asyncio
async def test_hygiene_cancellation_propagates() -> None:
    settings = Settings(
        APP_ENV="test",
        LOG_LEVEL="WARNING",
        DATABASE_URL="postgresql+asyncpg://fetchnow:fetchnow@127.0.0.1:5432/fetchnow",
        MEDIA_JOBS_ENABLED=False,
        MEDIA_INSPECTION_ENABLED=False,
        MEDIA_DOWNLOADS_ENABLED=True,
        MEDIA_BROWSER_DELIVERY_ENABLED=False,
        WORKER_CONCURRENCY=1,
    )
    runner = MediaJobWorkerRunner(
        settings,
        engine=MagicMock(dispose=AsyncMock()),
        session_factory=MagicMock(),
        http_client=MagicMock(aclose=AsyncMock()),
    )

    async def _cancel_step() -> None:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await runner._run_hygiene_step("download_hygiene", _cancel_step)


@pytest.mark.asyncio
async def test_batch_order_does_not_starve_later_due_jobs(
    session_factory: async_sessionmaker[AsyncSession], migrated_database: str
) -> None:
    """Deferred live-lease job must not block later due siblings in one batch."""
    settings = _settings(migrated_database)
    async with session_factory() as session:
        await _cleanup(session)
        blocked_parent, _ = await _parent_with_token(session, suffix="50")
        ok_parent, _ = await _parent_with_token(session, suffix="51")
        blocked_client = await _identity(session)
        ok_client = await _identity(session)
        blocked = await _ready_free(
            session,
            settings=settings,
            parent=blocked_parent,
            client=blocked_client,
            format_option_id="fmt_blocked",
            artifact_bytes=40,
        )
        ok = await _ready_free(
            session,
            settings=settings,
            parent=ok_parent,
            client=ok_client,
            format_option_id="fmt_ok",
        )
        entry = await session.scalar(
            select(FreeDownloadQuotaEntry).where(
                FreeDownloadQuotaEntry.download_job_id == blocked.id
            )
        )
        assert entry is not None and blocked.artifact_id is not None
        now = await QuotaRepository(session).database_now()
        # Make blocked expire earlier so it is visited first.
        blocked.expires_at = now - timedelta(seconds=30)
        blocked.created_at = now - timedelta(hours=3)
        ok.expires_at = now - timedelta(seconds=5)
        ok.created_at = now - timedelta(hours=2)
        session.add(
            FreeDownloadDeliveryRange(
                id=uuid.uuid4(),
                quota_entry_id=entry.id,
                artifact_id=blocked.artifact_id,
                fence_token=int(blocked.fence_token),
                artifact_bytes=40,
                request_start=0,
                request_end_exclusive=8,
                served_end_exclusive=2,
                state="active",
                started_at=now,
                lease_expires_at=now + timedelta(seconds=120),
                closed_at=None,
            )
        )
        QuotaRepository(session).release_locked(entry, now=now)
        blocked_id, ok_id, ok_artifact = blocked.id, ok.id, ok.artifact_id
        await session.commit()

    async with session_factory() as session:
        repo = MediaDownloadJobRepository(session)
        now = await repo.database_now()
        arts = await repo.expire_due_jobs(now)
        await session.commit()
        blocked_loaded = await session.get(MediaDownloadJob, blocked_id)
        ok_loaded = await session.get(MediaDownloadJob, ok_id)
        assert blocked_loaded is not None and ok_loaded is not None
        assert blocked_loaded.public_state == MediaDownloadJobState.READY.value
        assert ok_loaded.public_state == MediaDownloadJobState.EXPIRED.value
        assert ok_artifact in arts
