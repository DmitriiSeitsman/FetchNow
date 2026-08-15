"""Persistence for browser delivery grants."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fetchnow.downloads.errors import DownloadErrorCode, raise_download_error
from fetchnow.downloads.grant_models import MediaDeliveryGrant
from fetchnow.downloads.models import MediaDownloadJob


class BrowserGrantRepository:
    """Session-bound grant insert / lookup / bounded cleanup."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def database_now(self) -> datetime:
        value = await self._session.scalar(select(func.clock_timestamp()))
        if not isinstance(value, datetime):
            raise_download_error(
                DownloadErrorCode.INTERNAL_ERROR,
                internal_reason="DATABASE_CLOCK_INVALID",
            )
        return value

    async def lock_download_job(
        self, download_job_id: uuid.UUID
    ) -> MediaDownloadJob | None:
        """Lock the parent download job row for grant issuance."""
        stmt = (
            select(MediaDownloadJob)
            .where(MediaDownloadJob.id == download_job_id)
            .with_for_update()
        )
        row = await self._session.scalar(stmt)
        return row if isinstance(row, MediaDownloadJob) else None

    async def list_active_for_job(
        self,
        *,
        download_job_id: uuid.UUID,
        now: datetime,
    ) -> list[MediaDeliveryGrant]:
        stmt = (
            select(MediaDeliveryGrant)
            .where(
                MediaDeliveryGrant.download_job_id == download_job_id,
                MediaDeliveryGrant.revoked_at.is_(None),
                MediaDeliveryGrant.expires_at > now,
            )
            .order_by(MediaDeliveryGrant.created_at.asc())
            .with_for_update()
        )
        return list((await self._session.scalars(stmt)).all())

    async def revoke_grants(
        self, grants: list[MediaDeliveryGrant], *, now: datetime
    ) -> None:
        for grant in grants:
            # Never write revoked_at < created_at: a stale caller clock must not
            # trip ck_media_delivery_grants_revoked_after_created.
            grant.revoked_at = now if now >= grant.created_at else grant.created_at
        if grants:
            await self._session.flush()

    async def insert_grant(
        self,
        *,
        grant_id: uuid.UUID,
        download_job_id: uuid.UUID,
        token_hash: bytes,
        artifact_id: uuid.UUID,
        fence_token: int,
        created_at: datetime,
        expires_at: datetime,
    ) -> MediaDeliveryGrant:
        if len(token_hash) != 32:
            raise_download_error(
                DownloadErrorCode.INTERNAL_ERROR,
                internal_reason="GRANT_HASH_LENGTH",
            )
        if expires_at <= created_at:
            raise_download_error(
                DownloadErrorCode.INTERNAL_ERROR,
                internal_reason="GRANT_TTL_INVALID",
            )
        row = MediaDeliveryGrant(
            id=grant_id,
            download_job_id=download_job_id,
            token_hash=token_hash,
            artifact_id=artifact_id,
            fence_token=fence_token,
            created_at=created_at,
            expires_at=expires_at,
            revoked_at=None,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_by_id(self, grant_id: uuid.UUID) -> MediaDeliveryGrant | None:
        """Immutable snapshot read for delivery authorization (no FOR UPDATE)."""
        stmt = select(MediaDeliveryGrant).where(MediaDeliveryGrant.id == grant_id)
        row = await self._session.scalar(stmt)
        return row if isinstance(row, MediaDeliveryGrant) else None

    async def delete_expired_batch(self, *, now: datetime, limit: int = 64) -> int:
        """Delete expired or revoked grants in a bounded batch. No artifact I/O."""
        if limit < 1 or limit > 256:
            raise_download_error(
                DownloadErrorCode.INTERNAL_ERROR,
                internal_reason="GRANT_CLEANUP_LIMIT",
            )
        # Select ids first with SKIP LOCKED so worker claiming is not blocked.
        ids_stmt = (
            select(MediaDeliveryGrant.id)
            .where(
                and_(
                    (
                        (MediaDeliveryGrant.expires_at <= now)
                        | (MediaDeliveryGrant.revoked_at.is_not(None))
                    ),
                )
            )
            .order_by(MediaDeliveryGrant.expires_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        ids = list((await self._session.scalars(ids_stmt)).all())
        if not ids:
            return 0
        result = await self._session.execute(
            delete(MediaDeliveryGrant).where(MediaDeliveryGrant.id.in_(ids))
        )
        await self._session.flush()
        return int(result.rowcount or 0)
