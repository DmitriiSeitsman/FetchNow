"""API-facing media-download-job orchestration service."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from fetchnow.core.config import Settings
from fetchnow.downloads.byte_progress import DOWNLOAD_PERCENT_STAGES
from fetchnow.downloads.errors import (
    DownloadError,
    DownloadErrorCode,
    raise_download_error,
)
from fetchnow.downloads.filename import (
    fallback_filename,
    is_safe_suggested_filename,
    suggested_filename_for,
)
from fetchnow.downloads.models import MediaDownloadJob
from fetchnow.downloads.progress import (
    DownloadProgressStage,
    progress_for_public_state,
)
from fetchnow.downloads.repository import MediaDownloadJobRepository
from fetchnow.downloads.selection import format_snapshot_from_media_format
from fetchnow.downloads.snapshot_codec import (
    decode_selected_format_snapshot,
    encode_selected_format_snapshot,
)
from fetchnow.downloads.states import MediaDownloadJobState, project_public_state
from fetchnow.jobs.credentials import hash_access_token, tokens_match
from fetchnow.jobs.metadata_codec import media_metadata_from_jsonable
from fetchnow.jobs.repository import MediaJobRepository
from fetchnow.jobs.states import MediaJobState
from fetchnow.media_inspection.models import FormatCategory, MediaFormat


@dataclass(frozen=True, slots=True, repr=False)
class DownloadJobView:
    """Public-safe download job snapshot (no access token, path, or token)."""

    id: uuid.UUID
    media_job_id: uuid.UUID
    public_state: str
    format_option_id: str
    selected_format: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    completed_at: datetime | None
    artifact_ready: bool
    error_code: str | None
    progress_stage: str
    attempt_count: int
    max_attempts: int
    next_attempt_at: datetime | None
    cancellable: bool
    progress_percent: int | None = None
    artifact_bytes: int | None = None
    suggested_filename: str = ""
    created: bool = False

    def __repr__(self) -> str:
        return (
            f"DownloadJobView(id={self.id!r}, media_job_id={self.media_job_id!r}, "
            f"public_state={self.public_state!r}, "
            f"format_option_id={self.format_option_id!r}, "
            f"artifact_ready={self.artifact_ready!r}, "
            f"error_code={self.error_code!r})"
        )


def _find_format(
    metadata_formats: tuple[MediaFormat, ...], option_id: str
) -> MediaFormat:
    for fmt in metadata_formats:
        if fmt.format_option_id == option_id:
            return fmt
    raise_download_error(
        DownloadErrorCode.FORMAT_NOT_FOUND,
        internal_reason="FORMAT_MISSING",
    )


def _assert_format_eligible(fmt: MediaFormat, *, muxing_required: bool) -> None:
    if muxing_required:
        raise_download_error(
            DownloadErrorCode.MUXING_UNAVAILABLE,
            internal_reason="MUXING_REQUIRED",
        )
    if not fmt.free_tier_eligible:
        raise_download_error(
            DownloadErrorCode.FORMAT_NOT_ELIGIBLE,
            internal_reason="NOT_FREE_TIER",
        )
    if fmt.category is not FormatCategory.PROGRESSIVE:
        raise_download_error(
            DownloadErrorCode.FORMAT_NOT_ELIGIBLE,
            internal_reason="NOT_PROGRESSIVE",
        )
    if not (fmt.has_video and fmt.has_audio):
        raise_download_error(
            DownloadErrorCode.FORMAT_NOT_ELIGIBLE,
            internal_reason="MISSING_AV",
        )


class DownloadJobService:
    """Create and read durable download jobs authorized via parent MediaJob."""

    __slots__ = ("_settings",)

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def create(
        self,
        *,
        media_job_id: uuid.UUID,
        format_option_id: str,
        access_token: str,
        session: AsyncSession,
    ) -> DownloadJobView:
        """Enqueue or recover a download job for an inspected parent MediaJob."""
        if not self._settings.media_downloads_enabled:
            raise_download_error(
                DownloadErrorCode.DOWNLOADS_DISABLED,
                internal_reason="DOWNLOADS_DISABLED",
            )

        credential_hash = hash_access_token(access_token)
        parent_repo = MediaJobRepository(session)
        parent = await parent_repo.get_by_id(media_job_id)
        if parent is None or not tokens_match(parent.credential_hash, credential_hash):
            # Indistinguishable from unknown id — UUID alone never authorizes.
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND,
                internal_reason="PARENT_MISSING_OR_UNAUTHORIZED",
            )

        now = await parent_repo.database_now()
        if (
            parent.public_state == MediaJobState.EXPIRED.value
            or parent.expires_at <= now
        ):
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_EXPIRED,
                internal_reason="PARENT_EXPIRED",
            )
        if parent.public_state != MediaJobState.INSPECTED.value:
            raise_download_error(
                DownloadErrorCode.PARENT_JOB_NOT_READY,
                internal_reason="PARENT_NOT_INSPECTED",
            )
        if not isinstance(parent.result_metadata, dict):
            raise_download_error(
                DownloadErrorCode.PARENT_JOB_NOT_READY,
                internal_reason="PARENT_METADATA_MISSING",
            )

        metadata = media_metadata_from_jsonable(
            parent.result_metadata,
            max_bytes=self._settings.media_job_result_max_bytes,
        )
        fmt = _find_format(metadata.formats, format_option_id)
        _assert_format_eligible(fmt, muxing_required=metadata.muxing_required)
        snapshot = format_snapshot_from_media_format(fmt)
        job_id = uuid.uuid4()
        filename = suggested_filename_for(
            title=metadata.title,
            container=fmt.container,
            download_job_id=job_id,
        )

        repo = MediaDownloadJobRepository(session)
        job, created = await repo.enqueue_or_get(
            media_job_id=parent.id,
            format_option_id=fmt.format_option_id,
            provider_id=parent.provider_id,
            canonical_provider_url=parent.canonical_provider_url,
            media_id=parent.media_id,
            hostname=parent.hostname,
            path=parent.path,
            scheme=parent.scheme,
            port=parent.port,
            selected_format_snapshot=snapshot,
            now=now,
            ttl_seconds=self._settings.media_download_artifact_ttl_seconds,
            max_attempts=self._settings.media_download_max_attempts,
            parent_expires_at=parent.expires_at,
            suggested_filename=filename,
            job_id=job_id,
        )
        await session.flush()
        return self._to_view(job, created=created)

    async def get(
        self,
        *,
        download_job_id: uuid.UUID,
        access_token: str,
        session: AsyncSession,
    ) -> DownloadJobView:
        """Load download job; authorize via parent MediaJob credential hash."""
        credential_hash = hash_access_token(access_token)
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
        # Prefer an authorized expired view over making the job unobservable.
        # TTL may have elapsed before the worker hygiene pass marks the row.
        if (
            job.public_state != MediaDownloadJobState.EXPIRED.value
            and job.expires_at <= now
        ):
            return self._to_view(
                job,
                created=False,
                force_state=MediaDownloadJobState.EXPIRED,
                force_error_code=DownloadErrorCode.DOWNLOAD_EXPIRED.value,
            )
        return self._to_view(job, created=False)

    async def cancel(
        self,
        *,
        download_job_id: uuid.UUID,
        access_token: str,
        session: AsyncSession,
    ) -> DownloadJobView:
        """Idempotent cancel authorized via the parent MediaJob Bearer token."""
        if not self._settings.media_downloads_enabled:
            raise_download_error(
                DownloadErrorCode.DOWNLOADS_DISABLED,
                internal_reason="DOWNLOADS_DISABLED",
            )
        credential_hash = hash_access_token(access_token)
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
        updated = await repo.request_cancel(job_id=job.id, now=now)
        if updated is None:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND,
                internal_reason="MISSING",
            )
        await session.flush()
        return self._to_view(updated, created=False)

    @staticmethod
    def _public_snapshot(job: MediaDownloadJob) -> dict[str, Any]:
        """Re-encode the approved public schema; never echo stored JSONB.

        Persisted rows are treated as untrusted input: forged keys such as
        direct URLs or provider tokens cannot survive a strict decode/encode
        round trip, and malformed data fails closed as a catalog error.
        """
        try:
            fmt = decode_selected_format_snapshot(
                job.selected_format_snapshot,
                expected_format_option_id=job.format_option_id,
            )
            return dict(encode_selected_format_snapshot(fmt))
        except DownloadError:
            raise_download_error(
                DownloadErrorCode.INTERNAL_ERROR,
                internal_reason="PERSISTED_SNAPSHOT_INVALID",
            )

    def _to_view(
        self,
        job: MediaDownloadJob,
        *,
        created: bool,
        force_state: MediaDownloadJobState | None = None,
        force_error_code: str | None = None,
    ) -> DownloadJobView:
        try:
            state = force_state or project_public_state(
                job.public_state,
                job.progress_stage,
                job.cancel_requested_at,
            )
        except ValueError:
            raise_download_error(
                DownloadErrorCode.INTERNAL_ERROR,
                internal_reason="UNKNOWN_DOWNLOAD_STATE",
            )
        error_code: str | None = None
        if force_error_code is not None:
            error_code = force_error_code
        elif state is MediaDownloadJobState.FAILED:
            try:
                error_code = DownloadErrorCode(str(job.public_error_code)).value
            except ValueError:
                error_code = DownloadErrorCode.INTERNAL_ERROR.value
        elif state is MediaDownloadJobState.EXPIRED:
            error_code = DownloadErrorCode.DOWNLOAD_EXPIRED.value
        selected = self._public_snapshot(job)
        progress = str(job.progress_stage or "")
        try:
            stage = DownloadProgressStage(progress)
        except ValueError:
            stage = progress_for_public_state(state)
        if force_state is not None:
            stage = progress_for_public_state(force_state)
        cancellable = (
            state
            in {
                MediaDownloadJobState.QUEUED,
                MediaDownloadJobState.DOWNLOADING,
            }
            and force_state is None
        )
        next_attempt: datetime | None = None
        if (
            state is MediaDownloadJobState.QUEUED
            and stage is DownloadProgressStage.RETRYING
            and job.available_at is not None
            and force_state is None
        ):
            next_attempt = job.available_at
        # Never leak internal artifact locator — only a boolean readiness flag.
        return DownloadJobView(
            id=job.id,
            media_job_id=job.media_job_id,
            public_state=state.value,
            format_option_id=job.format_option_id,
            selected_format=selected,
            created_at=job.created_at,
            updated_at=job.updated_at,
            expires_at=job.expires_at,
            completed_at=job.completed_at,
            artifact_ready=state is MediaDownloadJobState.READY,
            error_code=error_code,
            progress_stage=stage.value,
            attempt_count=int(job.attempt_count),
            max_attempts=int(job.max_attempts),
            next_attempt_at=next_attempt,
            cancellable=cancellable,
            progress_percent=self._public_progress_percent(job, state, stage),
            artifact_bytes=self._public_artifact_bytes(job, state),
            suggested_filename=self._public_suggested_filename(job, selected),
            created=created,
        )

    @staticmethod
    def _public_progress_percent(
        job: MediaDownloadJob,
        state: MediaDownloadJobState,
        stage: DownloadProgressStage,
    ) -> int | None:
        raw = job.progress_percent
        if (
            state is not MediaDownloadJobState.DOWNLOADING
            or stage.value not in DOWNLOAD_PERCENT_STAGES
        ):
            if raw is not None:
                raise_download_error(
                    DownloadErrorCode.INTERNAL_ERROR,
                    internal_reason="PROGRESS_PERCENT_INVALID",
                )
            return None
        if raw is None:
            return None
        if type(raw) is bool or not isinstance(raw, int) or raw < 0 or raw > 99:
            raise_download_error(
                DownloadErrorCode.INTERNAL_ERROR,
                internal_reason="PROGRESS_PERCENT_INVALID",
            )
        return int(raw)

    @staticmethod
    def _public_artifact_bytes(
        job: MediaDownloadJob, state: MediaDownloadJobState
    ) -> int | None:
        raw = job.artifact_bytes
        if state is not MediaDownloadJobState.READY:
            if raw is not None:
                raise_download_error(
                    DownloadErrorCode.INTERNAL_ERROR,
                    internal_reason="ARTIFACT_BYTES_INVALID",
                )
            return None
        if type(raw) is bool or not isinstance(raw, int) or raw <= 0:
            raise_download_error(
                DownloadErrorCode.INTERNAL_ERROR,
                internal_reason="ARTIFACT_BYTES_INVALID",
            )
        return int(raw)

    @staticmethod
    def _public_suggested_filename(
        job: MediaDownloadJob, selected: dict[str, Any]
    ) -> str:
        container = str(selected.get("container") or job.artifact_container or "mp4")
        stored = job.suggested_filename
        try:
            if stored is None or stored == "":
                return fallback_filename(job.id, container)
            if not is_safe_suggested_filename(stored, container=container):
                raise_download_error(
                    DownloadErrorCode.INTERNAL_ERROR,
                    internal_reason="SUGGESTED_FILENAME_INVALID",
                )
            return stored
        except ValueError:
            raise_download_error(
                DownloadErrorCode.INTERNAL_ERROR,
                internal_reason="SUGGESTED_FILENAME_INVALID",
            )

    def to_public_dict(self, view: DownloadJobView) -> dict[str, Any]:
        """Serialize a DownloadJobView to camelCase API fields (no secrets).

        Cache-Control: no-store is the API layer's responsibility.
        """
        return {
            "id": str(view.id),
            "mediaJobId": str(view.media_job_id),
            "state": view.public_state,
            "formatOptionId": view.format_option_id,
            "selectedFormat": view.selected_format,
            "createdAt": view.created_at.isoformat(),
            "updatedAt": view.updated_at.isoformat(),
            "expiresAt": view.expires_at.isoformat(),
            "completedAt": (
                view.completed_at.isoformat() if view.completed_at else None
            ),
            "artifactReady": view.artifact_ready,
            "errorCode": view.error_code,
            "progressStage": view.progress_stage,
            "attempt": view.attempt_count,
            "maxAttempts": view.max_attempts,
            "nextAttemptAt": (
                view.next_attempt_at.isoformat() if view.next_attempt_at else None
            ),
            "cancellable": view.cancellable,
            "progressPercent": view.progress_percent,
            "artifactBytes": view.artifact_bytes,
            "suggestedFilename": view.suggested_filename,
        }
