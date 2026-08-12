"""Atomic PostgreSQL persistence for media-inspection jobs."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Select, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fetchnow.jobs.credentials import tokens_match
from fetchnow.jobs.errors import JobErrorCode, raise_job_error
from fetchnow.jobs.models import MediaJob
from fetchnow.jobs.states import MediaJobState, assert_transition


class MediaJobRepository:
    """Session-bound repository for enqueue/claim/complete/expire operations."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def database_now(self) -> datetime:
        """Read the authoritative PostgreSQL wall clock for this transaction."""
        value = await self._session.scalar(select(func.clock_timestamp()))
        if not isinstance(value, datetime):
            raise_job_error(
                JobErrorCode.INTERNAL_ERROR,
                internal_reason="DATABASE_CLOCK_INVALID",
            )
        return value

    async def get_by_credential_hash(self, credential_hash: bytes) -> MediaJob | None:
        """Find an idempotent request without resolving its URL again."""
        result = await self._session.scalar(
            select(MediaJob).where(MediaJob.credential_hash == credential_hash)
        )
        return result if isinstance(result, MediaJob) else None

    async def enqueue_or_get(
        self,
        *,
        credential_hash: bytes,
        request_fingerprint: bytes,
        provider_id: str,
        canonical_provider_url: str,
        media_id: str,
        hostname: str,
        path: str,
        scheme: str,
        port: int | None,
        now: datetime,
        ttl_seconds: int,
        max_attempts: int,
    ) -> tuple[MediaJob, bool]:
        """Insert a queued job or return the existing one for the same credential.

        Concurrent creates rely on UNIQUE(credential_hash). On conflict, matching
        fingerprints return the existing job; mismatched fingerprints raise
        IDEMPOTENCY_CONFLICT.
        """
        if len(credential_hash) != 32 or len(request_fingerprint) != 32:
            raise_job_error(
                JobErrorCode.INTERNAL_ERROR,
                internal_reason="HASH_LENGTH",
            )
        if "?" in canonical_provider_url or "#" in canonical_provider_url:
            raise_job_error(
                JobErrorCode.INTERNAL_ERROR,
                internal_reason="CANONICAL_QUERY_OR_FRAGMENT",
            )

        expires_at = now + timedelta(seconds=ttl_seconds)
        job = MediaJob(
            id=uuid.uuid4(),
            schema_version=1,
            result_version=0,
            public_state=MediaJobState.QUEUED.value,
            provider_id=provider_id,
            canonical_provider_url=canonical_provider_url,
            media_id=media_id,
            hostname=hostname,
            path=path,
            scheme=scheme,
            port=port,
            credential_hash=credential_hash,
            request_fingerprint=request_fingerprint,
            result_metadata=None,
            public_error_code=None,
            attempt_count=0,
            max_attempts=max_attempts,
            available_at=now,
            lease_owner=None,
            lease_expires_at=None,
            fence_token=0,
            created_at=now,
            updated_at=now,
            started_at=None,
            completed_at=None,
            expires_at=expires_at,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(job)
                await self._session.flush()
            return job, True
        except IntegrityError:
            existing = await self._session.scalar(
                select(MediaJob).where(MediaJob.credential_hash == credential_hash)
            )
            if existing is None:
                raise_job_error(
                    JobErrorCode.INTERNAL_ERROR,
                    internal_reason="IDEMPOTENCY_LOOKUP_MISS",
                )
            if not tokens_match(existing.request_fingerprint, request_fingerprint):
                raise_job_error(
                    JobErrorCode.IDEMPOTENCY_CONFLICT,
                    internal_reason="FINGERPRINT_MISMATCH",
                )
            return existing, False

    async def get_by_id(self, job_id: uuid.UUID) -> MediaJob | None:
        """Load a job by primary key."""
        result = await self._session.scalar(
            select(MediaJob).where(MediaJob.id == job_id)
        )
        return result if isinstance(result, MediaJob) else None

    def _claim_select(self, *, now: datetime, limit: int) -> Select[tuple[MediaJob]]:
        return (
            select(MediaJob)
            .where(
                MediaJob.public_state == MediaJobState.QUEUED.value,
                MediaJob.available_at <= func.clock_timestamp(),
                MediaJob.expires_at > func.clock_timestamp(),
                MediaJob.attempt_count < MediaJob.max_attempts,
            )
            .order_by(MediaJob.created_at.asc(), MediaJob.id.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )

    async def claim_next(
        self,
        *,
        worker_id: str,
        lease_seconds: float,
        now: datetime,
        limit: int = 1,
    ) -> list[MediaJob]:
        """Atomically claim up to ``limit`` queued jobs (SKIP LOCKED)."""
        if limit < 1:
            return []
        if not worker_id or len(worker_id) > 128:
            raise_job_error(
                JobErrorCode.INTERNAL_ERROR,
                internal_reason="INVALID_WORKER_ID",
            )
        claim_stmt = self._claim_select(now=now, limit=limit)
        rows = list((await self._session.scalars(claim_stmt)).all())
        claimed: list[MediaJob] = []
        lease_expires = now + timedelta(seconds=lease_seconds)
        for job in rows:
            assert_transition(job.public_state, MediaJobState.INSPECTING)
            job.public_state = MediaJobState.INSPECTING.value
            job.lease_owner = worker_id
            job.lease_expires_at = lease_expires
            job.fence_token = int(job.fence_token) + 1
            job.attempt_count = int(job.attempt_count) + 1
            job.updated_at = now
            if job.started_at is None:
                job.started_at = now
            claimed.append(job)
        if claimed:
            await self._session.flush()
        return claimed

    async def complete(
        self,
        *,
        job_id: uuid.UUID,
        owner: str,
        fence: int,
        metadata_json: dict[str, Any],
        now: datetime,
    ) -> bool:
        """Mark inspecting job inspected when lease owner + fence still match."""
        assert_transition(MediaJobState.INSPECTING, MediaJobState.INSPECTED)
        result = await self._session.execute(
            update(MediaJob)
            .where(
                MediaJob.id == job_id,
                MediaJob.public_state == MediaJobState.INSPECTING.value,
                MediaJob.lease_owner == owner,
                MediaJob.fence_token == fence,
                MediaJob.lease_expires_at > func.clock_timestamp(),
                MediaJob.expires_at > func.clock_timestamp(),
            )
            .values(
                public_state=MediaJobState.INSPECTED.value,
                result_metadata=metadata_json,
                public_error_code=None,
                result_version=1,
                lease_owner=None,
                lease_expires_at=None,
                completed_at=func.clock_timestamp(),
                updated_at=func.clock_timestamp(),
            )
        )
        return bool(result.rowcount)

    async def fail_permanent(
        self,
        *,
        job_id: uuid.UUID,
        owner: str,
        fence: int,
        public_error_code: str,
        now: datetime,
    ) -> bool:
        """Terminal failure for the current lease owner."""
        assert_transition(MediaJobState.INSPECTING, MediaJobState.FAILED)
        result = await self._session.execute(
            update(MediaJob)
            .where(
                MediaJob.id == job_id,
                MediaJob.public_state == MediaJobState.INSPECTING.value,
                MediaJob.lease_owner == owner,
                MediaJob.fence_token == fence,
                MediaJob.lease_expires_at > func.clock_timestamp(),
                MediaJob.expires_at > func.clock_timestamp(),
            )
            .values(
                public_state=MediaJobState.FAILED.value,
                public_error_code=public_error_code,
                result_metadata=None,
                lease_owner=None,
                lease_expires_at=None,
                completed_at=func.clock_timestamp(),
                updated_at=func.clock_timestamp(),
            )
        )
        return bool(result.rowcount)

    async def fail_retry(
        self,
        *,
        job_id: uuid.UUID,
        owner: str,
        fence: int,
        now: datetime,
        backoff_seconds: float,
        public_error_code: str,
    ) -> bool:
        """Reschedule to queued with backoff, or terminal-fail when attempts exhausted.

        Requires matching lease_owner and fence_token. Returns False when the
        predicate misses (stale worker / reclaimed lease).
        """
        job = await self.get_by_id(job_id)
        if job is None:
            return False
        if (
            job.public_state != MediaJobState.INSPECTING.value
            or job.lease_owner != owner
            or int(job.fence_token) != int(fence)
        ):
            return False

        if int(job.attempt_count) >= int(job.max_attempts):
            return await self.fail_permanent(
                job_id=job_id,
                owner=owner,
                fence=fence,
                public_error_code=public_error_code,
                now=now,
            )

        assert_transition(MediaJobState.INSPECTING, MediaJobState.QUEUED)
        available_at = now + timedelta(seconds=max(0.0, backoff_seconds))
        result = await self._session.execute(
            update(MediaJob)
            .where(
                MediaJob.id == job_id,
                MediaJob.public_state == MediaJobState.INSPECTING.value,
                MediaJob.lease_owner == owner,
                MediaJob.fence_token == fence,
                MediaJob.lease_expires_at > func.clock_timestamp(),
                MediaJob.expires_at > func.clock_timestamp(),
            )
            .values(
                public_state=MediaJobState.QUEUED.value,
                public_error_code=None,
                available_at=available_at,
                lease_owner=None,
                lease_expires_at=None,
                updated_at=func.clock_timestamp(),
            )
        )
        return bool(result.rowcount)

    async def release_to_queued(
        self,
        *,
        job_id: uuid.UUID,
        owner: str,
        fence: int,
        now: datetime,
    ) -> bool:
        """Return a still-owned inspecting job to queued without counting a failure.

        Used for cancellation / shutdown: never marks success.
        """
        assert_transition(MediaJobState.INSPECTING, MediaJobState.QUEUED)
        result = await self._session.execute(
            update(MediaJob)
            .where(
                MediaJob.id == job_id,
                MediaJob.public_state == MediaJobState.INSPECTING.value,
                MediaJob.lease_owner == owner,
                MediaJob.fence_token == fence,
                MediaJob.lease_expires_at > func.clock_timestamp(),
                MediaJob.expires_at > func.clock_timestamp(),
            )
            .values(
                public_state=MediaJobState.QUEUED.value,
                # Undo the claim attempt increment so cancel is not a failed try.
                attempt_count=func.greatest(MediaJob.attempt_count - 1, 0),
                available_at=now,
                lease_owner=None,
                lease_expires_at=None,
                updated_at=func.clock_timestamp(),
            )
        )
        return bool(result.rowcount)

    async def renew_lease(
        self,
        *,
        job_id: uuid.UUID,
        owner: str,
        fence: int,
        lease_seconds: float,
        now: datetime,
    ) -> bool:
        """Extend a still-live lease; a stale/expired owner can never revive it."""
        result = await self._session.execute(
            update(MediaJob)
            .where(
                MediaJob.id == job_id,
                MediaJob.public_state == MediaJobState.INSPECTING.value,
                MediaJob.lease_owner == owner,
                MediaJob.fence_token == fence,
                MediaJob.lease_expires_at > func.clock_timestamp(),
                MediaJob.expires_at > func.clock_timestamp(),
            )
            .values(
                lease_expires_at=func.clock_timestamp()
                + timedelta(seconds=lease_seconds),
                updated_at=func.clock_timestamp(),
            )
        )
        return bool(result.rowcount)

    async def reclaim_expired_leases(self, now: datetime) -> int:
        """Requeue inspecting jobs with expired leases; bump fence tokens."""
        # Load candidates with SKIP LOCKED so concurrent reclaimers do not fight.
        stmt = (
            select(MediaJob)
            .where(
                MediaJob.public_state == MediaJobState.INSPECTING.value,
                MediaJob.lease_expires_at.is_not(None),
                MediaJob.lease_expires_at <= func.clock_timestamp(),
                MediaJob.expires_at > func.clock_timestamp(),
            )
            .with_for_update(skip_locked=True)
        )
        jobs = list((await self._session.scalars(stmt)).all())
        for job in jobs:
            exhausted = int(job.attempt_count) >= int(job.max_attempts)
            target = MediaJobState.FAILED if exhausted else MediaJobState.QUEUED
            assert_transition(MediaJobState.INSPECTING, target)
            job.public_state = target.value
            job.lease_owner = None
            job.lease_expires_at = None
            job.fence_token = int(job.fence_token) + 1
            job.available_at = now
            job.updated_at = now
            if exhausted:
                job.public_error_code = JobErrorCode.MEDIA_INSPECTION_FAILED.value
                job.completed_at = now
        if jobs:
            await self._session.flush()
        return len(jobs)

    async def expire_due_jobs(self, now: datetime) -> int:
        """Mark past-TTL non-expired jobs as expired (including leased ones)."""
        stmt = (
            select(MediaJob)
            .where(
                MediaJob.expires_at <= func.clock_timestamp(),
                MediaJob.public_state.in_(
                    (
                        MediaJobState.QUEUED.value,
                        MediaJobState.INSPECTING.value,
                        MediaJobState.INSPECTED.value,
                        MediaJobState.FAILED.value,
                    )
                ),
            )
            .with_for_update(skip_locked=True)
        )
        jobs = list((await self._session.scalars(stmt)).all())
        for job in jobs:
            assert_transition(job.public_state, MediaJobState.EXPIRED)
            job.public_state = MediaJobState.EXPIRED.value
            job.lease_owner = None
            job.lease_expires_at = None
            job.updated_at = now
            job.fence_token = int(job.fence_token) + 1
            if job.completed_at is None:
                job.completed_at = now
        if jobs:
            await self._session.flush()
        return len(jobs)
