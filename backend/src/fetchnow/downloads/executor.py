"""Worker-side download attempt executor with fenced publication."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fetchnow.core.config import Settings
from fetchnow.downloads.artifacts import ArtifactStore, AttemptWorkspace
from fetchnow.downloads.errors import (
    DownloadError,
    DownloadErrorCode,
    raise_download_error,
)
from fetchnow.downloads.models import MediaDownloadJob
from fetchnow.downloads.process_download import (
    DownloadProcessRunner,
    assert_env_has_no_secrets,
    build_sanitized_env,
)
from fetchnow.downloads.repository import MediaDownloadJobRepository
from fetchnow.downloads.selection import (
    ResolvedDownloadSelection,
    resolve_selection_from_draft,
)
from fetchnow.downloads.snapshot_codec import decode_selected_format_snapshot
from fetchnow.downloads.ytdlp_download_argv import build_ytdlp_download_argv
from fetchnow.jobs.target_rebuild import rebuild_resolution_result
from fetchnow.media_inspection.errors import InspectionError
from fetchnow.media_inspection.registry import InspectionExtractorRegistry
from fetchnow.media_inspection.service import MediaInspectionService
from fetchnow.media_inspection.ytdlp_adapter import validate_ytdlp_executable
from fetchnow.url.providers import ProviderRegistry
from fetchnow.url.validate import URLValidator

logger = logging.getLogger("fetchnow.downloads.executor")

_OUTPUT_TEMPLATE = "output/artifact.%(ext)s"


@dataclass(frozen=True, slots=True)
class DownloadClaimSnapshot:
    """Lease-owned snapshot for one download attempt (no secrets)."""

    job_id: uuid.UUID
    media_job_id: uuid.UUID
    fence: int
    attempt_count: int
    format_option_id: str
    provider_id: str
    canonical_provider_url: str
    media_id: str
    hostname: str
    path: str
    scheme: str
    port: int | None
    selected_format_snapshot: dict[str, Any]
    expires_at: datetime


def snapshot_from_job(job: MediaDownloadJob) -> DownloadClaimSnapshot:
    """Build an immutable claim snapshot from a claimed ORM row."""
    return DownloadClaimSnapshot(
        job_id=job.id,
        media_job_id=job.media_job_id,
        fence=int(job.fence_token),
        attempt_count=int(job.attempt_count),
        format_option_id=job.format_option_id,
        provider_id=job.provider_id,
        canonical_provider_url=job.canonical_provider_url,
        media_id=job.media_id,
        hostname=job.hostname,
        path=job.path,
        scheme=job.scheme,
        port=job.port,
        selected_format_snapshot=dict(job.selected_format_snapshot or {}),
        expires_at=job.expires_at,
    )


class DownloadExecutor:
    """Execute one leased download attempt under fence ownership."""

    def __init__(
        self,
        settings: Settings,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        inspection_service: MediaInspectionService,
        inspection_registry: InspectionExtractorRegistry,
        provider_registry: ProviderRegistry | None = None,
        validator: URLValidator | None = None,
        artifact_store: ArtifactStore | None = None,
        process_runner: DownloadProcessRunner | None = None,
        worker_id: str,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._inspection = inspection_service
        self._inspection_registry = inspection_registry
        self._providers = provider_registry
        self._validator = validator
        self._runner = process_runner or DownloadProcessRunner()
        self._worker_id = worker_id
        self._closed = False
        if artifact_store is not None:
            self._store = artifact_store
        else:
            # Worker-only fail-closed construction: executable + managed root.
            self.validate_worker_execution_settings(settings)
            root = settings.media_download_temp_root.strip()
            if not root:
                root = os.path.join(settings.temp_storage_path, "downloads")
            self._store = ArtifactStore(
                root=root,
                max_bytes=settings.media_download_max_bytes,
                orphan_grace_seconds=settings.media_download_orphan_grace_seconds,
            )

    @staticmethod
    def validate_worker_execution_settings(settings: Settings) -> None:
        """Reject worker enablement without trusted yt-dlp path and artifact root."""
        try:
            validate_ytdlp_executable(settings.media_inspection_ytdlp_path)
        except InspectionError:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_TOOL_FAILED,
                internal_reason="EXECUTABLE_INVALID",
            )
        root = settings.media_download_temp_root.strip()
        if not root:
            root = os.path.join(settings.temp_storage_path, "downloads")
        ArtifactStore.validate_root(root)

    @property
    def artifact_store(self) -> ArtifactStore:
        return self._store

    async def run_claimed(self, snap: DownloadClaimSnapshot) -> None:
        """Public worker entry: execute one leased download attempt."""
        await self.execute(snap)

    async def aclose(self) -> None:
        """Release executor resources exactly once (filesystem store is sync)."""
        if self._closed:
            return
        self._closed = True

    async def lease_still_owned(self, *, job_id: uuid.UUID, fence: int) -> bool:
        """Renew/verify the lease; False means publication is forbidden."""
        async with self._session_factory() as session:
            repo = MediaDownloadJobRepository(session)
            now = await repo.database_now()
            renewed = await repo.renew_lease(
                job_id=job_id,
                owner=self._worker_id,
                fence=fence,
                lease_seconds=self._settings.media_download_lease_seconds,
                now=now,
            )
            await session.commit()
            return renewed

    async def execute(self, snap: DownloadClaimSnapshot) -> None:
        """Run inspect→select→download→publish→cleanup→ready under the claim fence."""
        workspace: AttemptWorkspace | None = None
        primary_error: BaseException | None = None
        published_orphan = False
        try:
            self._store.ensure_min_free(self._settings.media_download_min_free_bytes)
            if not await self.lease_still_owned(job_id=snap.job_id, fence=snap.fence):
                return

            selection = await self._resolve_selection(snap)
            if not await self.lease_still_owned(job_id=snap.job_id, fence=snap.fence):
                return

            workspace = self._store.create_attempt_workspace(
                job_id=snap.job_id,
                attempt=snap.attempt_count,
                fence=snap.fence,
            )
            await self._run_download(snap, selection, workspace)
            if not await self.lease_still_owned(job_id=snap.job_id, fence=snap.fence):
                return

            published = self._store.publish(
                workspace=workspace,
                expected_container=selection.container,
                job_id=snap.job_id,
                format_option_id=snap.format_option_id,
                expires_at=snap.expires_at,
            )
            published_orphan = True

            # Cleanup must succeed before ready commit; failure leaves the
            # published artifact for reconcile and fails closed.
            self._store.cleanup_attempt_workspace(workspace, require_gone=True)
            workspace = None

            if not await self.lease_still_owned(job_id=snap.job_id, fence=snap.fence):
                # Stale fence after publish: leave orphan, do not ready.
                logger.warning(
                    "Stale lease prevented ready commit job_id=%s",
                    snap.job_id,
                )
                return

            async with self._session_factory() as session:
                repo = MediaDownloadJobRepository(session)
                now = await repo.database_now()
                applied = await repo.complete_ready(
                    job_id=snap.job_id,
                    owner=self._worker_id,
                    fence=snap.fence,
                    artifact_id=published.artifact_id,
                    artifact_bytes=published.size_bytes,
                    artifact_content_type=published.content_type,
                    artifact_container=published.container,
                    now=now,
                )
                await session.commit()
            if applied:
                published_orphan = False
            else:
                logger.warning(
                    "Stale lease prevented ready commit job_id=%s",
                    snap.job_id,
                )
        except asyncio.CancelledError as exc:
            primary_error = exc
            await self._release_cancel(snap.job_id, snap.fence)
            raise
        except DownloadError as exc:
            primary_error = exc
            # Cleanup failure after publish: do not complete_ready (already skipped).
            if published_orphan and (
                exc.code is DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE
            ):
                logger.warning(
                    "download_cleanup_failed_after_publish job_id=%s",
                    snap.job_id,
                )
            await self._fail(snap, exc.code)
        except InspectionError:
            primary_error = DownloadError(
                DownloadErrorCode.FORMAT_UNAVAILABLE,
                internal_reason="INSPECTION_FAILED",
            )
            await self._fail(snap, DownloadErrorCode.FORMAT_UNAVAILABLE)
        except Exception:
            primary_error = DownloadError(
                DownloadErrorCode.INTERNAL_ERROR,
                internal_reason="UNEXPECTED",
            )
            logger.exception(
                "Unexpected download failure job_id=%s",
                snap.job_id,
            )
            await self._fail(snap, DownloadErrorCode.INTERNAL_ERROR)
        finally:
            if workspace is not None:
                try:
                    self._store.cleanup_attempt_workspace(
                        workspace, require_gone=False
                    )
                except Exception:
                    if primary_error is None:
                        logger.warning(
                            "download_workspace_cleanup_failed job_id=%s",
                            snap.job_id,
                        )
                    # Never replace CancelledError / primary domain error.

    async def _resolve_selection(
        self, snap: DownloadClaimSnapshot
    ) -> ResolvedDownloadSelection:
        resolution = await rebuild_resolution_result(
            provider_id=snap.provider_id,
            canonical_provider_url=snap.canonical_provider_url,
            media_id=snap.media_id,
            hostname=snap.hostname,
            path=snap.path,
            scheme=snap.scheme,
            port=snap.port,
            settings=self._settings,
            inspection_registry=self._inspection_registry,
            provider_registry=self._providers,
            validator=self._validator,
        )
        draft = await self._inspection.inspect_draft(resolution)
        if (
            draft.provider_id != snap.provider_id
            or draft.media_id != snap.media_id
            or draft.canonical_provider_url != snap.canonical_provider_url
        ):
            raise_download_error(
                DownloadErrorCode.FORMAT_UNAVAILABLE,
                internal_reason="IDENTITY_DRIFT",
            )
        persisted = decode_selected_format_snapshot(
            snap.selected_format_snapshot,
            expected_format_option_id=snap.format_option_id,
        )
        return resolve_selection_from_draft(
            draft,
            self._settings,
            snap.format_option_id,
            persisted,
        )

    async def _run_download(
        self,
        snap: DownloadClaimSnapshot,
        selection: ResolvedDownloadSelection,
        workspace: AttemptWorkspace,
    ) -> None:
        settings = self._settings
        try:
            executable = validate_ytdlp_executable(
                settings.media_inspection_ytdlp_path
            )
        except InspectionError:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_TOOL_FAILED,
                internal_reason="EXECUTABLE_INVALID",
            )
        binding = self._inspection_registry.resolve(
            provider_id=snap.provider_id, hostname=snap.hostname
        )

        # Token is an argv value only — never logged.
        argv = build_ytdlp_download_argv(
            executable=executable,
            url=snap.canonical_provider_url,
            provider_format_token=selection.provider_format_token,
            allowed_extractors=binding.allowed_extractor_keys,
            socket_timeout=int(settings.media_download_socket_timeout_seconds),
            cache_dir=str(workspace.cache),
            output_template=_OUTPUT_TEMPLATE,
            max_filesize_bytes=int(settings.media_download_max_bytes),
        )
        env = build_sanitized_env(
            home_dir=str(workspace.home), tmp_dir=str(workspace.path)
        )
        assert_env_has_no_secrets(env)

        logger.info(
            "media_download_start",
            extra={
                "download_job_id": str(snap.job_id),
                "provider_id": snap.provider_id,
                "format_option_id": snap.format_option_id,
            },
        )

        result = await self._runner.run(
            argv,
            env=env,
            cwd=str(workspace.path),
            timeout_seconds=settings.media_download_timeout_seconds,
            stdout_limit_bytes=settings.media_download_stdout_max_bytes,
            stderr_limit_bytes=settings.media_download_stderr_max_bytes,
            output_dir=str(workspace.output),
            max_output_bytes=settings.media_download_max_bytes,
            min_free_bytes=settings.media_download_min_free_bytes,
            root_for_disk=str(self._store.root),
        )
        if result.timed_out:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_TIMEOUT,
                internal_reason="PROCESS_TIMEOUT",
            )
        if result.signal is not None:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_TOOL_FAILED,
                internal_reason="PROCESS_SIGNAL",
            )
        if result.exit_code != 0:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_TOOL_FAILED,
                internal_reason="PROCESS_NONZERO",
            )

    async def _fail(
        self, snap: DownloadClaimSnapshot, code: DownloadErrorCode
    ) -> None:
        retryable = code in {
            DownloadErrorCode.DOWNLOAD_TIMEOUT,
            DownloadErrorCode.DOWNLOAD_TOOL_FAILED,
            DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
            DownloadErrorCode.INTERNAL_ERROR,
        }
        async with self._session_factory() as session:
            repo = MediaDownloadJobRepository(session)
            now = await repo.database_now()
            if retryable:
                backoff = _retry_backoff_seconds(
                    self._settings, snap.attempt_count
                )
                await repo.fail_retry(
                    job_id=snap.job_id,
                    owner=self._worker_id,
                    fence=snap.fence,
                    now=now,
                    backoff_seconds=backoff,
                    public_error_code=code.value,
                )
            else:
                await repo.fail_permanent(
                    job_id=snap.job_id,
                    owner=self._worker_id,
                    fence=snap.fence,
                    public_error_code=code.value,
                    now=now,
                )
            await session.commit()

    async def _release_cancel(self, job_id: uuid.UUID, fence: int) -> None:
        try:
            async with self._session_factory() as session:
                repo = MediaDownloadJobRepository(session)
                now = await repo.database_now()
                await repo.release_to_queued(
                    job_id=job_id,
                    owner=self._worker_id,
                    fence=fence,
                    now=now,
                )
                await session.commit()
        except Exception:
            logger.exception(
                "Failed to release cancelled download job_id=%s",
                job_id,
            )


def _retry_backoff_seconds(settings: Settings, attempt_count: int) -> float:
    base = settings.media_download_retry_base_seconds
    maximum = settings.media_download_retry_max_seconds
    exp = max(0, attempt_count - 1)
    delay = base * (2**exp)
    return float(min(maximum, max(base, delay)))
