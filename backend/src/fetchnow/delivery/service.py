"""Authorize and open a ready download artifact for delivery."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from fetchnow.core.config import Settings
from fetchnow.delivery.reader import ArtifactReader, OpenArtifactHandle
from fetchnow.downloads.browser_grant_tokens import (
    grant_hashes_match,
    hash_grant_token,
    parse_grant_token,
)
from fetchnow.downloads.errors import DownloadErrorCode, raise_download_error
from fetchnow.downloads.filename import (
    content_disposition_header,
    fallback_filename,
    is_safe_suggested_filename,
)
from fetchnow.downloads.grant_repository import BrowserGrantRepository
from fetchnow.downloads.models import MediaDownloadJob
from fetchnow.downloads.repository import MediaDownloadJobRepository
from fetchnow.downloads.states import MediaDownloadJobState
from fetchnow.jobs.credentials import hash_access_token, tokens_match
from fetchnow.jobs.errors import JobError, JobErrorCode
from fetchnow.jobs.repository import MediaJobRepository
from fetchnow.jobs.states import MediaJobState


@dataclass(frozen=True, slots=True, repr=False)
class DeliveryAuthorization:
    """DB-validated ready job metadata used to open the artifact."""

    job: MediaDownloadJob
    now: datetime

    def __repr__(self) -> str:
        return (
            f"DeliveryAuthorization(job_id={self.job.id!r}, "
            f"state={self.job.public_state!r})"
        )


class DeliveryService:
    """Parent-Bearer authorization + read-only artifact open."""

    __slots__ = ("_settings", "_reader", "_semaphore")

    def __init__(
        self,
        settings: Settings,
        *,
        reader: ArtifactReader | None = None,
    ) -> None:
        self._settings = settings
        if not settings.media_delivery_enabled:
            # Reader is only constructed when enabled so unsafe roots fail closed
            # at delivery startup, not at API import time.
            self._reader = reader
        else:
            self._reader = reader or ArtifactReader(settings.media_delivery_root)
        self._semaphore = asyncio.Semaphore(settings.media_delivery_concurrency)

    @property
    def semaphore(self) -> asyncio.Semaphore:
        return self._semaphore

    @property
    def chunk_bytes(self) -> int:
        return self._settings.media_delivery_chunk_bytes

    @property
    def range_enabled(self) -> bool:
        return self._settings.media_delivery_range_enabled

    async def authorize(
        self,
        *,
        download_job_id: uuid.UUID,
        access_token: str,
        session: AsyncSession,
    ) -> DeliveryAuthorization:
        """Return a ready coherent job or raise a catalog error.

        Missing/wrong Bearer and unknown UUID are indistinguishable
        (``DOWNLOAD_JOB_NOT_FOUND``). Malformed tokens are rejected via the
        shared credential parser before any database lookup.
        """
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

        if not self._settings.media_delivery_enabled:
            raise_download_error(
                DownloadErrorCode.DELIVERY_DISABLED,
                internal_reason="DELIVERY_DISABLED",
            )

        repo = MediaDownloadJobRepository(session)
        job = await repo.get_by_id(download_job_id)
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

        now = await repo.database_now()
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
        return DeliveryAuthorization(job=job, now=now)

    async def authorize_browser_grant(
        self,
        *,
        grant_id: uuid.UUID,
        raw_token: str,
        session: AsyncSession,
    ) -> DeliveryAuthorization:
        """Authorize a native browser GET/HEAD via grant UUID + cookie token.

        Unknown grant, wrong hash, expired grant/job, non-ready job, replaced
        artifact, or stale fence are externally indistinguishable
        (``DOWNLOAD_JOB_NOT_FOUND`` or ``DOWNLOAD_EXPIRED`` as appropriate).

        Uses a plain immutable SELECT (no ``FOR UPDATE``). Authorization is
        decided before streaming opens the artifact FD — the same TOCTOU class
        as artifact expiry between authorize and open. Issuance continues to
        lock the parent download row when enforcing the active-grant cap.
        """
        # Validate token shape before any DB work.
        parse_grant_token(raw_token)
        token_hash = hash_grant_token(raw_token)

        if not self._settings.media_delivery_enabled:
            raise_download_error(
                DownloadErrorCode.DELIVERY_DISABLED,
                internal_reason="DELIVERY_DISABLED",
            )
        if not self._settings.media_browser_delivery_enabled:
            raise_download_error(
                DownloadErrorCode.DELIVERY_DISABLED,
                internal_reason="BROWSER_DELIVERY_DISABLED",
            )

        grants = BrowserGrantRepository(session)
        grant = await grants.get_by_id(grant_id)
        if grant is None or not grant_hashes_match(grant.token_hash, token_hash):
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND,
                internal_reason="GRANT_UNAUTHORIZED",
            )

        now = await grants.database_now()
        if grant.revoked_at is not None or grant.expires_at <= now:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_EXPIRED,
                internal_reason="GRANT_EXPIRED",
            )

        repo = MediaDownloadJobRepository(session)
        job = await repo.get_by_id(grant.download_job_id)
        if job is None:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND,
                internal_reason="GRANT_JOB_MISSING",
            )

        parent_repo = MediaJobRepository(session)
        parent = await parent_repo.get_by_id(job.media_job_id)
        if parent is None:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND,
                internal_reason="GRANT_PARENT_MISSING",
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
        if job.artifact_id != grant.artifact_id or int(job.fence_token) != int(
            grant.fence_token
        ):
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND,
                internal_reason="GRANT_ARTIFACT_MISMATCH",
            )

        return DeliveryAuthorization(job=job, now=now)

    def open_artifact(self, authz: DeliveryAuthorization) -> OpenArtifactHandle:
        """Open the published artifact through the read-only reader."""
        if self._reader is None:
            raise_download_error(
                DownloadErrorCode.DELIVERY_DISABLED,
                internal_reason="READER_UNCONFIGURED",
            )
        job = authz.job
        # Re-check expiry immediately before open (TOCTOU with worker cleanup).
        if job.expires_at <= authz.now:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_EXPIRED,
                internal_reason="TTL_AT_OPEN",
            )
        assert job.artifact_id is not None
        assert job.artifact_bytes is not None
        assert job.artifact_container is not None
        assert job.artifact_content_type is not None
        return self._reader.open_published(
            artifact_id=job.artifact_id,
            download_job_id=job.id,
            format_option_id=job.format_option_id,
            expected_bytes=int(job.artifact_bytes),
            expected_container=str(job.artifact_container),
            expected_content_type=str(job.artifact_content_type),
            expected_expires_at=job.expires_at,
            expected_fence=int(job.fence_token),
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
        if int(job.artifact_bytes) <= 0:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="READY_POINTER_EMPTY",
            )

    @staticmethod
    def content_disposition(job: MediaDownloadJob) -> str:
        """RFC 8187 attachment name from the persisted suggested filename."""
        container = str(job.artifact_container or "").strip().lower()
        try:
            fallback = fallback_filename(job.id, container)
            stored = job.suggested_filename
            if stored is None or stored == "":
                name = fallback
            elif not is_safe_suggested_filename(stored, container=container):
                raise_download_error(
                    DownloadErrorCode.INTERNAL_ERROR,
                    internal_reason="SUGGESTED_FILENAME_INVALID",
                )
            else:
                name = stored
            return content_disposition_header(filename=name, ascii_fallback=fallback)
        except ValueError:
            raise_download_error(
                DownloadErrorCode.INTERNAL_ERROR,
                internal_reason="CONTENT_DISPOSITION_INVALID",
            )

    @staticmethod
    def security_headers(
        *, content_type: str, content_disposition: str
    ) -> dict[str, str]:
        return {
            "Content-Type": content_type,
            "Content-Disposition": content_disposition,
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        }
