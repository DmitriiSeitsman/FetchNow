"""PostgreSQL persistence and row-locking for Free quota accounting."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fetchnow.quota.errors import QuotaInvariantError, QuotaStatus
from fetchnow.quota.models import AnonymousClient, FreeDownloadQuotaEntry
from fetchnow.quota.policy import EffectiveDownloadPolicy


class QuotaRepository:
    """Session-bound quota repository.

    Lock order for quota-aware transactions is:
    media job (only when required) -> anonymous client -> download job ->
    quota entry. Reads used only to discover the anonymous-client id do not
    acquire a lock. No quota path acquires these locks in reverse order.
    """

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def database_now(self) -> datetime:
        value = await self._session.scalar(select(func.clock_timestamp()))
        if not isinstance(value, datetime):
            raise QuotaInvariantError("database clock returned an invalid value")
        return value

    async def get_active_client_by_hash(
        self, *, token_hash: bytes, now: datetime
    ) -> AnonymousClient | None:
        row = await self._session.scalar(
            select(AnonymousClient).where(
                AnonymousClient.token_hash == token_hash,
                AnonymousClient.expires_at > now,
            )
        )
        return row if isinstance(row, AnonymousClient) else None

    async def create_client(
        self, *, token_hash: bytes, now: datetime, ttl_seconds: int
    ) -> AnonymousClient:
        if len(token_hash) != 32:
            raise QuotaInvariantError("anonymous token hash length is invalid")
        row = AnonymousClient(
            id=uuid.uuid4(),
            token_hash=token_hash,
            created_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def lock_client(
        self, client_id: uuid.UUID, *, require_active_at: datetime | None = None
    ) -> AnonymousClient | None:
        predicates = [AnonymousClient.id == client_id]
        if require_active_at is not None:
            predicates.append(AnonymousClient.expires_at > require_active_at)
        row = await self._session.scalar(
            select(AnonymousClient).where(*predicates).with_for_update()
        )
        return row if isinstance(row, AnonymousClient) else None

    async def status_locked(
        self,
        *,
        client_id: uuid.UUID,
        now: datetime,
        policy: EffectiveDownloadPolicy,
    ) -> QuotaStatus:
        if policy.download_limit is None or policy.quota_window_seconds is None:
            raise QuotaInvariantError("unlimited policy cannot enter Free accounting")
        cutoff = now - timedelta(seconds=policy.quota_window_seconds)
        used = int(
            await self._session.scalar(
                select(func.count(FreeDownloadQuotaEntry.id)).where(
                    FreeDownloadQuotaEntry.anonymous_client_id == client_id,
                    FreeDownloadQuotaEntry.state == "consumed",
                    FreeDownloadQuotaEntry.consumed_at > cutoff,
                    FreeDownloadQuotaEntry.consumed_at <= now,
                )
            )
            or 0
        )
        reserved = int(
            await self._session.scalar(
                select(func.count(FreeDownloadQuotaEntry.id)).where(
                    FreeDownloadQuotaEntry.anonymous_client_id == client_id,
                    FreeDownloadQuotaEntry.state == "reserved",
                    FreeDownloadQuotaEntry.reservation_expires_at > now,
                )
            )
            or 0
        )
        reset_at: datetime | None = None
        # Reservations reduce remaining capacity but cannot promise a reset.
        # Publish resetAt only when consumed successes alone exhaust the limit.
        if used >= policy.download_limit:
            releasing_consumption = await self._session.scalar(
                select(FreeDownloadQuotaEntry.consumed_at)
                .where(
                    FreeDownloadQuotaEntry.anonymous_client_id == client_id,
                    FreeDownloadQuotaEntry.state == "consumed",
                    FreeDownloadQuotaEntry.consumed_at > cutoff,
                    FreeDownloadQuotaEntry.consumed_at <= now,
                )
                .order_by(FreeDownloadQuotaEntry.consumed_at.asc())
                .offset(used - policy.download_limit)
                .limit(1)
            )
            if isinstance(releasing_consumption, datetime):
                reset_at = releasing_consumption + timedelta(
                    seconds=policy.quota_window_seconds
                )
        return QuotaStatus(
            tier=policy.tier,
            download_limit=policy.download_limit,
            downloads_used=used,
            downloads_reserved=reserved,
            downloads_remaining=max(0, policy.download_limit - used - reserved),
            reset_at=reset_at,
            window_seconds=policy.quota_window_seconds,
            premium_expires_at=None,
        )

    async def reserve_locked(
        self,
        *,
        client_id: uuid.UUID,
        download_job_id: uuid.UUID,
        reserved_at: datetime,
        reservation_expires_at: datetime,
    ) -> FreeDownloadQuotaEntry:
        if reservation_expires_at <= reserved_at:
            raise QuotaInvariantError("reservation expiry must be in the future")
        row = FreeDownloadQuotaEntry(
            id=uuid.uuid4(),
            anonymous_client_id=client_id,
            download_job_id=download_job_id,
            state="reserved",
            reserved_at=reserved_at,
            reservation_expires_at=reservation_expires_at,
            consumed_at=None,
            released_at=None,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def identity_id_for_download(
        self, download_job_id: uuid.UUID
    ) -> uuid.UUID | None:
        value = await self._session.scalar(
            select(FreeDownloadQuotaEntry.anonymous_client_id).where(
                FreeDownloadQuotaEntry.download_job_id == download_job_id
            )
        )
        return value if isinstance(value, uuid.UUID) else None

    async def lock_entry_for_download(
        self, download_job_id: uuid.UUID
    ) -> FreeDownloadQuotaEntry | None:
        row = await self._session.scalar(
            select(FreeDownloadQuotaEntry)
            .where(FreeDownloadQuotaEntry.download_job_id == download_job_id)
            .with_for_update()
        )
        return row if isinstance(row, FreeDownloadQuotaEntry) else None

    @staticmethod
    def consume_locked(entry: FreeDownloadQuotaEntry, *, now: datetime) -> None:
        if entry.state != "reserved":
            raise QuotaInvariantError("ready transition lacks a reserved quota entry")
        entry.state = "consumed"
        entry.consumed_at = now
        entry.released_at = None

    @staticmethod
    def release_locked(
        entry: FreeDownloadQuotaEntry, *, now: datetime, expired: bool = False
    ) -> None:
        if entry.state == "reserved":
            entry.state = "expired" if expired else "released"
            entry.consumed_at = None
            entry.released_at = now
            return
        if entry.state in {"released", "expired"}:
            return
        if entry.state == "consumed":
            raise QuotaInvariantError("cannot release a consumed quota entry")
        raise QuotaInvariantError("unknown quota entry state")

    async def delete_terminal_entries_before(
        self, *, cutoff: datetime, limit: int = 64
    ) -> int:
        candidates = list(
            (
                await self._session.execute(
                    select(
                        FreeDownloadQuotaEntry.id,
                        FreeDownloadQuotaEntry.anonymous_client_id,
                    )
                    .where(
                        (
                            (FreeDownloadQuotaEntry.state == "consumed")
                            & (FreeDownloadQuotaEntry.consumed_at <= cutoff)
                        )
                        | (
                            FreeDownloadQuotaEntry.state.in_(("released", "expired"))
                            & (FreeDownloadQuotaEntry.released_at <= cutoff)
                        )
                    )
                    .order_by(FreeDownloadQuotaEntry.id)
                    .limit(limit)
                )
            ).all()
        )
        deleted = 0
        for entry_id, client_id in candidates:
            client = await self.lock_client(client_id)
            if client is None:
                raise QuotaInvariantError("quota entry references a missing identity")
            entry = await self._session.scalar(
                select(FreeDownloadQuotaEntry)
                .where(FreeDownloadQuotaEntry.id == entry_id)
                .with_for_update(skip_locked=True)
            )
            if entry is None:
                continue
            terminal_before_cutoff = (
                entry.state == "consumed"
                and entry.consumed_at is not None
                and entry.consumed_at <= cutoff
            ) or (
                entry.state in {"released", "expired"}
                and entry.released_at is not None
                and entry.released_at <= cutoff
            )
            if terminal_before_cutoff:
                await self._session.delete(entry)
                deleted += 1
        return deleted

    async def delete_expired_clients_without_entries(
        self, *, now: datetime, limit: int = 64
    ) -> int:
        has_entry = select(FreeDownloadQuotaEntry.id).where(
            FreeDownloadQuotaEntry.anonymous_client_id == AnonymousClient.id
        )
        ids = list(
            (
                await self._session.scalars(
                    select(AnonymousClient.id)
                    .where(
                        AnonymousClient.expires_at <= now,
                        ~has_entry.exists(),
                    )
                    .order_by(AnonymousClient.expires_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        if not ids:
            return 0
        result = await self._session.execute(
            delete(AnonymousClient).where(AnonymousClient.id.in_(ids))
        )
        return int(result.rowcount or 0)
