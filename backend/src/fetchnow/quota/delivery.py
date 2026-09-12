"""Bounded PostgreSQL accounting for server-observed Free delivery bytes."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Never, Protocol

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from fetchnow.downloads.errors import DownloadErrorCode, raise_download_error
from fetchnow.downloads.models import MediaDownloadJob
from fetchnow.downloads.states import MediaDownloadJobState
from fetchnow.quota.errors import QuotaInvariantError
from fetchnow.quota.models import (
    FreeDownloadDeliveryRange,
    FreeDownloadQuotaEntry,
)
from fetchnow.quota.repository import QuotaRepository

logger = logging.getLogger("fetchnow.quota.delivery")

# A lease longer than the gateway's 300-second inactivity window lets an active
# response renew safely without retaining abandoned artifacts indefinitely.
DELIVERY_ATTEMPT_LEASE_SECONDS = 600
DELIVERY_CHECKPOINT_BYTES = 8 * 1024 * 1024
DELIVERY_CHECKPOINT_SECONDS = 30.0
DELIVERY_FINALIZE_TIMEOUT_SECONDS = 15.0
MAX_ACTIVE_ATTEMPTS = 16
MAX_CLOSED_FRAGMENTS = 64


class DeliveryAuthorizationLike(Protocol):
    @property
    def job(self) -> MediaDownloadJob: ...

    @property
    def policy(self) -> object | None: ...


Revalidate = Callable[[AsyncSession], Awaitable[DeliveryAuthorizationLike]]


@dataclass(frozen=True, slots=True)
class DeliveryAccountingAttempt:
    id: uuid.UUID
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class DeliveryAccountingResult:
    consumed: bool = False
    duplicate: bool = False


def normalize_intervals(
    intervals: Sequence[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Return sorted, adjacent/overlapping half-open interval union."""
    ordered = sorted((start, end) for start, end in intervals if end > start)
    merged: list[tuple[int, int]] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue
        previous_start, previous_end = merged[-1]
        merged[-1] = (previous_start, max(previous_end, end))
    return merged


class DeliveryQuotaAccounting:
    """Short-transaction delivery ledger shared by both HTTP transports."""

    __slots__ = ("_compatibility_mode",)

    def __init__(self, *, compatibility_mode: bool) -> None:
        self._compatibility_mode = compatibility_mode

    @property
    def compatibility_mode(self) -> bool:
        return self._compatibility_mode

    async def begin(
        self,
        *,
        authorization: DeliveryAuthorizationLike,
        start: int,
        end: int,
        session: AsyncSession,
        revalidate: Revalidate,
    ) -> DeliveryAccountingAttempt | None:
        """Create a required Free attempt before any response bytes are sent."""
        job_snapshot = authorization.job
        policy = authorization.policy
        if getattr(policy, "tier", "free") == "premium":
            return None
        if start < 0 or end <= start or job_snapshot.artifact_bytes is None:
            raise QuotaInvariantError("invalid delivery accounting interval")

        quota = QuotaRepository(session)
        client_id = await quota.identity_id_for_download(job_snapshot.id)
        # Legacy/quota-disabled Free jobs have no reservation and remain deliverable.
        if client_id is None:
            return None
        client = await quota.lock_client(client_id)
        if client is None:
            raise QuotaInvariantError(
                "delivery quota entry references missing identity"
            )
        job = await session.scalar(
            select(MediaDownloadJob)
            .where(MediaDownloadJob.id == job_snapshot.id)
            .with_for_update()
        )
        if job is None:
            self._fail_closed("ACCOUNTING_JOB_MISSING")
        entry = await quota.lock_entry_for_download(job_snapshot.id)
        if entry is None:
            raise QuotaInvariantError("delivery quota entry disappeared")

        # Re-run transport authorization inside the begin transaction after the
        # canonical parent locks have been acquired.
        current = await revalidate(session)
        if current.job.id != job.id:
            self._fail_closed("ACCOUNTING_REAUTH_MISMATCH")
        now = await quota.database_now()
        self._validate_job_binding(
            job,
            artifact_id=job_snapshot.artifact_id,
            fence_token=int(job_snapshot.fence_token),
            artifact_bytes=int(job_snapshot.artifact_bytes),
            require_unexpired=True,
            now=now,
        )
        assert job.artifact_id is not None
        assert job.artifact_bytes is not None
        if entry.state == "consumed":
            logger.info("delivery_quota_consumed outcome=duplicate")
            return None
        if entry.state != "reserved":
            self._fail_closed("ACCOUNTING_QUOTA_TERMINAL")

        rows = await self._lock_ranges(session, entry.id)
        await self._close_stale(session, rows=rows, now=now)
        rows = await self._lock_ranges(session, entry.id)
        self._assert_generation(rows, job)
        await self._compact_closed(session, entry.id, rows, now=now)
        rows = await self._lock_ranges(session, entry.id)
        active = [row for row in rows if row.state == "active"]
        closed = self._closed_intervals(rows)
        if len(active) >= MAX_ACTIVE_ATTEMPTS:
            self._bound_rejected("ACCOUNTING_ACTIVE_LIMIT")
        # Reserve the first possible byte of every active response as well as
        # the new response. Extending any prefix can only merge components,
        # never add another one, so this keeps future concurrent finalization
        # within the durable fragment bound.
        active_first_bytes = [
            (
                int(row.request_start),
                min(int(row.request_start) + 1, int(row.request_end_exclusive)),
            )
            for row in active
        ]
        first_byte = (start, min(start + 1, end))
        if (
            len(normalize_intervals([*closed, *active_first_bytes, first_byte]))
            > MAX_CLOSED_FRAGMENTS
        ):
            self._bound_rejected("ACCOUNTING_FRAGMENT_LIMIT")

        lease_expires = now + timedelta(seconds=DELIVERY_ATTEMPT_LEASE_SECONDS)
        attempt = FreeDownloadDeliveryRange(
            id=uuid.uuid4(),
            quota_entry_id=entry.id,
            artifact_id=job.artifact_id,
            fence_token=int(job.fence_token),
            artifact_bytes=int(job.artifact_bytes),
            request_start=start,
            request_end_exclusive=end,
            served_end_exclusive=start,
            state="active",
            started_at=now,
            lease_expires_at=lease_expires,
            closed_at=None,
        )
        session.add(attempt)
        entry.reservation_expires_at = max(entry.reservation_expires_at, lease_expires)
        await session.flush()
        logger.info(
            "delivery_accounting_attempt_started outcome=ok range=%s",
            "full" if start == 0 and end == int(job.artifact_bytes) else "partial",
        )
        return DeliveryAccountingAttempt(attempt.id, start, end)

    async def checkpoint(
        self,
        *,
        attempt_id: uuid.UUID,
        observed_end: int,
        session: AsyncSession,
    ) -> DeliveryAccountingResult:
        """Durably advance an active response prefix and renew its lease."""
        locked = await self._lock_attempt_context(
            session, attempt_id=attempt_id, allow_closed=False
        )
        if locked is None:
            raise QuotaInvariantError("active delivery attempt disappeared")
        quota, job, entry, attempt, now = locked
        if observed_end < attempt.served_end_exclusive:
            observed_end = int(attempt.served_end_exclusive)
        if observed_end > attempt.request_end_exclusive:
            raise QuotaInvariantError("delivery checkpoint exceeds requested range")
        attempt.served_end_exclusive = observed_end
        lease_expires = now + timedelta(seconds=DELIVERY_ATTEMPT_LEASE_SECONDS)
        attempt.lease_expires_at = lease_expires
        if entry.state == "reserved":
            entry.reservation_expires_at = max(
                entry.reservation_expires_at, lease_expires
            )
        result = await self._consume_if_complete(
            session, quota=quota, job=job, entry=entry, now=now
        )
        await session.flush()
        logger.info("delivery_accounting_checkpoint outcome=ok")
        return result

    async def finalize(
        self,
        *,
        attempt_id: uuid.UUID,
        observed_end: int,
        session: AsyncSession,
    ) -> DeliveryAccountingResult:
        """Close an attempt at its exact durable prefix and compact coverage."""
        locked = await self._lock_attempt_context(
            session, attempt_id=attempt_id, allow_closed=True
        )
        if locked is None:
            return DeliveryAccountingResult(duplicate=True)
        quota, job, entry, attempt, now = locked
        if attempt.state == "closed":
            return DeliveryAccountingResult(duplicate=True)
        if observed_end < attempt.served_end_exclusive:
            observed_end = int(attempt.served_end_exclusive)
        if observed_end > attempt.request_end_exclusive:
            raise QuotaInvariantError("delivery finalization exceeds requested range")
        attempt.served_end_exclusive = observed_end
        if observed_end == attempt.request_start:
            await session.delete(attempt)
        else:
            attempt.request_end_exclusive = observed_end
            attempt.state = "closed"
            attempt.lease_expires_at = None
            attempt.closed_at = now
        await session.flush()
        rows = await self._lock_ranges(session, entry.id)
        await self._compact_closed(session, entry.id, rows, now=now)
        result = await self._consume_if_complete(
            session, quota=quota, job=job, entry=entry, now=now
        )
        if entry.state == "reserved":
            live_until = max(
                (
                    row.lease_expires_at
                    for row in await self._lock_ranges(session, entry.id)
                    if row.state == "active" and row.lease_expires_at is not None
                ),
                default=job.expires_at,
            )
            entry.reservation_expires_at = max(job.expires_at, live_until)
        await session.flush()
        return result

    async def has_live_attempt_locked(
        self,
        *,
        session: AsyncSession,
        entry: FreeDownloadQuotaEntry,
        now: datetime,
    ) -> bool:
        """Return true after locking ledger rows when a delivery lease is live."""
        rows = await self._lock_ranges(session, entry.id)
        return any(
            row.state == "active"
            and row.lease_expires_at is not None
            and row.lease_expires_at > now
            for row in rows
        )

    async def reconcile_locked(
        self,
        *,
        session: AsyncSession,
        quota: QuotaRepository,
        job: MediaDownloadJob,
        entry: FreeDownloadQuotaEntry,
        now: datetime,
    ) -> DeliveryAccountingResult:
        """Close stale attempts, compact evidence and repair full coverage."""
        rows = await self._lock_ranges(session, entry.id)
        self._assert_generation(rows, job)
        stale = [
            row
            for row in rows
            if row.state == "active"
            and row.lease_expires_at is not None
            and row.lease_expires_at <= now
        ]
        await self._close_stale(session, rows=stale, now=now)
        rows = await self._lock_ranges(session, entry.id)
        await self._compact_closed(session, entry.id, rows, now=now)
        result = await self._consume_if_complete(
            session, quota=quota, job=job, entry=entry, now=now
        )
        await session.flush()
        return result

    async def _lock_attempt_context(
        self,
        session: AsyncSession,
        *,
        attempt_id: uuid.UUID,
        allow_closed: bool,
    ) -> (
        tuple[
            QuotaRepository,
            MediaDownloadJob,
            FreeDownloadQuotaEntry,
            FreeDownloadDeliveryRange,
            datetime,
        ]
        | None
    ):
        discovery = await session.execute(
            select(
                FreeDownloadDeliveryRange.quota_entry_id,
                FreeDownloadQuotaEntry.anonymous_client_id,
                FreeDownloadQuotaEntry.download_job_id,
            )
            .join(
                FreeDownloadQuotaEntry,
                FreeDownloadQuotaEntry.id == FreeDownloadDeliveryRange.quota_entry_id,
            )
            .where(FreeDownloadDeliveryRange.id == attempt_id)
        )
        found = discovery.one_or_none()
        if found is None:
            return None
        entry_id, client_id, job_id = found
        quota = QuotaRepository(session)
        client = await quota.lock_client(client_id)
        if client is None:
            raise QuotaInvariantError("delivery attempt identity disappeared")
        job = await session.scalar(
            select(MediaDownloadJob)
            .where(MediaDownloadJob.id == job_id)
            .with_for_update()
        )
        if job is None:
            raise QuotaInvariantError("delivery attempt job disappeared")
        entry = await session.scalar(
            select(FreeDownloadQuotaEntry)
            .where(FreeDownloadQuotaEntry.id == entry_id)
            .with_for_update()
        )
        if entry is None:
            raise QuotaInvariantError("delivery attempt quota entry disappeared")
        attempt = await session.scalar(
            select(FreeDownloadDeliveryRange)
            .where(FreeDownloadDeliveryRange.id == attempt_id)
            .with_for_update()
        )
        if attempt is None:
            return None
        if attempt.state == "closed" and not allow_closed:
            raise QuotaInvariantError("delivery checkpoint targets closed attempt")
        self._validate_job_binding(
            job,
            artifact_id=attempt.artifact_id,
            fence_token=int(attempt.fence_token),
            artifact_bytes=int(attempt.artifact_bytes),
            require_unexpired=False,
            now=None,
        )
        now = await quota.database_now()
        return quota, job, entry, attempt, now

    async def _lock_ranges(
        self, session: AsyncSession, entry_id: uuid.UUID
    ) -> list[FreeDownloadDeliveryRange]:
        return list(
            (
                await session.scalars(
                    select(FreeDownloadDeliveryRange)
                    .where(FreeDownloadDeliveryRange.quota_entry_id == entry_id)
                    .order_by(
                        FreeDownloadDeliveryRange.request_start,
                        FreeDownloadDeliveryRange.id,
                    )
                    .with_for_update()
                )
            ).all()
        )

    @staticmethod
    def _closed_intervals(
        rows: Sequence[FreeDownloadDeliveryRange],
    ) -> list[tuple[int, int]]:
        return normalize_intervals(
            [
                (int(row.request_start), int(row.served_end_exclusive))
                for row in rows
                if row.served_end_exclusive > row.request_start
            ]
        )

    async def _close_stale(
        self,
        session: AsyncSession,
        *,
        rows: Sequence[FreeDownloadDeliveryRange],
        now: datetime,
    ) -> None:
        for row in rows:
            if row.state != "active" or (
                row.lease_expires_at is not None and row.lease_expires_at > now
            ):
                continue
            if row.served_end_exclusive == row.request_start:
                await session.delete(row)
            else:
                row.request_end_exclusive = row.served_end_exclusive
                row.state = "closed"
                row.lease_expires_at = None
                row.closed_at = now
            logger.info("delivery_attempt_lease_expired outcome=reconciled")
        await session.flush()

    async def _compact_closed(
        self,
        session: AsyncSession,
        entry_id: uuid.UUID,
        rows: Sequence[FreeDownloadDeliveryRange],
        *,
        now: datetime,
    ) -> None:
        closed = [row for row in rows if row.state == "closed"]
        union = normalize_intervals(
            [
                (int(row.request_start), int(row.served_end_exclusive))
                for row in closed
                if row.served_end_exclusive > row.request_start
            ]
        )
        if len(union) > MAX_CLOSED_FRAGMENTS:
            raise QuotaInvariantError("delivery coverage exceeds fragment bound")
        if len(closed) == len(union) and all(
            int(row.request_start) == start
            and int(row.served_end_exclusive) == end
            and int(row.request_end_exclusive) == end
            for row, (start, end) in zip(closed, union, strict=True)
        ):
            return
        template = closed[0] if closed else None
        if template is None:
            return
        artifact_id = template.artifact_id
        fence = int(template.fence_token)
        size = int(template.artifact_bytes)
        await session.execute(
            delete(FreeDownloadDeliveryRange).where(
                FreeDownloadDeliveryRange.quota_entry_id == entry_id,
                FreeDownloadDeliveryRange.state == "closed",
            )
        )
        for start, end in union:
            session.add(
                FreeDownloadDeliveryRange(
                    id=uuid.uuid4(),
                    quota_entry_id=entry_id,
                    artifact_id=artifact_id,
                    fence_token=fence,
                    artifact_bytes=size,
                    request_start=start,
                    request_end_exclusive=end,
                    served_end_exclusive=end,
                    state="closed",
                    started_at=template.started_at,
                    lease_expires_at=None,
                    closed_at=now,
                )
            )
        await session.flush()

    async def _consume_if_complete(
        self,
        session: AsyncSession,
        *,
        quota: QuotaRepository,
        job: MediaDownloadJob,
        entry: FreeDownloadQuotaEntry,
        now: datetime,
    ) -> DeliveryAccountingResult:
        if entry.state == "consumed":
            logger.info("delivery_quota_consumed outcome=duplicate_suppressed")
            return DeliveryAccountingResult(duplicate=True)
        if entry.state != "reserved":
            self._fail_closed("ACCOUNTING_QUOTA_NOT_RESERVED")
        rows = await self._lock_ranges(session, entry.id)
        self._assert_generation(rows, job)
        coverage = self._closed_intervals(rows)
        size = int(job.artifact_bytes or 0)
        if coverage != [(0, size)]:
            return DeliveryAccountingResult()
        quota.consume_locked(entry, now=now)
        logger.info("delivery_quota_consumed outcome=ok")
        return DeliveryAccountingResult(consumed=True)

    @staticmethod
    def _assert_generation(
        rows: Sequence[FreeDownloadDeliveryRange], job: MediaDownloadJob
    ) -> None:
        for row in rows:
            if (
                row.artifact_id != job.artifact_id
                or int(row.fence_token) != int(job.fence_token)
                or int(row.artifact_bytes) != int(job.artifact_bytes or 0)
            ):
                logger.error(
                    "delivery_accounting_invariant outcome=generation_mismatch"
                )
                raise QuotaInvariantError("delivery evidence generation mismatch")

    @staticmethod
    def _validate_job_binding(
        job: MediaDownloadJob,
        *,
        artifact_id: uuid.UUID | None,
        fence_token: int,
        artifact_bytes: int,
        require_unexpired: bool,
        now: datetime | None,
    ) -> None:
        if (
            job.public_state != MediaDownloadJobState.READY.value
            or job.artifact_id is None
            or job.artifact_id != artifact_id
            or int(job.fence_token) != fence_token
            or int(job.artifact_bytes or 0) != artifact_bytes
        ):
            DeliveryQuotaAccounting._fail_closed("ACCOUNTING_ARTIFACT_MISMATCH")
        if require_unexpired and now is not None and job.expires_at <= now:
            DeliveryQuotaAccounting._fail_closed("ACCOUNTING_JOB_EXPIRED")

    @staticmethod
    def _bound_rejected(reason: str) -> None:
        logger.info("delivery_accounting_bound outcome=rejected reason=%s", reason)
        raise_download_error(
            DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
            internal_reason=reason,
        )

    @staticmethod
    def _fail_closed(reason: str) -> Never:
        logger.error("delivery_accounting_invariant outcome=failed reason=%s", reason)
        raise_download_error(
            DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
            internal_reason=reason,
        )
