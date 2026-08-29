"""Bounded repair and retention for Free quota state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fetchnow.core.config import Settings
from fetchnow.downloads.models import MediaDownloadJob
from fetchnow.downloads.progress import DownloadProgressStage
from fetchnow.downloads.states import MediaDownloadJobState
from fetchnow.quota.errors import QuotaInvariantError
from fetchnow.quota.models import FreeDownloadQuotaEntry
from fetchnow.quota.repository import QuotaRepository


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    consumed: int = 0
    released: int = 0
    expired: int = 0
    entries_deleted: int = 0
    clients_deleted: int = 0


class QuotaReconciler:
    """Repair reserved entries from authoritative job state, then prune."""

    __slots__ = ("_settings",)

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def run(self, *, session: AsyncSession, limit: int = 64) -> ReconcileResult:
        quota = QuotaRepository(session)
        now = await quota.database_now()
        candidates = list(
            (
                await session.execute(
                    select(
                        FreeDownloadQuotaEntry.anonymous_client_id,
                        FreeDownloadQuotaEntry.download_job_id,
                    )
                    .where(FreeDownloadQuotaEntry.state == "reserved")
                    .order_by(FreeDownloadQuotaEntry.reserved_at)
                    .limit(limit)
                )
            ).all()
        )
        consumed = released = expired = 0
        for client_id, job_id in candidates:
            client = await quota.lock_client(client_id)
            if client is None:
                raise QuotaInvariantError("reserved entry references missing identity")
            job = await session.scalar(
                select(MediaDownloadJob)
                .where(MediaDownloadJob.id == job_id)
                .with_for_update()
            )
            entry = await quota.lock_entry_for_download(job_id)
            if entry is None or entry.state != "reserved":
                continue
            if job is None:
                # FK CASCADE normally makes this impossible.
                raise QuotaInvariantError("reserved entry references missing job")
            if job.public_state == MediaDownloadJobState.READY.value:
                if job.completed_at is None:
                    raise QuotaInvariantError("ready job lacks completion timestamp")
                quota.consume_locked(entry, now=job.completed_at)
                consumed += 1
            elif job.public_state == MediaDownloadJobState.FAILED.value:
                quota.release_locked(entry, now=now)
                released += 1
            elif job.public_state == MediaDownloadJobState.EXPIRED.value:
                quota.release_locked(entry, now=now, expired=True)
                expired += 1
            elif (
                entry.reservation_expires_at <= now
                and job.expires_at <= now
                and job.public_state
                in {
                    MediaDownloadJobState.QUEUED.value,
                    MediaDownloadJobState.DOWNLOADING.value,
                }
            ):
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
                job.completed_at = job.completed_at or now
                job.fence_token = int(job.fence_token) + 1
                quota.release_locked(entry, now=now, expired=True)
                expired += 1
        await session.flush()

        cutoff = now - timedelta(
            seconds=self._settings.free_download_quota_retention_seconds
        )
        entries_deleted = await quota.delete_terminal_entries_before(
            cutoff=cutoff, limit=limit
        )
        clients_deleted = await quota.delete_expired_clients_without_entries(
            now=now, limit=limit
        )
        await session.flush()
        return ReconcileResult(
            consumed=consumed,
            released=released,
            expired=expired,
            entries_deleted=entries_deleted,
            clients_deleted=clients_deleted,
        )
