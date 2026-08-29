"""Anonymous identity bootstrap and public Free quota projection."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from fetchnow.core.config import Settings
from fetchnow.quota.errors import (
    AnonymousIdentityRequiredError,
    FreeQuotaExceededError,
    QuotaStatus,
)
from fetchnow.quota.policy import effective_download_policy
from fetchnow.quota.repository import QuotaRepository
from fetchnow.quota.tokens import (
    COOKIE_NAME,
    extract_anonymous_cookie,
    generate_anonymous_token,
    hash_anonymous_token,
)


@dataclass(frozen=True, slots=True, repr=False)
class AnonymousIdentity:
    id: uuid.UUID
    expires_at: datetime
    raw_token: str | None = None
    cookie_max_age: int | None = None

    def __repr__(self) -> str:
        return f"AnonymousIdentity(id={self.id!r}, expires_at={self.expires_at!r})"


class QuotaService:
    __slots__ = ("_settings",)

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def bootstrap_identity(
        self, *, cookie_header: str | None, session: AsyncSession
    ) -> AnonymousIdentity:
        repo = QuotaRepository(session)
        now = await repo.database_now()
        token = extract_anonymous_cookie(cookie_header)
        if token is not None:
            token_hash = hash_anonymous_token(token)
            if token_hash is not None:
                existing = await repo.get_active_client_by_hash(
                    token_hash=token_hash, now=now
                )
                if existing is not None:
                    return AnonymousIdentity(existing.id, existing.expires_at)

        # Never adopt a client-supplied unknown value. Mint server-side only.
        raw_token = generate_anonymous_token()
        token_hash = hash_anonymous_token(raw_token)
        if token_hash is None:
            raise RuntimeError("generated anonymous token was not canonical")
        created = await repo.create_client(
            token_hash=token_hash,
            now=now,
            ttl_seconds=self._settings.anonymous_client_ttl_seconds,
        )
        return AnonymousIdentity(
            created.id,
            created.expires_at,
            raw_token,
            int(self._settings.anonymous_client_ttl_seconds),
        )

    async def require_identity(
        self, *, cookie_header: str | None, session: AsyncSession
    ) -> AnonymousIdentity:
        token = extract_anonymous_cookie(cookie_header)
        token_hash = hash_anonymous_token(token) if token is not None else None
        if token_hash is None:
            raise AnonymousIdentityRequiredError()
        repo = QuotaRepository(session)
        now = await repo.database_now()
        existing = await repo.get_active_client_by_hash(token_hash=token_hash, now=now)
        if existing is None:
            raise AnonymousIdentityRequiredError()
        return AnonymousIdentity(existing.id, existing.expires_at)

    async def status(
        self, *, identity_id: uuid.UUID, session: AsyncSession
    ) -> QuotaStatus:
        repo = QuotaRepository(session)
        now = await repo.database_now()
        client = await repo.lock_client(identity_id, require_active_at=now)
        if client is None:
            raise AnonymousIdentityRequiredError()
        return await repo.status_locked(
            client_id=identity_id,
            now=now,
            policy=effective_download_policy(self._settings),
        )

    async def admit_locked(
        self,
        *,
        identity_id: uuid.UUID,
        download_job_id: uuid.UUID,
        reservation_expires_at: datetime,
        session: AsyncSession,
    ) -> QuotaStatus:
        repo = QuotaRepository(session)
        now = await repo.database_now()
        client = await repo.lock_client(identity_id, require_active_at=now)
        if client is None:
            raise AnonymousIdentityRequiredError()
        policy = effective_download_policy(self._settings)
        before = await repo.status_locked(
            client_id=identity_id, now=now, policy=policy
        )
        if before.downloads_remaining < 1:
            raise FreeQuotaExceededError(before)
        await repo.reserve_locked(
            client_id=identity_id,
            download_job_id=download_job_id,
            reserved_at=now,
            reservation_expires_at=reservation_expires_at,
        )
        return QuotaStatus(
            tier=before.tier,
            download_limit=before.download_limit,
            downloads_used=before.downloads_used,
            downloads_reserved=before.downloads_reserved + 1,
            downloads_remaining=max(0, before.downloads_remaining - 1),
            reset_at=before.reset_at,
        )

    def set_cookie_header(self, identity: AnonymousIdentity) -> str | None:
        if identity.raw_token is None:
            return None
        expires = identity.expires_at.astimezone(UTC)
        if identity.cookie_max_age is None:
            return None
        return (
            f"{COOKIE_NAME}={identity.raw_token}; Path=/; "
            f"Max-Age={identity.cookie_max_age}; "
            f"Expires={format_datetime(expires, usegmt=True)}; "
            "HttpOnly; Secure; SameSite=Lax"
        )

    @staticmethod
    def to_public_dict(status: QuotaStatus) -> dict[str, Any]:
        return {
            "tier": status.tier,
            "downloadLimit": status.download_limit,
            "downloadsUsed": status.downloads_used,
            "downloadsReserved": status.downloads_reserved,
            "downloadsRemaining": status.downloads_remaining,
            "resetAt": (
                status.reset_at.isoformat().replace("+00:00", "Z")
                if status.reset_at is not None
                else None
            ),
        }

    @staticmethod
    def error_details(status: QuotaStatus) -> dict[str, Any]:
        return {
            "limit": status.download_limit,
            "remaining": status.downloads_remaining,
            "resetAt": (
                status.reset_at.isoformat().replace("+00:00", "Z")
                if status.reset_at is not None
                else None
            ),
        }
