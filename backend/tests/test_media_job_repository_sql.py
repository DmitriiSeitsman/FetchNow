"""Unit tests for media-job repository SQL claim/fencing predicates."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from fetchnow.jobs.errors import JobError, JobErrorCode
from fetchnow.jobs.models import MediaJob
from fetchnow.jobs.repository import MediaJobRepository
from fetchnow.jobs.states import MediaJobState


def test_claim_select_compiles_with_skip_locked() -> None:
    repo = MediaJobRepository(session=MagicMock())
    now = datetime.now(tz=UTC)
    stmt = repo._claim_select(now=now, limit=1)
    compiled = str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": False},
        )
    ).upper()
    assert "FOR UPDATE" in compiled
    assert "SKIP LOCKED" in compiled


@pytest.mark.asyncio
async def test_complete_requires_owner_and_fence_predicate() -> None:
    """Stale fence conceptually cannot complete: UPDATE predicate must match."""
    session = MagicMock()
    result = MagicMock()
    result.rowcount = 0
    session.execute = AsyncMock(return_value=result)
    repo = MediaJobRepository(session)
    applied = await repo.complete(
        job_id=uuid4(),
        owner="worker-a",
        fence=1,
        metadata_json={"providerId": "vk"},
        now=datetime.now(tz=UTC),
    )
    assert applied is False
    session.execute.assert_awaited_once()
    stmt = session.execute.await_args.args[0]
    compiled = str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": False},
        )
    )
    assert "lease_owner" in compiled
    assert "fence_token" in compiled
    assert "public_state" in compiled
    # Bound parameter carries the inspecting state value.
    params = stmt.compile(dialect=postgresql.dialect()).params
    assert MediaJobState.INSPECTING.value in params.values()


@pytest.mark.asyncio
async def test_fail_retry_rejects_mismatched_fence() -> None:
    job = MediaJob(
        id=uuid4(),
        schema_version=1,
        result_version=0,
        public_state=MediaJobState.INSPECTING.value,
        provider_id="vk",
        canonical_provider_url="https://vk.com/video-1_2",
        media_id="-1_2",
        hostname="vk.com",
        path="/video-1_2",
        scheme="https",
        port=None,
        credential_hash=b"\x00" * 32,
        request_fingerprint=b"\x01" * 32,
        result_metadata=None,
        public_error_code=None,
        attempt_count=1,
        max_attempts=3,
        available_at=datetime.now(tz=UTC),
        lease_owner="worker-a",
        lease_expires_at=datetime.now(tz=UTC),
        fence_token=7,
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
        started_at=datetime.now(tz=UTC),
        completed_at=None,
        expires_at=datetime.now(tz=UTC),
    )
    session = MagicMock()
    repo = MediaJobRepository(session)
    with patch(
        "fetchnow.jobs.repository.MediaJobRepository.get_by_id",
        new=AsyncMock(return_value=job),
    ):
        applied = await repo.fail_retry(
            job_id=job.id,
            owner="worker-a",
            fence=6,
            now=datetime.now(tz=UTC),
            backoff_seconds=2.0,
            public_error_code="MEDIA_INSPECTION_FAILED",
        )
    assert applied is False


@pytest.mark.asyncio
async def test_enqueue_rejects_query_in_canonical() -> None:
    repo = MediaJobRepository(session=MagicMock())
    with pytest.raises(JobError) as exc_info:
        await repo.enqueue_or_get(
            credential_hash=b"\x00" * 32,
            request_fingerprint=b"\x01" * 32,
            provider_id="vk",
            canonical_provider_url="https://vk.com/video-1_2?x=1",
            media_id="-1_2",
            hostname="vk.com",
            path="/video-1_2",
            scheme="https",
            port=None,
            now=datetime.now(tz=UTC),
            ttl_seconds=60,
            max_attempts=3,
        )
    assert exc_info.value.code == JobErrorCode.INTERNAL_ERROR
    assert exc_info.value.internal_reason == "CANONICAL_QUERY_OR_FRAGMENT"


def _expire_job(
    *,
    public_state: str,
    result_metadata: dict[str, object] | None = None,
    public_error_code: str | None = None,
    completed_at: datetime | None = None,
    lease_owner: str | None = None,
    lease_expires_at: datetime | None = None,
    fence_token: int = 4,
) -> MediaJob:
    now = datetime.now(tz=UTC)
    return MediaJob(
        id=uuid4(),
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
        credential_hash=b"\x00" * 32,
        request_fingerprint=b"\x01" * 32,
        result_metadata=result_metadata,
        public_error_code=public_error_code,
        attempt_count=1,
        max_attempts=3,
        available_at=now,
        lease_owner=lease_owner,
        lease_expires_at=lease_expires_at,
        fence_token=fence_token,
        created_at=now,
        updated_at=now,
        started_at=now,
        completed_at=completed_at,
        expires_at=now,
    )


async def _run_expire(job: MediaJob, now: datetime) -> tuple[int, MagicMock]:
    session = MagicMock()
    scalars_result = MagicMock()
    scalars_result.all.return_value = [job]
    session.scalars = AsyncMock(return_value=scalars_result)
    session.flush = AsyncMock()
    repo = MediaJobRepository(session)
    count = await repo.expire_due_jobs(now)
    return count, session


@pytest.mark.asyncio
async def test_expire_select_compiles_with_skip_locked() -> None:
    job = _expire_job(public_state=MediaJobState.QUEUED.value)
    now = datetime.now(tz=UTC)
    _, session = await _run_expire(job, now)
    stmt = session.scalars.await_args.args[0]
    compiled = str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": False},
        )
    ).upper()
    assert "FOR UPDATE" in compiled
    assert "SKIP LOCKED" in compiled


@pytest.mark.asyncio
async def test_expire_clears_inspected_result_and_preserves_completed_at() -> None:
    completed = datetime.now(tz=UTC)
    job = _expire_job(
        public_state=MediaJobState.INSPECTED.value,
        result_metadata={"providerId": "vk"},
        completed_at=completed,
        fence_token=3,
    )
    provider_id = job.provider_id
    credential_hash = job.credential_hash
    request_fingerprint = job.request_fingerprint
    expires_at = job.expires_at
    now = datetime.now(tz=UTC)
    count, session = await _run_expire(job, now)
    assert count == 1
    session.flush.assert_awaited_once()
    assert job.public_state == MediaJobState.EXPIRED.value
    assert job.result_metadata is None
    assert job.public_error_code is None
    assert job.completed_at == completed
    assert job.lease_owner is None
    assert job.lease_expires_at is None
    assert job.fence_token == 4
    assert job.updated_at == now
    assert job.provider_id == provider_id
    assert job.credential_hash == credential_hash
    assert job.request_fingerprint == request_fingerprint
    assert job.expires_at == expires_at


@pytest.mark.asyncio
async def test_expire_clears_failed_error_and_preserves_completed_at() -> None:
    completed = datetime.now(tz=UTC)
    job = _expire_job(
        public_state=MediaJobState.FAILED.value,
        public_error_code=JobErrorCode.MEDIA_INSPECTION_FAILED.value,
        completed_at=completed,
        fence_token=8,
    )
    now = datetime.now(tz=UTC)
    count, _ = await _run_expire(job, now)
    assert count == 1
    assert job.public_state == MediaJobState.EXPIRED.value
    assert job.result_metadata is None
    assert job.public_error_code is None
    assert job.completed_at == completed
    assert job.fence_token == 9


@pytest.mark.asyncio
async def test_expire_queued_sets_completed_at() -> None:
    job = _expire_job(public_state=MediaJobState.QUEUED.value, fence_token=0)
    now = datetime.now(tz=UTC)
    await _run_expire(job, now)
    assert job.public_state == MediaJobState.EXPIRED.value
    assert job.completed_at == now
    assert job.result_metadata is None
    assert job.public_error_code is None
    assert job.fence_token == 1


@pytest.mark.asyncio
async def test_expire_inspecting_drops_lease_and_sets_completed_at() -> None:
    lease_until = datetime.now(tz=UTC)
    job = _expire_job(
        public_state=MediaJobState.INSPECTING.value,
        lease_owner="worker-stale",
        lease_expires_at=lease_until,
        fence_token=2,
    )
    now = datetime.now(tz=UTC)
    await _run_expire(job, now)
    assert job.public_state == MediaJobState.EXPIRED.value
    assert job.lease_owner is None
    assert job.lease_expires_at is None
    assert job.completed_at == now
    assert job.result_metadata is None
    assert job.public_error_code is None
    assert job.fence_token == 3
