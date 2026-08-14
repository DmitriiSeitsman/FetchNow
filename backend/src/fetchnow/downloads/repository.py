"""Atomic PostgreSQL persistence for media-download jobs."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Select, and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fetchnow.downloads.errors import DownloadErrorCode, raise_download_error
from fetchnow.downloads.models import MediaDownloadJob
from fetchnow.downloads.progress import (
    DownloadProgressStage,
    assert_progress_transition,
)
from fetchnow.downloads.states import (
    MediaDownloadJobState,
    assert_transition,
    is_stored_cancelled,
)


class MediaDownloadJobRepository:
    """Session-bound repository for enqueue/claim/complete/expire operations."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def database_now(self) -> datetime:
        """Read the authoritative PostgreSQL wall clock for this transaction."""
        value = await self._session.scalar(select(func.clock_timestamp()))
        if not isinstance(value, datetime):
            raise_download_error(
                DownloadErrorCode.INTERNAL_ERROR,
                internal_reason="DATABASE_CLOCK_INVALID",
            )
        return value

    async def enqueue_or_get(
        self,
        *,
        media_job_id: uuid.UUID,
        format_option_id: str,
        provider_id: str,
        canonical_provider_url: str,
        media_id: str,
        hostname: str,
        path: str,
        scheme: str,
        port: int | None,
        selected_format_snapshot: dict[str, Any],
        now: datetime,
        ttl_seconds: int,
        max_attempts: int,
        parent_expires_at: datetime,
        suggested_filename: str | None = None,
        job_id: uuid.UUID | None = None,
    ) -> tuple[MediaDownloadJob, bool]:
        """Insert a queued download job or return the existing idempotent row.

        Concurrent creates rely on UNIQUE(media_job_id, format_option_id). A
        recovered row is returned only when every immutable field matches the
        requested creation snapshot; otherwise the caller fails closed.
        """
        if "?" in canonical_provider_url or "#" in canonical_provider_url:
            raise_download_error(
                DownloadErrorCode.INTERNAL_ERROR,
                internal_reason="CANONICAL_QUERY_OR_FRAGMENT",
            )
        if not format_option_id or len(format_option_id) > 64:
            raise_download_error(
                DownloadErrorCode.INTERNAL_ERROR,
                internal_reason="FORMAT_OPTION_ID_INVALID",
            )

        # A download job must never outlive the parent authorization that
        # gates every read of it.
        expires_at = min(now + timedelta(seconds=ttl_seconds), parent_expires_at)
        job = MediaDownloadJob(
            id=job_id or uuid.uuid4(),
            media_job_id=media_job_id,
            schema_version=1,
            public_state=MediaDownloadJobState.QUEUED.value,
            format_option_id=format_option_id,
            provider_id=provider_id,
            canonical_provider_url=canonical_provider_url,
            media_id=media_id,
            hostname=hostname,
            path=path,
            scheme=scheme,
            port=port,
            selected_format_snapshot=selected_format_snapshot,
            attempt_count=0,
            max_attempts=max_attempts,
            available_at=now,
            lease_owner=None,
            lease_expires_at=None,
            fence_token=0,
            artifact_id=None,
            artifact_bytes=None,
            artifact_content_type=None,
            artifact_container=None,
            public_error_code=None,
            progress_stage=DownloadProgressStage.QUEUED.value,
            progress_percent=None,
            cancel_requested_at=None,
            suggested_filename=suggested_filename,
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
                select(MediaDownloadJob).where(
                    MediaDownloadJob.media_job_id == media_job_id,
                    MediaDownloadJob.format_option_id == format_option_id,
                )
            )
            if existing is None:
                raise_download_error(
                    DownloadErrorCode.INTERNAL_ERROR,
                    internal_reason="IDEMPOTENCY_LOOKUP_MISS",
                )
            if not self._immutable_fields_match(
                existing,
                media_job_id=media_job_id,
                format_option_id=format_option_id,
                provider_id=provider_id,
                canonical_provider_url=canonical_provider_url,
                media_id=media_id,
                hostname=hostname,
                path=path,
                scheme=scheme,
                port=port,
                selected_format_snapshot=selected_format_snapshot,
                max_attempts=max_attempts,
            ):
                raise_download_error(
                    DownloadErrorCode.INTERNAL_ERROR,
                    internal_reason="IDEMPOTENT_ROW_MISMATCH",
                )
            return existing, False

    @staticmethod
    def _immutable_fields_match(
        existing: MediaDownloadJob,
        *,
        media_job_id: uuid.UUID,
        format_option_id: str,
        provider_id: str,
        canonical_provider_url: str,
        media_id: str,
        hostname: str,
        path: str,
        scheme: str,
        port: int | None,
        selected_format_snapshot: dict[str, Any],
        max_attempts: int,
    ) -> bool:
        """Compare every field that must be identical across idempotent creates."""
        stored_snapshot = existing.selected_format_snapshot
        if not isinstance(stored_snapshot, dict):
            return False
        return (
            existing.media_job_id == media_job_id
            and existing.format_option_id == format_option_id
            and existing.provider_id == provider_id
            and existing.canonical_provider_url == canonical_provider_url
            and existing.media_id == media_id
            and existing.hostname == hostname
            and existing.path == path
            and existing.scheme == scheme
            and existing.port == port
            and int(existing.schema_version) == 1
            and int(existing.max_attempts) == int(max_attempts)
            and dict(stored_snapshot) == dict(selected_format_snapshot)
        )

    async def get_by_id(self, job_id: uuid.UUID) -> MediaDownloadJob | None:
        """Load a download job by primary key."""
        result = await self._session.scalar(
            select(MediaDownloadJob).where(MediaDownloadJob.id == job_id)
        )
        return result if isinstance(result, MediaDownloadJob) else None

    def _claim_select(
        self, *, now: datetime, limit: int
    ) -> Select[tuple[MediaDownloadJob]]:
        return (
            select(MediaDownloadJob)
            .where(
                MediaDownloadJob.public_state == MediaDownloadJobState.QUEUED.value,
                MediaDownloadJob.available_at <= func.clock_timestamp(),
                MediaDownloadJob.expires_at > func.clock_timestamp(),
                MediaDownloadJob.attempt_count < MediaDownloadJob.max_attempts,
                MediaDownloadJob.cancel_requested_at.is_(None),
            )
            .order_by(MediaDownloadJob.created_at.asc(), MediaDownloadJob.id.asc())
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
    ) -> list[MediaDownloadJob]:
        """Atomically claim up to ``limit`` queued download jobs (SKIP LOCKED)."""
        if limit < 1:
            return []
        if not worker_id or len(worker_id) > 128:
            raise_download_error(
                DownloadErrorCode.INTERNAL_ERROR,
                internal_reason="INVALID_WORKER_ID",
            )
        claim_stmt = self._claim_select(now=now, limit=limit)
        rows = list((await self._session.scalars(claim_stmt)).all())
        claimed: list[MediaDownloadJob] = []
        lease_expires = now + timedelta(seconds=lease_seconds)
        for job in rows:
            assert_transition(job.public_state, MediaDownloadJobState.DOWNLOADING)
            job.public_state = MediaDownloadJobState.DOWNLOADING.value
            job.progress_stage = DownloadProgressStage.INSPECTING.value
            job.progress_percent = None
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
        del now  # Predicate uses PostgreSQL clock_timestamp().
        result = await self._session.execute(
            update(MediaDownloadJob)
            .where(
                MediaDownloadJob.id == job_id,
                MediaDownloadJob.public_state
                == MediaDownloadJobState.DOWNLOADING.value,
                MediaDownloadJob.lease_owner == owner,
                MediaDownloadJob.fence_token == fence,
                MediaDownloadJob.lease_expires_at > func.clock_timestamp(),
                MediaDownloadJob.expires_at > func.clock_timestamp(),
            )
            .values(
                lease_expires_at=func.clock_timestamp()
                + timedelta(seconds=lease_seconds),
                updated_at=func.clock_timestamp(),
            )
        )
        return bool(result.rowcount)

    async def complete_ready(
        self,
        *,
        job_id: uuid.UUID,
        owner: str,
        fence: int,
        artifact_id: uuid.UUID,
        artifact_bytes: int,
        artifact_content_type: str,
        artifact_container: str,
        now: datetime,
    ) -> bool:
        """Mark downloading job ready when lease owner + fence still match."""
        del now
        if artifact_bytes <= 0:
            raise_download_error(
                DownloadErrorCode.INTERNAL_ERROR,
                internal_reason="ARTIFACT_BYTES_INVALID",
            )
        assert_transition(
            MediaDownloadJobState.DOWNLOADING, MediaDownloadJobState.READY
        )
        result = await self._session.execute(
            update(MediaDownloadJob)
            .where(
                MediaDownloadJob.id == job_id,
                MediaDownloadJob.public_state
                == MediaDownloadJobState.DOWNLOADING.value,
                MediaDownloadJob.lease_owner == owner,
                MediaDownloadJob.fence_token == fence,
                MediaDownloadJob.lease_expires_at > func.clock_timestamp(),
                MediaDownloadJob.expires_at > func.clock_timestamp(),
                MediaDownloadJob.cancel_requested_at.is_(None),
            )
            .values(
                public_state=MediaDownloadJobState.READY.value,
                progress_stage=DownloadProgressStage.READY.value,
                progress_percent=None,
                artifact_id=artifact_id,
                artifact_bytes=artifact_bytes,
                artifact_content_type=artifact_content_type,
                artifact_container=artifact_container,
                public_error_code=None,
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
        del now
        assert_transition(
            MediaDownloadJobState.DOWNLOADING, MediaDownloadJobState.FAILED
        )
        result = await self._session.execute(
            update(MediaDownloadJob)
            .where(
                MediaDownloadJob.id == job_id,
                MediaDownloadJob.public_state
                == MediaDownloadJobState.DOWNLOADING.value,
                MediaDownloadJob.lease_owner == owner,
                MediaDownloadJob.fence_token == fence,
                MediaDownloadJob.lease_expires_at > func.clock_timestamp(),
                MediaDownloadJob.expires_at > func.clock_timestamp(),
                MediaDownloadJob.cancel_requested_at.is_(None),
            )
            .values(
                public_state=MediaDownloadJobState.FAILED.value,
                progress_stage=DownloadProgressStage.FAILED.value,
                progress_percent=None,
                public_error_code=public_error_code,
                artifact_id=None,
                artifact_bytes=None,
                artifact_content_type=None,
                artifact_container=None,
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
        """Reschedule to queued with backoff, or terminal-fail when exhausted."""
        job = await self.get_by_id(job_id)
        if job is None:
            return False
        if (
            job.public_state != MediaDownloadJobState.DOWNLOADING.value
            or job.lease_owner != owner
            or int(job.fence_token) != int(fence)
        ):
            return False

        if job.cancel_requested_at is not None:
            return await self.complete_cancelled(
                job_id=job_id,
                owner=owner,
                fence=fence,
                now=now,
            )

        if int(job.attempt_count) >= int(job.max_attempts):
            return await self.fail_permanent(
                job_id=job_id,
                owner=owner,
                fence=fence,
                public_error_code=public_error_code,
                now=now,
            )

        assert_transition(
            MediaDownloadJobState.DOWNLOADING, MediaDownloadJobState.QUEUED
        )
        available_at = now + timedelta(seconds=max(0.0, backoff_seconds))
        result = await self._session.execute(
            update(MediaDownloadJob)
            .where(
                MediaDownloadJob.id == job_id,
                MediaDownloadJob.public_state
                == MediaDownloadJobState.DOWNLOADING.value,
                MediaDownloadJob.lease_owner == owner,
                MediaDownloadJob.fence_token == fence,
                MediaDownloadJob.lease_expires_at > func.clock_timestamp(),
                MediaDownloadJob.expires_at > func.clock_timestamp(),
                MediaDownloadJob.cancel_requested_at.is_(None),
            )
            .values(
                public_state=MediaDownloadJobState.QUEUED.value,
                progress_stage=DownloadProgressStage.RETRYING.value,
                progress_percent=None,
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
        """Return a still-owned downloading job to queued without counting a failure."""
        assert_transition(
            MediaDownloadJobState.DOWNLOADING, MediaDownloadJobState.QUEUED
        )
        result = await self._session.execute(
            update(MediaDownloadJob)
            .where(
                MediaDownloadJob.id == job_id,
                MediaDownloadJob.public_state
                == MediaDownloadJobState.DOWNLOADING.value,
                MediaDownloadJob.lease_owner == owner,
                MediaDownloadJob.fence_token == fence,
                MediaDownloadJob.lease_expires_at > func.clock_timestamp(),
                MediaDownloadJob.expires_at > func.clock_timestamp(),
                MediaDownloadJob.cancel_requested_at.is_(None),
            )
            .values(
                public_state=MediaDownloadJobState.QUEUED.value,
                progress_stage=DownloadProgressStage.QUEUED.value,
                progress_percent=None,
                attempt_count=func.greatest(MediaDownloadJob.attempt_count - 1, 0),
                available_at=now,
                lease_owner=None,
                lease_expires_at=None,
                updated_at=func.clock_timestamp(),
            )
        )
        return bool(result.rowcount)

    async def list_active_fences(self) -> frozenset[tuple[uuid.UUID, int]]:
        """Return (job_id, fence_token) for jobs currently downloading."""
        rows = list(
            (
                await self._session.scalars(
                    select(MediaDownloadJob).where(
                        MediaDownloadJob.public_state
                        == MediaDownloadJobState.DOWNLOADING.value
                    )
                )
            ).all()
        )
        return frozenset((job.id, int(job.fence_token)) for job in rows)

    async def list_referenced_artifact_ids(self) -> frozenset[uuid.UUID]:
        """Return artifact ids still pointed to by durable download job rows."""
        rows = list(
            (
                await self._session.scalars(
                    select(MediaDownloadJob.artifact_id).where(
                        MediaDownloadJob.artifact_id.is_not(None)
                    )
                )
            ).all()
        )
        return frozenset(artifact_id for artifact_id in rows if artifact_id is not None)

    async def reclaim_expired_leases(self, now: datetime) -> int:
        """Requeue downloading jobs with expired leases; bump fence tokens."""
        stmt = (
            select(MediaDownloadJob)
            .where(
                MediaDownloadJob.public_state
                == MediaDownloadJobState.DOWNLOADING.value,
                MediaDownloadJob.lease_expires_at.is_not(None),
                MediaDownloadJob.lease_expires_at <= func.clock_timestamp(),
                MediaDownloadJob.expires_at > func.clock_timestamp(),
            )
            .with_for_update(skip_locked=True)
        )
        jobs = list((await self._session.scalars(stmt)).all())
        for job in jobs:
            if job.cancel_requested_at is not None:
                assert_transition(
                    MediaDownloadJobState.DOWNLOADING,
                    MediaDownloadJobState.EXPIRED,
                )
                self._apply_stored_cancel(job, now)
                job.fence_token = int(job.fence_token) + 1
                continue
            exhausted = int(job.attempt_count) >= int(job.max_attempts)
            target = (
                MediaDownloadJobState.FAILED
                if exhausted
                else MediaDownloadJobState.QUEUED
            )
            assert_transition(MediaDownloadJobState.DOWNLOADING, target)
            job.public_state = target.value
            job.progress_stage = (
                DownloadProgressStage.FAILED.value
                if exhausted
                else DownloadProgressStage.RETRYING.value
            )
            job.progress_percent = None
            job.lease_owner = None
            job.lease_expires_at = None
            job.fence_token = int(job.fence_token) + 1
            job.available_at = now
            job.updated_at = now
            if exhausted:
                job.public_error_code = DownloadErrorCode.DOWNLOAD_TOOL_FAILED.value
                job.completed_at = now
                job.artifact_id = None
                job.artifact_bytes = None
                job.artifact_content_type = None
                job.artifact_container = None
        if jobs:
            await self._session.flush()
        return len(jobs)

    async def expire_due_jobs(self, now: datetime) -> list[uuid.UUID]:
        """Mark past-TTL jobs expired; return artifact ids that need filesystem delete.

        Ready jobs clear artifact pointer fields so the result CHECK holds.
        """
        stmt = (
            select(MediaDownloadJob)
            .where(
                MediaDownloadJob.expires_at <= func.clock_timestamp(),
                or_(
                    MediaDownloadJob.public_state.in_(
                        (
                            MediaDownloadJobState.QUEUED.value,
                            MediaDownloadJobState.DOWNLOADING.value,
                            MediaDownloadJobState.READY.value,
                            MediaDownloadJobState.FAILED.value,
                        )
                    ),
                    and_(
                        MediaDownloadJob.public_state
                        == MediaDownloadJobState.EXPIRED.value,
                        MediaDownloadJob.progress_stage
                        == DownloadProgressStage.CANCELLED.value,
                        MediaDownloadJob.cancel_requested_at.is_not(None),
                    ),
                ),
            )
            .with_for_update(skip_locked=True)
        )
        jobs = list((await self._session.scalars(stmt)).all())
        artifact_ids: list[uuid.UUID] = []
        for job in jobs:
            if is_stored_cancelled(
                job.public_state, job.progress_stage, job.cancel_requested_at
            ):
                assert_transition(
                    MediaDownloadJobState.CANCELLED, MediaDownloadJobState.EXPIRED
                )
            else:
                assert_transition(job.public_state, MediaDownloadJobState.EXPIRED)
            if job.artifact_id is not None:
                artifact_ids.append(job.artifact_id)
            job.public_state = MediaDownloadJobState.EXPIRED.value
            job.progress_stage = DownloadProgressStage.EXPIRED.value
            job.progress_percent = None
            job.lease_owner = None
            job.lease_expires_at = None
            job.cancel_requested_at = None
            job.artifact_id = None
            job.artifact_bytes = None
            job.artifact_content_type = None
            job.artifact_container = None
            job.public_error_code = None
            job.updated_at = now
            job.fence_token = int(job.fence_token) + 1
            if job.completed_at is None:
                job.completed_at = now
        if jobs:
            await self._session.flush()
        return artifact_ids

    async def request_cancel(
        self, *, job_id: uuid.UUID, now: datetime
    ) -> MediaDownloadJob | None:
        """Idempotent user cancel. Ready/failed/expired are left unchanged."""
        stmt = (
            select(MediaDownloadJob)
            .where(MediaDownloadJob.id == job_id)
            .with_for_update()
        )
        job = await self._session.scalar(stmt)
        if job is None:
            return None
        if is_stored_cancelled(
            job.public_state, job.progress_stage, job.cancel_requested_at
        ):
            return job
        state = job.public_state
        if state in {
            MediaDownloadJobState.READY.value,
            MediaDownloadJobState.FAILED.value,
            MediaDownloadJobState.EXPIRED.value,
        }:
            return job
        if state == MediaDownloadJobState.QUEUED.value:
            assert_transition(
                MediaDownloadJobState.QUEUED, MediaDownloadJobState.EXPIRED
            )
            job.cancel_requested_at = now
            self._apply_stored_cancel(job, now)
            await self._session.flush()
            return job
        if state == MediaDownloadJobState.DOWNLOADING.value:
            if job.cancel_requested_at is None:
                job.cancel_requested_at = now
                job.updated_at = now
                await self._session.flush()
            return job
        raise_download_error(
            DownloadErrorCode.INTERNAL_ERROR,
            internal_reason="UNKNOWN_DOWNLOAD_STATE",
        )

    async def complete_cancelled(
        self,
        *,
        job_id: uuid.UUID,
        owner: str,
        fence: int,
        now: datetime,
    ) -> bool:
        """Terminal-cancel only a fenced downloading job with a user request."""
        del now
        assert_transition(
            MediaDownloadJobState.DOWNLOADING, MediaDownloadJobState.EXPIRED
        )
        result = await self._session.execute(
            update(MediaDownloadJob)
            .where(
                MediaDownloadJob.id == job_id,
                MediaDownloadJob.public_state
                == MediaDownloadJobState.DOWNLOADING.value,
                MediaDownloadJob.lease_owner == owner,
                MediaDownloadJob.fence_token == fence,
                MediaDownloadJob.cancel_requested_at.is_not(None),
            )
            .values(
                public_state=MediaDownloadJobState.EXPIRED.value,
                progress_stage=DownloadProgressStage.CANCELLED.value,
                progress_percent=None,
                public_error_code=None,
                artifact_id=None,
                artifact_bytes=None,
                artifact_content_type=None,
                artifact_container=None,
                lease_owner=None,
                lease_expires_at=None,
                completed_at=func.clock_timestamp(),
                updated_at=func.clock_timestamp(),
            )
        )
        return bool(result.rowcount)

    async def set_progress_stage(
        self,
        *,
        job_id: uuid.UUID,
        owner: str,
        fence: int,
        stage: DownloadProgressStage | str,
        now: datetime,
    ) -> bool:
        """Fenced active-stage update. Terminal stages use complete/fail/cancel."""
        target = (
            stage
            if isinstance(stage, DownloadProgressStage)
            else DownloadProgressStage(str(stage))
        )
        if target in {
            DownloadProgressStage.READY,
            DownloadProgressStage.FAILED,
            DownloadProgressStage.CANCELLED,
            DownloadProgressStage.EXPIRED,
        }:
            return False
        stmt = (
            select(MediaDownloadJob)
            .where(
                MediaDownloadJob.id == job_id,
                MediaDownloadJob.public_state
                == MediaDownloadJobState.DOWNLOADING.value,
                MediaDownloadJob.lease_owner == owner,
                MediaDownloadJob.fence_token == fence,
                MediaDownloadJob.cancel_requested_at.is_(None),
            )
            .with_for_update()
        )
        job = await self._session.scalar(stmt)
        if job is None:
            return False
        assert_progress_transition(job.progress_stage, target)
        if job.progress_stage != target.value:
            job.progress_percent = None
        job.progress_stage = target.value
        job.updated_at = now
        await self._session.flush()
        return True

    async def set_progress_percent(
        self,
        *,
        job_id: uuid.UUID,
        owner: str,
        fence: int,
        percent: int,
        now: datetime,
    ) -> bool:
        """Fenced monotonic percent update during an active download subprocess."""
        if type(percent) is bool or not isinstance(percent, int):
            return False
        if percent < 0 or percent > 99:
            return False
        stmt = (
            select(MediaDownloadJob)
            .where(
                MediaDownloadJob.id == job_id,
                MediaDownloadJob.public_state
                == MediaDownloadJobState.DOWNLOADING.value,
                MediaDownloadJob.lease_owner == owner,
                MediaDownloadJob.fence_token == fence,
                MediaDownloadJob.cancel_requested_at.is_(None),
                MediaDownloadJob.lease_expires_at > func.clock_timestamp(),
                MediaDownloadJob.expires_at > func.clock_timestamp(),
                MediaDownloadJob.progress_stage.in_(
                    (
                        DownloadProgressStage.DOWNLOADING_VIDEO.value,
                        DownloadProgressStage.DOWNLOADING_AUDIO.value,
                    )
                ),
            )
            .with_for_update()
        )
        job = await self._session.scalar(stmt)
        if job is None:
            return False
        stored = job.progress_percent
        if stored is not None and percent < int(stored):
            return False
        if stored is not None and percent == int(stored):
            return True
        job.progress_percent = percent
        job.updated_at = now
        await self._session.flush()
        return True

    async def lease_status(
        self,
        *,
        job_id: uuid.UUID,
        owner: str,
        fence: int,
        lease_seconds: float,
        now: datetime,
    ) -> str:
        """Return ``owned``, ``lost``, or ``cancelled`` for the current fence."""
        del now
        stmt = (
            select(MediaDownloadJob)
            .where(
                MediaDownloadJob.id == job_id,
                MediaDownloadJob.public_state
                == MediaDownloadJobState.DOWNLOADING.value,
                MediaDownloadJob.lease_owner == owner,
                MediaDownloadJob.fence_token == fence,
                MediaDownloadJob.lease_expires_at > func.clock_timestamp(),
                MediaDownloadJob.expires_at > func.clock_timestamp(),
            )
            .with_for_update()
        )
        job = await self._session.scalar(stmt)
        if job is None:
            return "lost"
        if job.cancel_requested_at is not None:
            return "cancelled"
        renewed = await self.renew_lease(
            job_id=job_id,
            owner=owner,
            fence=fence,
            lease_seconds=lease_seconds,
            now=datetime.now(),
        )
        return "owned" if renewed else "lost"

    @staticmethod
    def _apply_stored_cancel(job: MediaDownloadJob, now: datetime) -> None:
        """Encode user cancel as PR9-readable expired + PR10 cancel markers."""
        job.public_state = MediaDownloadJobState.EXPIRED.value
        job.progress_stage = DownloadProgressStage.CANCELLED.value
        job.progress_percent = None
        job.lease_owner = None
        job.lease_expires_at = None
        job.updated_at = now
        if job.completed_at is None:
            job.completed_at = now
        job.public_error_code = None
        job.artifact_id = None
        job.artifact_bytes = None
        job.artifact_content_type = None
        job.artifact_container = None
