"""Server-authoritative Free → Premium promotion for READY NORMAL_VIDEO jobs."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fetchnow.core.config import Settings
from fetchnow.downloads.errors import DownloadErrorCode, raise_download_error
from fetchnow.downloads.grant_repository import BrowserGrantRepository
from fetchnow.downloads.models import MediaDownloadJob
from fetchnow.downloads.snapshot_codec import (
    attach_effective_policy_snapshot,
    decode_authorized_identity_id,
    decode_effective_policy_snapshot,
    decode_selected_format_snapshot,
    strip_effective_policy_snapshot,
)
from fetchnow.downloads.states import MediaDownloadJobState
from fetchnow.jobs.credentials import hash_access_token, tokens_match
from fetchnow.jobs.repository import MediaJobRepository
from fetchnow.media_inspection.models import MediaKind
from fetchnow.premium.service import PremiumEntitlementService
from fetchnow.quota.delivery import normalize_intervals
from fetchnow.quota.models import FreeDownloadDeliveryRange, FreeDownloadQuotaEntry
from fetchnow.quota.policy import effective_download_policy
from fetchnow.quota.repository import QuotaRepository

logger = logging.getLogger("fetchnow.downloads.promote")


@dataclass(frozen=True, slots=True)
class PromoteResult:
    """Result of a premium-upgrade attempt.

    ``created`` is True when this call performed the Free → Premium transition.
    """

    job: MediaDownloadJob
    created: bool


class DownloadPremiumUpgradeService:
    """Promote a READY Free NORMAL_VIDEO artifact to a Premium policy snapshot."""

    __slots__ = ("_settings",)

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def upgrade(
        self,
        *,
        download_job_id: uuid.UUID,
        access_token: str,
        anonymous_client_id: uuid.UUID,
        session: AsyncSession,
    ) -> PromoteResult:
        credential_hash = hash_access_token(access_token)
        quota = QuotaRepository(session)
        grants = BrowserGrantRepository(session)
        now = await quota.database_now()

        client = await quota.lock_client(anonymous_client_id, require_active_at=now)
        if client is None:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND,
                internal_reason="IDENTITY_INACTIVE",
            )

        job = await grants.lock_download_job(download_job_id)
        if job is None:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND,
                internal_reason="MISSING",
            )

        parent_repo = MediaJobRepository(session)
        parent = await parent_repo.get_by_id(job.media_job_id)
        if parent is None or not tokens_match(parent.credential_hash, credential_hash):
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND,
                internal_reason="UNAUTHORIZED",
            )

        if job.expires_at <= now:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_EXPIRED,
                internal_reason="JOB_EXPIRED",
            )
        if job.public_state != MediaDownloadJobState.READY.value:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_NOT_READY,
                internal_reason="NOT_READY",
            )
        if (
            job.artifact_id is None
            or job.artifact_bytes is None
            or int(job.artifact_bytes) <= 0
            or int(job.fence_token) < 1
        ):
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_NOT_READY,
                internal_reason="ARTIFACT_INCOHERENT",
            )

        try:
            format_snapshot = decode_selected_format_snapshot(
                strip_effective_policy_snapshot(dict(job.selected_format_snapshot)),
                expected_format_option_id=str(job.format_option_id),
            )
        except Exception:
            raise_download_error(
                DownloadErrorCode.FORMAT_UNAVAILABLE,
                internal_reason="FORMAT_SNAPSHOT_INVALID",
            )
        if format_snapshot.media_kind is not MediaKind.NORMAL_VIDEO:
            raise_download_error(
                DownloadErrorCode.MEDIA_CAPABILITY_UNAVAILABLE,
                internal_reason="MEDIA_KIND_INELIGIBLE",
            )

        try:
            stored_policy = decode_effective_policy_snapshot(
                job.selected_format_snapshot
            )
        except Exception:
            raise_download_error(
                DownloadErrorCode.FORMAT_UNAVAILABLE,
                internal_reason="POLICY_SNAPSHOT_INVALID",
            )
        if stored_policy is None:
            raise_download_error(
                DownloadErrorCode.FORMAT_UNAVAILABLE,
                internal_reason="POLICY_SNAPSHOT_MISSING",
            )

        authorized_identity = decode_authorized_identity_id(
            job.selected_format_snapshot
        )
        if stored_policy.tier == "premium":
            if authorized_identity != anonymous_client_id:
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND,
                    internal_reason="PREMIUM_IDENTITY_MISMATCH",
                )
            logger.info("premium_upgrade outcome=idempotent")
            return PromoteResult(job=job, created=False)

        if stored_policy.tier != "free":
            raise_download_error(
                DownloadErrorCode.FORMAT_UNAVAILABLE,
                internal_reason="POLICY_TIER_UNKNOWN",
            )

        premium = await PremiumEntitlementService().status_for_client(
            anonymous_client_id=anonymous_client_id, session=session
        )
        if (
            not premium.capability.is_premium
            or premium.capability.premium_expires_at is None
            or premium.capability.premium_expires_at <= now
        ):
            raise_download_error(
                DownloadErrorCode.MEDIA_CAPABILITY_REQUIRES_PREMIUM,
                internal_reason="PREMIUM_INACTIVE",
            )

        entry = await quota.lock_entry_for_download(job.id)
        if entry is None:
            if self._settings.free_download_quota_enabled:
                raise_download_error(
                    DownloadErrorCode.QUOTA_STATE_INCOHERENT,
                    internal_reason="QUOTA_ENTRY_MISSING",
                )
        else:
            if entry.anonymous_client_id != anonymous_client_id:
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND,
                    internal_reason="QUOTA_IDENTITY_MISMATCH",
                )
            rows = await self._lock_ranges(session, entry.id)
            await self._close_stale_leases(session, rows=rows, now=now)
            rows = await self._lock_ranges(session, entry.id)
            if any(
                row.state == "active"
                and row.lease_expires_at is not None
                and row.lease_expires_at > now
                for row in rows
            ):
                raise_download_error(
                    DownloadErrorCode.DELIVERY_IN_PROGRESS,
                    internal_reason="ACTIVE_DELIVERY_LEASE",
                )
            self._assert_generation(rows, job)
            coverage = normalize_intervals(
                [
                    (int(row.request_start), int(row.served_end_exclusive))
                    for row in rows
                    if int(row.served_end_exclusive) > int(row.request_start)
                ]
            )
            size = int(job.artifact_bytes)
            complete = coverage == [(0, size)]
            await self._apply_quota_rule(
                quota,
                entry=entry,
                complete_coverage=complete,
                now=now,
            )

        premium_policy = effective_download_policy(self._settings, premium.capability)
        format_only = strip_effective_policy_snapshot(
            dict(job.selected_format_snapshot)
        )
        job.selected_format_snapshot = attach_effective_policy_snapshot(
            format_only,
            premium_policy,
            authorized_identity_id=anonymous_client_id,
        )
        job.updated_at = now

        active_grants = await grants.list_active_for_job(
            download_job_id=job.id, now=now
        )
        revoked = len(active_grants)
        await grants.revoke_grants(active_grants, now=now)
        await session.flush()
        logger.info(
            "premium_upgrade outcome=ok grants_revoked=%s reservation_handled=%s",
            revoked,
            entry.state if entry is not None else "none",
        )
        return PromoteResult(job=job, created=True)

    @staticmethod
    async def _apply_quota_rule(
        quota: QuotaRepository,
        *,
        entry: FreeDownloadQuotaEntry,
        complete_coverage: bool,
        now: datetime,
    ) -> None:
        if entry.state == "consumed":
            return
        if entry.state in {"released", "expired"}:
            raise_download_error(
                DownloadErrorCode.QUOTA_STATE_INCOHERENT,
                internal_reason="FREE_TERMINAL_QUOTA",
            )
        if entry.state != "reserved":
            raise_download_error(
                DownloadErrorCode.QUOTA_STATE_INCOHERENT,
                internal_reason="QUOTA_STATE_UNKNOWN",
            )
        if complete_coverage:
            # Successful Free delivery already happened; never refund.
            quota.consume_locked(entry, now=now)
            logger.info("premium_upgrade_quota outcome=consumed_complete_coverage")
            return
        quota.release_locked(entry, now=now)
        logger.info("premium_upgrade_quota outcome=released_incomplete")

    @staticmethod
    async def _lock_ranges(
        session: AsyncSession, entry_id: uuid.UUID
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
    async def _close_stale_leases(
        session: AsyncSession,
        *,
        rows: list[FreeDownloadDeliveryRange],
        now: datetime,
    ) -> None:
        changed = False
        for row in rows:
            if row.state != "active":
                continue
            if row.lease_expires_at is not None and row.lease_expires_at > now:
                continue
            if int(row.served_end_exclusive) == int(row.request_start):
                await session.delete(row)
            else:
                row.request_end_exclusive = row.served_end_exclusive
                row.state = "closed"
                row.lease_expires_at = None
                row.closed_at = now
            changed = True
        if changed:
            await session.flush()

    @staticmethod
    def _assert_generation(
        rows: list[FreeDownloadDeliveryRange], job: MediaDownloadJob
    ) -> None:
        for row in rows:
            if (
                row.artifact_id != job.artifact_id
                or int(row.fence_token) != int(job.fence_token)
                or int(row.artifact_bytes) != int(job.artifact_bytes or 0)
            ):
                raise_download_error(
                    DownloadErrorCode.QUOTA_STATE_INCOHERENT,
                    internal_reason="LEDGER_GENERATION_MISMATCH",
                )
