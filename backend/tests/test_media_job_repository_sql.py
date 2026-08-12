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
