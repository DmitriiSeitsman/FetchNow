"""Real PostgreSQL acceptance tests for READY Free → Premium promotion."""

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
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fetchnow.core.config import Settings
from fetchnow.db.session import create_engine, create_session_factory
from fetchnow.delivery.service import DeliveryService
from fetchnow.downloads.errors import DownloadError, DownloadErrorCode
from fetchnow.downloads.grant_models import MediaDeliveryGrant
from fetchnow.downloads.grant_service import BrowserGrantService
from fetchnow.downloads.models import MediaDownloadJob
from fetchnow.downloads.promote import DownloadPremiumUpgradeService
from fetchnow.downloads.repository import MediaDownloadJobRepository
from fetchnow.downloads.service import DownloadJobService
from fetchnow.downloads.snapshot_codec import (
    attach_effective_policy_snapshot,
    decode_authorized_identity_id,
    decode_effective_policy_snapshot,
)
from fetchnow.jobs.credentials import (
    generate_access_token,
    hash_access_token,
    hash_request_fingerprint,
)
from fetchnow.jobs.models import MediaJob
from fetchnow.jobs.repository import MediaJobRepository
from fetchnow.payments.models import PaymentOrder
from fetchnow.premium.models import PremiumEntitlement
from fetchnow.quota.delivery import DeliveryQuotaAccounting
from fetchnow.quota.models import (
    AnonymousClient,
    FreeDownloadDeliveryRange,
    FreeDownloadQuotaEntry,
)
from fetchnow.quota.policy import effective_download_policy
from fetchnow.quota.reconcile import QuotaReconciler
from fetchnow.quota.repository import QuotaRepository
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


def _settings(url: str, *, quota_enabled: bool = True) -> Settings:
    return Settings(
        APP_ENV="test",
        DATABASE_URL=url,
        FREE_DOWNLOAD_QUOTA_ENABLED=quota_enabled,
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


_FORMAT = {
    "formatOptionId": "fmt_upgrade_p720",
    "container": "mp4",
    "width": 1280,
    "height": 720,
    "fps": 30,
    "hasVideo": True,
    "hasAudio": True,
    "category": "progressive",
    "videoCodec": "avc",
    "audioCodec": "aac",
    "approxBytes": 1_000_000,
    "qualityLabel": "p720",
    "freeTierEligible": True,
    "mediaKind": "normal_video",
    "requiresPremium": False,
    "bitrateKbps": None,
}


async def _cleanup(session: AsyncSession) -> None:
    await session.execute(text("DELETE FROM free_download_delivery_ranges"))
    await session.execute(text("DELETE FROM free_download_quota_entries"))
    await session.execute(text("DELETE FROM media_delivery_grants"))
    await session.execute(text("DELETE FROM premium_entitlements"))
    await session.execute(text("DELETE FROM payment_orders"))
    await session.execute(text("DELETE FROM anonymous_clients"))
    await session.execute(text("DELETE FROM media_download_jobs"))
    await session.execute(text("DELETE FROM media_jobs"))
    await session.commit()


async def _identity(session: AsyncSession) -> AnonymousClient:
    repo = QuotaRepository(session)
    now = await repo.database_now()
    token_hash = hash_anonymous_token(generate_anonymous_token())
    assert token_hash is not None
    return await repo.create_client(
        token_hash=token_hash, now=now, ttl_seconds=31_536_000
    )


async def _parent_with_token(session: AsyncSession) -> tuple[MediaJob, str]:
    repo = MediaJobRepository(session)
    now = await repo.database_now()
    token = generate_access_token()
    row, _ = await repo.enqueue_or_get(
        credential_hash=hash_access_token(token),
        request_fingerprint=hash_request_fingerprint(
            access_token=token,
            normalized_request="https://vk.com/video-upgrade-1",
        ),
        provider_id="vk",
        canonical_provider_url="https://vk.com/video-upgrade-1",
        media_id="upgrade-1",
        hostname="vk.com",
        path="/video-upgrade-1",
        scheme="https",
        port=None,
        now=now,
        ttl_seconds=3600,
        max_attempts=3,
    )
    return row, token


async def _ready_free_job(
    session: AsyncSession,
    *,
    parent: MediaJob,
    client: AnonymousClient,
    settings: Settings,
    artifact_bytes: int = 1_048_576,
) -> MediaDownloadJob:
    repo = MediaDownloadJobRepository(session)
    now = await repo.database_now()
    policy = effective_download_policy(settings)
    snapshot = attach_effective_policy_snapshot(dict(_FORMAT), policy)
    job, created = await repo.enqueue_or_get(
        media_job_id=parent.id,
        format_option_id=_FORMAT["formatOptionId"],
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
        worker_id="upgrade-worker", lease_seconds=60, now=now, limit=1
    )
    assert len(claimed) == 1
    artifact_id = uuid.uuid4()
    applied = await repo.complete_ready(
        job_id=claimed[0].id,
        owner="upgrade-worker",
        fence=int(claimed[0].fence_token),
        artifact_id=artifact_id,
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


async def _entitlement(
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


async def _add_range(
    session: AsyncSession,
    *,
    entry: FreeDownloadQuotaEntry,
    job: MediaDownloadJob,
    start: int,
    end: int,
    state: str = "closed",
) -> FreeDownloadDeliveryRange:
    now = await QuotaRepository(session).database_now()
    row = FreeDownloadDeliveryRange(
        id=uuid.uuid4(),
        quota_entry_id=entry.id,
        artifact_id=job.artifact_id,
        fence_token=int(job.fence_token),
        artifact_bytes=int(job.artifact_bytes or 0),
        request_start=start,
        request_end_exclusive=end,
        served_end_exclusive=end if state == "closed" else start,
        state=state,
        started_at=now,
        lease_expires_at=(now + timedelta(seconds=600) if state == "active" else None),
        closed_at=now if state == "closed" else None,
    )
    if state == "active":
        row.served_end_exclusive = start
        row.request_end_exclusive = end
    session.add(row)
    await session.flush()
    return row


@pytest.mark.asyncio
async def test_upgrade_releases_reserved_without_coverage(
    migrated_database: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    settings = _settings(migrated_database)
    async with session_factory() as session:
        await _cleanup(session)
        parent, token = await _parent_with_token(session)
        client = await _identity(session)
        job = await _ready_free_job(
            session, parent=parent, client=client, settings=settings
        )
        await _entitlement(session, client_id=client.id)
        artifact_id = job.artifact_id
        fence = int(job.fence_token)
        bytes_ = int(job.artifact_bytes or 0)

        result = await DownloadJobService(settings).upgrade_to_premium(
            download_job_id=job.id,
            access_token=token,
            anonymous_client_id=client.id,
            session=session,
        )
        await session.commit()

        assert result.created is True
        loaded = await MediaDownloadJobRepository(session).get_by_id(job.id)
        assert loaded is not None
        assert loaded.artifact_id == artifact_id
        assert int(loaded.fence_token) == fence
        assert int(loaded.artifact_bytes or 0) == bytes_
        policy = decode_effective_policy_snapshot(loaded.selected_format_snapshot)
        assert policy is not None
        assert policy.tier == "premium"
        assert policy.delivery_rate_bytes_per_second is None
        assert (
            decode_authorized_identity_id(loaded.selected_format_snapshot) == client.id
        )
        entry = await QuotaRepository(session).lock_entry_for_download(job.id)
        assert entry is not None
        assert entry.state == "released"


@pytest.mark.asyncio
async def test_upgrade_consumes_when_coverage_complete(
    migrated_database: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    settings = _settings(migrated_database)
    async with session_factory() as session:
        await _cleanup(session)
        parent, token = await _parent_with_token(session)
        client = await _identity(session)
        job = await _ready_free_job(
            session,
            parent=parent,
            client=client,
            settings=settings,
            artifact_bytes=1000,
        )
        entry = await QuotaRepository(session).lock_entry_for_download(job.id)
        assert entry is not None
        await _add_range(session, entry=entry, job=job, start=0, end=1000)
        await _entitlement(session, client_id=client.id)

        await DownloadJobService(settings).upgrade_to_premium(
            download_job_id=job.id,
            access_token=token,
            anonymous_client_id=client.id,
            session=session,
        )
        await session.commit()
        entry = await QuotaRepository(session).lock_entry_for_download(job.id)
        assert entry is not None
        assert entry.state == "consumed"
        assert entry.consumed_at is not None
        ranges = list(
            (
                await session.scalars(
                    select(FreeDownloadDeliveryRange).where(
                        FreeDownloadDeliveryRange.quota_entry_id == entry.id
                    )
                )
            ).all()
        )
        assert len(ranges) == 1


@pytest.mark.asyncio
async def test_upgrade_rejects_active_lease_and_inactive_premium(
    migrated_database: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    settings = _settings(migrated_database)
    async with session_factory() as session:
        await _cleanup(session)
        parent, token = await _parent_with_token(session)
        client = await _identity(session)
        job = await _ready_free_job(
            session, parent=parent, client=client, settings=settings
        )
        with pytest.raises(DownloadError) as inactive:
            await DownloadJobService(settings).upgrade_to_premium(
                download_job_id=job.id,
                access_token=token,
                anonymous_client_id=client.id,
                session=session,
            )
        assert (
            inactive.value.code is DownloadErrorCode.MEDIA_CAPABILITY_REQUIRES_PREMIUM
        )
        await session.rollback()

        await _cleanup(session)
        parent, token = await _parent_with_token(session)
        client = await _identity(session)
        job = await _ready_free_job(
            session, parent=parent, client=client, settings=settings
        )
        entry = await QuotaRepository(session).lock_entry_for_download(job.id)
        assert entry is not None
        await _add_range(
            session, entry=entry, job=job, start=0, end=100, state="active"
        )
        await _entitlement(session, client_id=client.id)
        with pytest.raises(DownloadError) as busy:
            await DownloadJobService(settings).upgrade_to_premium(
                download_job_id=job.id,
                access_token=token,
                anonymous_client_id=client.id,
                session=session,
            )
        assert busy.value.code is DownloadErrorCode.DELIVERY_IN_PROGRESS


@pytest.mark.asyncio
async def test_upgrade_idempotent_and_revokes_grants_once(
    migrated_database: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    settings = _settings(migrated_database)
    async with session_factory() as session:
        await _cleanup(session)
        parent, token = await _parent_with_token(session)
        client = await _identity(session)
        job = await _ready_free_job(
            session, parent=parent, client=client, settings=settings
        )
        await _entitlement(session, client_id=client.id)
        grant_service = BrowserGrantService(settings)
        first_grant = await grant_service.issue(
            download_job_id=job.id, access_token=token, session=session
        )
        await session.commit()

        service = DownloadJobService(settings)
        first = await service.upgrade_to_premium(
            download_job_id=job.id,
            access_token=token,
            anonymous_client_id=client.id,
            session=session,
        )
        await session.commit()
        assert first.created is True
        revoked = await session.get(MediaDeliveryGrant, first_grant.grant_id)
        assert revoked is not None
        assert revoked.revoked_at is not None

        premium_grant = await grant_service.issue(
            download_job_id=job.id, access_token=token, session=session
        )
        await session.commit()
        second = await service.upgrade_to_premium(
            download_job_id=job.id,
            access_token=token,
            anonymous_client_id=client.id,
            session=session,
        )
        await session.commit()
        assert second.created is False
        still_active = await session.get(MediaDeliveryGrant, premium_grant.grant_id)
        assert still_active is not None
        assert still_active.revoked_at is None


@pytest.mark.asyncio
async def test_upgrade_fails_closed_for_free_released_and_reconciler_accepts_premium(
    migrated_database: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    settings = _settings(migrated_database)
    async with session_factory() as session:
        await _cleanup(session)
        parent, token = await _parent_with_token(session)
        client = await _identity(session)
        job = await _ready_free_job(
            session, parent=parent, client=client, settings=settings
        )
        entry = await QuotaRepository(session).lock_entry_for_download(job.id)
        assert entry is not None
        QuotaRepository.release_locked(
            entry, now=await QuotaRepository(session).database_now()
        )
        await session.flush()
        await _entitlement(session, client_id=client.id)
        with pytest.raises(DownloadError) as incoherent:
            await DownloadJobService(settings).upgrade_to_premium(
                download_job_id=job.id,
                access_token=token,
                anonymous_client_id=client.id,
                session=session,
            )
        assert incoherent.value.code is DownloadErrorCode.QUOTA_STATE_INCOHERENT
        await session.rollback()

        await _cleanup(session)
        parent, token = await _parent_with_token(session)
        client = await _identity(session)
        job = await _ready_free_job(
            session, parent=parent, client=client, settings=settings
        )
        await _entitlement(session, client_id=client.id)
        await DownloadJobService(settings).upgrade_to_premium(
            download_job_id=job.id,
            access_token=token,
            anonymous_client_id=client.id,
            session=session,
        )
        await session.commit()
        result = await QuotaReconciler(settings).run(session=session, limit=16)
        await session.commit()
        assert result.consumed == 0


@pytest.mark.asyncio
async def test_concurrent_upgrade_requests_serialize(
    migrated_database: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    settings = _settings(migrated_database)

    async def _once() -> bool:
        async with session_factory() as session:
            parent, token = await _parent_with_token(session)
            client = await _identity(session)
            job = await _ready_free_job(
                session, parent=parent, client=client, settings=settings
            )
            await _entitlement(session, client_id=client.id)
            await session.commit()
            job_id = job.id
            client_id = client.id
            access = token

        async def _upgrade() -> bool:
            async with session_factory() as session:
                view = await DownloadJobService(settings).upgrade_to_premium(
                    download_job_id=job_id,
                    access_token=access,
                    anonymous_client_id=client_id,
                    session=session,
                )
                await session.commit()
                return view.created

        created = await asyncio.gather(_upgrade(), _upgrade())
        return created.count(True) == 1 and created.count(False) == 1

    assert await _once() is True


@pytest.mark.asyncio
async def test_wrong_identity_rejected(
    migrated_database: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    settings = _settings(migrated_database)
    async with session_factory() as session:
        await _cleanup(session)
        parent, token = await _parent_with_token(session)
        owner = await _identity(session)
        other = await _identity(session)
        job = await _ready_free_job(
            session, parent=parent, client=owner, settings=settings
        )
        await _entitlement(session, client_id=other.id)
        with pytest.raises(DownloadError) as forbidden:
            await DownloadPremiumUpgradeService(settings).upgrade(
                download_job_id=job.id,
                access_token=token,
                anonymous_client_id=other.id,
                session=session,
            )
        assert forbidden.value.code is DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND


@pytest.mark.asyncio
async def test_upgrade_releases_partial_coverage_and_rejects_old_grant(
    migrated_database: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    settings = _settings(migrated_database)
    async with session_factory() as session:
        await _cleanup(session)
        parent, token = await _parent_with_token(session)
        client = await _identity(session)
        job = await _ready_free_job(
            session,
            parent=parent,
            client=client,
            settings=settings,
            artifact_bytes=1000,
        )
        entry = await QuotaRepository(session).lock_entry_for_download(job.id)
        assert entry is not None
        await _add_range(session, entry=entry, job=job, start=0, end=400)
        await _entitlement(session, client_id=client.id)
        grant_service = BrowserGrantService(settings)
        old_grant = await grant_service.issue(
            download_job_id=job.id, access_token=token, session=session
        )
        await session.commit()

        await DownloadJobService(settings).upgrade_to_premium(
            download_job_id=job.id,
            access_token=token,
            anonymous_client_id=client.id,
            session=session,
        )
        await session.commit()

        entry = await QuotaRepository(session).lock_entry_for_download(job.id)
        assert entry is not None
        assert entry.state == "released"
        ranges = list(
            (
                await session.scalars(
                    select(FreeDownloadDeliveryRange).where(
                        FreeDownloadDeliveryRange.quota_entry_id == entry.id
                    )
                )
            ).all()
        )
        assert len(ranges) == 1
        assert int(ranges[0].request_start) == 0
        assert int(ranges[0].served_end_exclusive) == 400

        delivery = DeliveryService(settings, reader=MagicMock())
        with pytest.raises(DownloadError) as expired:
            await delivery.authorize_browser_grant(
                grant_id=old_grant.grant_id,
                raw_token=old_grant.raw_token,
                session=session,
            )
        assert expired.value.code is DownloadErrorCode.DOWNLOAD_EXPIRED


@pytest.mark.asyncio
async def test_upgrade_preserves_consumed_and_does_not_extend_expiry(
    migrated_database: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    settings = _settings(migrated_database)
    async with session_factory() as session:
        await _cleanup(session)
        parent, token = await _parent_with_token(session)
        client = await _identity(session)
        job = await _ready_free_job(
            session,
            parent=parent,
            client=client,
            settings=settings,
            artifact_bytes=500,
        )
        entry = await QuotaRepository(session).lock_entry_for_download(job.id)
        assert entry is not None
        await _add_range(session, entry=entry, job=job, start=0, end=500)
        now = await QuotaRepository(session).database_now()
        QuotaRepository.consume_locked(entry, now=now)
        consumed_at = entry.consumed_at
        await _entitlement(session, client_id=client.id)
        await session.commit()

        service = DownloadJobService(settings)
        first = await service.upgrade_to_premium(
            download_job_id=job.id,
            access_token=token,
            anonymous_client_id=client.id,
            session=session,
        )
        await session.commit()
        assert first.created is True
        entry = await QuotaRepository(session).lock_entry_for_download(job.id)
        assert entry is not None
        assert entry.state == "consumed"
        assert entry.consumed_at == consumed_at

        loaded = await MediaDownloadJobRepository(session).get_by_id(job.id)
        assert loaded is not None
        first_policy = decode_effective_policy_snapshot(loaded.selected_format_snapshot)
        assert first_policy is not None
        first_expiry = first_policy.premium_expires_at

        second = await service.upgrade_to_premium(
            download_job_id=job.id,
            access_token=token,
            anonymous_client_id=client.id,
            session=session,
        )
        await session.commit()
        assert second.created is False
        loaded = await MediaDownloadJobRepository(session).get_by_id(job.id)
        assert loaded is not None
        again = decode_effective_policy_snapshot(loaded.selected_format_snapshot)
        assert again is not None
        assert again.premium_expires_at == first_expiry


@pytest.mark.asyncio
async def test_upgrade_quota_disabled_without_manufacturing_entry(
    migrated_database: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    settings = _settings(migrated_database, quota_enabled=False)
    async with session_factory() as session:
        await _cleanup(session)
        parent, token = await _parent_with_token(session)
        client = await _identity(session)
        repo = MediaDownloadJobRepository(session)
        now = await repo.database_now()
        policy = effective_download_policy(settings)
        snapshot = attach_effective_policy_snapshot(dict(_FORMAT), policy)
        job, created = await repo.enqueue_or_get(
            media_job_id=parent.id,
            format_option_id=_FORMAT["formatOptionId"],
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
        claimed = await repo.claim_next(
            worker_id="upgrade-worker", lease_seconds=60, now=now, limit=1
        )
        assert len(claimed) == 1
        applied = await repo.complete_ready(
            job_id=claimed[0].id,
            owner="upgrade-worker",
            fence=int(claimed[0].fence_token),
            artifact_id=uuid.uuid4(),
            artifact_bytes=2048,
            artifact_container="mp4",
            artifact_content_type="video/mp4",
            now=now,
            consume_quota_on_ready=False,
        )
        assert applied is True
        await _entitlement(session, client_id=client.id)
        await session.commit()

        view = await DownloadJobService(settings).upgrade_to_premium(
            download_job_id=claimed[0].id,
            access_token=token,
            anonymous_client_id=client.id,
            session=session,
        )
        await session.commit()
        assert view.created is True
        entry = await QuotaRepository(session).lock_entry_for_download(claimed[0].id)
        assert entry is None
        loaded = await MediaDownloadJobRepository(session).get_by_id(claimed[0].id)
        assert loaded is not None
        policy = decode_effective_policy_snapshot(loaded.selected_format_snapshot)
        assert policy is not None
        assert policy.tier == "premium"


@pytest.mark.asyncio
async def test_authorize_then_promote_then_begin_fails_closed_on_revoked_grant(
    migrated_database: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Old grant authorized before promote must not start Free accounting after.

    Interleaving: authorize browser grant → promote (snapshot + revoke) →
    accounting.begin revalidates and fail-closes. No refund, no new Free
    attempt, no mid-response pacing switch (stream never starts).
    """
    settings = _settings(migrated_database)
    async with session_factory() as session:
        await _cleanup(session)
        parent, token = await _parent_with_token(session)
        client = await _identity(session)
        job = await _ready_free_job(
            session,
            parent=parent,
            client=client,
            settings=settings,
            artifact_bytes=1000,
        )
        entry = await QuotaRepository(session).lock_entry_for_download(job.id)
        assert entry is not None
        await _add_range(session, entry=entry, job=job, start=0, end=400)
        await _entitlement(session, client_id=client.id)
        grant_service = BrowserGrantService(settings)
        old_grant = await grant_service.issue(
            download_job_id=job.id, access_token=token, session=session
        )
        await session.commit()

        job_id = job.id
        delivery = DeliveryService(settings, reader=MagicMock())
        authz = await delivery.authorize_browser_grant(
            grant_id=old_grant.grant_id,
            raw_token=old_grant.raw_token,
            session=session,
        )
        assert authz.policy is not None
        assert authz.policy.tier == "free"
        free_rate = authz.policy.delivery_rate_bytes_per_second

        await DownloadJobService(settings).upgrade_to_premium(
            download_job_id=job_id,
            access_token=token,
            anonymous_client_id=client.id,
            session=session,
        )
        await session.commit()

        entry = await QuotaRepository(session).lock_entry_for_download(job_id)
        assert entry is not None
        assert entry.state == "released"

        accounting = DeliveryQuotaAccounting(compatibility_mode=False)

        async def revalidate(fresh: AsyncSession):
            return await delivery.authorize_browser_grant(
                grant_id=old_grant.grant_id,
                raw_token=old_grant.raw_token,
                session=fresh,
            )

        with pytest.raises(DownloadError) as blocked:
            await accounting.begin(
                authorization=authz,
                start=0,
                end=1000,
                session=session,
                revalidate=revalidate,
            )
        assert blocked.value.code is DownloadErrorCode.DOWNLOAD_EXPIRED
        await session.rollback()

        entry = await QuotaRepository(session).lock_entry_for_download(job_id)
        assert entry is not None
        assert entry.state == "released"
        active = list(
            (
                await session.scalars(
                    select(FreeDownloadDeliveryRange).where(
                        FreeDownloadDeliveryRange.quota_entry_id == entry.id,
                        FreeDownloadDeliveryRange.state == "active",
                    )
                )
            ).all()
        )
        assert active == []
        # Authorize-time Free rate is retained on the stale authz object only;
        # begin fail-closes before any byte is paced with that rate.
        assert free_rate is not None
        assert free_rate > 0
