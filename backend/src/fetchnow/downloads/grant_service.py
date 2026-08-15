"""Issue short-lived browser-native delivery grants (API process)."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from fetchnow.core.config import Settings
from fetchnow.downloads.browser_grant_tokens import (
    COOKIE_NAME,
    content_path_for_grant,
    generate_grant_token,
    hash_grant_token,
)
from fetchnow.downloads.errors import DownloadErrorCode, raise_download_error
from fetchnow.downloads.grant_repository import BrowserGrantRepository
from fetchnow.downloads.models import MediaDownloadJob
from fetchnow.downloads.repository import MediaDownloadJobRepository
from fetchnow.downloads.states import MediaDownloadJobState
from fetchnow.jobs.credentials import hash_access_token, tokens_match
from fetchnow.jobs.errors import JobError, JobErrorCode
from fetchnow.jobs.repository import MediaJobRepository
from fetchnow.jobs.states import MediaJobState

logger = logging.getLogger("fetchnow.downloads.grant_service")


@dataclass(frozen=True, slots=True, repr=False)
class IssuedBrowserGrant:
    """Public grant issuance result — never carries the raw token."""

    grant_id: uuid.UUID
    download_path: str
    expires_at: datetime
    raw_token: str
    cookie_max_age: int

    def __repr__(self) -> str:
        return (
            f"IssuedBrowserGrant(grant_id={self.grant_id!r}, "
            f"expires_at={self.expires_at!r})"
        )


class BrowserGrantService:
    """Authorize parent Bearer and create a hashed browser delivery grant."""

    __slots__ = ("_settings",)

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def set_cookie_header(self, issued: IssuedBrowserGrant) -> str:
        """Build a Secure HttpOnly SameSite=Strict Set-Cookie (no Domain)."""
        path = content_path_for_grant(issued.grant_id)
        # Production always issues Secure cookies. Tests may inject transport.
        return (
            f"{COOKIE_NAME}={issued.raw_token}; "
            f"Path={path}; "
            f"Max-Age={issued.cookie_max_age}; "
            "HttpOnly; Secure; SameSite=Strict"
        )

    async def issue(
        self,
        *,
        download_job_id: uuid.UUID,
        access_token: str,
        session: AsyncSession,
    ) -> IssuedBrowserGrant:
        try:
            credential_hash = hash_access_token(access_token)
        except JobError as exc:
            if exc.code is JobErrorCode.INVALID_ACCESS_TOKEN:
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND,
                    internal_reason="TOKEN_SHAPE",
                )
            raise_download_error(
                DownloadErrorCode.INTERNAL_ERROR,
                internal_reason="TOKEN_HASH_FAILED",
            )

        if not self._settings.media_browser_delivery_enabled:
            raise_download_error(
                DownloadErrorCode.DELIVERY_DISABLED,
                internal_reason="BROWSER_DELIVERY_DISABLED",
            )
        if not self._settings.media_downloads_enabled:
            raise_download_error(
                DownloadErrorCode.DOWNLOADS_DISABLED,
                internal_reason="DOWNLOADS_DISABLED",
            )

        grants = BrowserGrantRepository(session)
        download_repo = MediaDownloadJobRepository(session)

        # Capture the clock only after the job lock so concurrent issuers that
        # waited cannot revoke an already-committed grant with a stale ``now``
        # (CHECK ck_media_delivery_grants_revoked_after_created).
        job = await grants.lock_download_job(download_job_id)
        if job is None:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND,
                internal_reason="MISSING",
            )
        now = await grants.database_now()

        parent_repo = MediaJobRepository(session)
        parent = await parent_repo.get_by_id(job.media_job_id)
        if parent is None or not tokens_match(parent.credential_hash, credential_hash):
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND,
                internal_reason="UNAUTHORIZED",
            )

        if (
            parent.public_state == MediaJobState.EXPIRED.value
            or parent.expires_at <= now
            or job.public_state == MediaDownloadJobState.EXPIRED.value
            or job.expires_at <= now
        ):
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_EXPIRED,
                internal_reason="TTL_ELAPSED",
            )

        if job.public_state != MediaDownloadJobState.READY.value:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_NOT_READY,
                internal_reason="NOT_READY",
            )
        self._assert_ready_pointer(job)

        configured_ttl = int(self._settings.media_browser_grant_ttl_seconds)
        grant_expires = min(now + timedelta(seconds=configured_ttl), job.expires_at)
        if grant_expires <= now:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_EXPIRED,
                internal_reason="GRANT_TTL_ELAPSED",
            )

        max_active = int(self._settings.media_browser_grant_max_active)
        active = await grants.list_active_for_job(
            download_job_id=job.id,
            now=now,
        )
        # Evict oldest active grants when at cap so tabs do not starve, but
        # never grow unbounded.
        overflow = len(active) - (max_active - 1)
        if overflow > 0:
            await grants.revoke_grants(active[:overflow], now=now)

        raw_token = generate_grant_token()
        token_hash = hash_grant_token(raw_token)
        grant_id = uuid.uuid4()
        assert job.artifact_id is not None
        await grants.insert_grant(
            grant_id=grant_id,
            download_job_id=job.id,
            token_hash=token_hash,
            artifact_id=job.artifact_id,
            fence_token=int(job.fence_token),
            created_at=now,
            expires_at=grant_expires,
        )
        # Touch download_repo clock for parity / future hooks (no-op use).
        del download_repo

        max_age = max(1, int((grant_expires - now).total_seconds()))
        logger.info(
            "browser_delivery_grant_issued outcome=ok",
            extra={"outcome": "ok"},
        )
        return IssuedBrowserGrant(
            grant_id=grant_id,
            download_path=content_path_for_grant(grant_id),
            expires_at=grant_expires,
            raw_token=raw_token,
            cookie_max_age=max_age,
        )

    @staticmethod
    def _assert_ready_pointer(job: MediaDownloadJob) -> None:
        if (
            job.artifact_id is None
            or job.artifact_bytes is None
            or job.artifact_container is None
            or job.artifact_content_type is None
            or job.completed_at is None
            or job.public_error_code is not None
        ):
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="READY_POINTER_INCOMPLETE",
            )

    @staticmethod
    def to_public_dict(issued: IssuedBrowserGrant) -> dict[str, str]:
        return {
            "downloadPath": issued.download_path,
            "expiresAt": issued.expires_at.isoformat().replace("+00:00", "Z"),
        }
