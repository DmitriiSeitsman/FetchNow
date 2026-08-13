"""Worker-side download attempt executor with fenced publication."""

from __future__ import annotations

import asyncio
import logging
import math
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
from fetchnow.downloads.ffmpeg_argv import build_ffmpeg_mux_argv
from fetchnow.downloads.ffprobe_argv import build_ffprobe_argv
from fetchnow.downloads.ffprobe_parse import parse_ffprobe_json
from fetchnow.downloads.models import MediaDownloadJob
from fetchnow.downloads.process_download import (
    DownloadProcessRunner,
    assert_env_has_no_secrets,
    build_sanitized_env,
)
from fetchnow.downloads.repository import MediaDownloadJobRepository
from fetchnow.downloads.selection import (
    DownloadSelection,
    MuxedDownloadSelection,
    ResolvedDownloadSelection,
    resolve_selection_from_draft,
)
from fetchnow.downloads.snapshot_codec import decode_selected_format_snapshot
from fetchnow.downloads.tool_executable import validate_trusted_executable
from fetchnow.downloads.ytdlp_download_argv import build_ytdlp_download_argv
from fetchnow.jobs.target_rebuild import rebuild_resolution_result
from fetchnow.media_inspection.errors import InspectionError
from fetchnow.media_inspection.protocols import ProcessResult
from fetchnow.media_inspection.registry import InspectionExtractorRegistry
from fetchnow.media_inspection.service import MediaInspectionService
from fetchnow.media_inspection.ytdlp_adapter import validate_ytdlp_executable
from fetchnow.url.providers import ProviderRegistry
from fetchnow.url.validate import URLValidator

logger = logging.getLogger("fetchnow.downloads.executor")

_OUTPUT_TEMPLATE = "output/artifact.%(ext)s"


def lease_watch_interval_seconds(lease_seconds: float) -> float:
    """Bounded renew interval strictly below lease expiry."""
    lease = float(lease_seconds)
    if not math.isfinite(lease) or lease <= 0:
        return 0.05
    interval = min(max(lease / 4.0, 0.05), 15.0)
    if interval >= lease:
        interval = lease / 2.0
    return float(interval)


class _LeaseLostError(Exception):
    """Internal: lease/fence no longer owned. Never published as a tool error."""


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
        self._lease_lost = False
        self._watchdog_tasks: set[asyncio.Task[None]] = set()
        self._watchdog_started = 0
        self._watchdog_finished = 0
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
        if settings.media_muxing_enabled:
            validate_trusted_executable(
                settings.media_muxing_ffmpeg_path, kind="ffmpeg"
            )
            validate_trusted_executable(
                settings.media_muxing_ffprobe_path, kind="ffprobe"
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
        self._lease_lost = False
        try:
            self._store.ensure_min_free(self._settings.media_download_min_free_bytes)
            if not await self.lease_still_owned(job_id=snap.job_id, fence=snap.fence):
                return

            selection = await self._resolve_selection(snap)
            peak = _peak_reservation_bytes(
                selection, self._settings.media_download_max_bytes
            )
            self._store.ensure_min_free(
                self._settings.media_download_min_free_bytes + peak
            )
            if not await self.lease_still_owned(job_id=snap.job_id, fence=snap.fence):
                return

            workspace = self._store.create_attempt_workspace(
                job_id=snap.job_id,
                attempt=snap.attempt_count,
                fence=snap.fence,
            )
            await self._run_download(snap, selection, workspace, peak_bytes=peak)
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
        except _LeaseLostError:
            return
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
                    self._store.cleanup_attempt_workspace(workspace, require_gone=False)
                except Exception:
                    if primary_error is None:
                        logger.warning(
                            "download_workspace_cleanup_failed job_id=%s",
                            snap.job_id,
                        )
                    # Never replace CancelledError / primary domain error.

    async def _resolve_selection(
        self, snap: DownloadClaimSnapshot
    ) -> DownloadSelection:
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
        selection: DownloadSelection,
        workspace: AttemptWorkspace,
        *,
        peak_bytes: int,
    ) -> None:
        if isinstance(selection, MuxedDownloadSelection):
            await self._run_muxed_download(
                snap, selection, workspace, peak_bytes=peak_bytes
            )
            return
        await self._run_direct_download(snap, selection, workspace)

    async def _run_direct_download(
        self,
        snap: DownloadClaimSnapshot,
        selection: ResolvedDownloadSelection,
        workspace: AttemptWorkspace,
    ) -> None:
        settings = self._settings
        try:
            executable = validate_ytdlp_executable(settings.media_inspection_ytdlp_path)
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

        result = await self._run_supervised(
            snap,
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

    async def _run_muxed_download(
        self,
        snap: DownloadClaimSnapshot,
        selection: MuxedDownloadSelection,
        workspace: AttemptWorkspace,
        *,
        peak_bytes: int,
    ) -> None:
        settings = self._settings
        if not settings.media_muxing_enabled:
            raise_download_error(
                DownloadErrorCode.MUXING_UNAVAILABLE,
                internal_reason="MUXING_DISABLED",
            )
        ffmpeg = validate_trusted_executable(
            settings.media_muxing_ffmpeg_path, kind="ffmpeg"
        )
        ffprobe = validate_trusted_executable(
            settings.media_muxing_ffprobe_path, kind="ffprobe"
        )
        logger.info(
            "media_mux_start",
            extra={
                "download_job_id": str(snap.job_id),
                "provider_id": snap.provider_id,
                "format_option_id": snap.format_option_id,
            },
        )
        video_cap, audio_cap, output_cap = _mux_stage_byte_caps(
            selection, settings.media_download_max_bytes
        )
        if not await self.lease_still_owned(job_id=snap.job_id, fence=snap.fence):
            raise _LeaseLostError()
        await self._run_exact_ytdlp(
            snap,
            token=selection.video_format_token,
            workspace=workspace,
            output_template="video/stream.%(ext)s",
            output_dir=str(workspace.video),
            max_bytes=video_cap,
            min_free_headroom=max(0, peak_bytes - video_cap),
        )
        video_path = self._store.find_single_regular_file_in(
            workspace.video,
            allowed_suffixes=frozenset({"mp4", "webm"}),
        )
        consumed = video_path.stat().st_size
        self._ensure_remaining_disk(peak_bytes=peak_bytes, consumed_bytes=consumed)
        if not await self.lease_still_owned(job_id=snap.job_id, fence=snap.fence):
            raise _LeaseLostError()
        await self._run_exact_ytdlp(
            snap,
            token=selection.audio_format_token,
            workspace=workspace,
            output_template="audio/stream.%(ext)s",
            output_dir=str(workspace.audio),
            max_bytes=audio_cap,
            min_free_headroom=max(0, peak_bytes - consumed - audio_cap),
        )
        audio_path = self._store.find_single_regular_file_in(
            workspace.audio,
            allowed_suffixes=frozenset({"m4a", "mp4", "webm", "ogg", "opus"}),
        )
        consumed += audio_path.stat().st_size
        self._ensure_remaining_disk(peak_bytes=peak_bytes, consumed_bytes=consumed)
        if not await self.lease_still_owned(job_id=snap.job_id, fence=snap.fence):
            raise _LeaseLostError()
        mux_path = workspace.mux / f"artifact.{selection.container}"
        try:
            argv = build_ffmpeg_mux_argv(
                executable=ffmpeg,
                video_path=str(video_path),
                audio_path=str(audio_path),
                output_path=str(mux_path),
                output_container=selection.container,
            )
        except ValueError:
            error = DownloadError(
                DownloadErrorCode.MUXING_FAILED,
                internal_reason="ARGV_INVALID",
            )
            raise error from None
        env = build_sanitized_env(
            home_dir=str(workspace.home), tmp_dir=str(workspace.path)
        )
        assert_env_has_no_secrets(env)
        mux_result = await self._run_supervised(
            snap,
            argv,
            env=env,
            cwd=str(workspace.path),
            timeout_seconds=settings.media_muxing_timeout_seconds,
            stdout_limit_bytes=settings.media_muxing_max_stdout_bytes,
            stderr_limit_bytes=settings.media_muxing_max_stderr_bytes,
            output_dir=str(workspace.mux),
            extra_size_dirs=(str(workspace.video), str(workspace.audio)),
            max_output_bytes=peak_bytes,
            min_free_bytes=(
                settings.media_download_min_free_bytes
                + max(0, peak_bytes - consumed)
            ),
            root_for_disk=str(self._store.root),
            overflow_code=DownloadErrorCode.MUXING_FAILED,
            too_large_code=DownloadErrorCode.DOWNLOAD_TOO_LARGE,
            storage_code=DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
            spawn_code=DownloadErrorCode.MUXING_FAILED,
        )
        if mux_result.timed_out:
            raise_download_error(
                DownloadErrorCode.MUXING_TIMEOUT,
                internal_reason="PROCESS_TIMEOUT",
            )
        if mux_result.signal is not None or mux_result.exit_code != 0:
            raise_download_error(
                DownloadErrorCode.MUXING_FAILED,
                internal_reason="PROCESS_NONZERO",
            )
        mux_file = self._store.find_single_regular_file_in(
            workspace.mux,
            allowed_suffixes=frozenset({selection.container}),
            error_code=DownloadErrorCode.MUXED_OUTPUT_INVALID,
        )
        if not await self.lease_still_owned(job_id=snap.job_id, fence=snap.fence):
            raise _LeaseLostError()
        try:
            probe_argv = build_ffprobe_argv(
                executable=ffprobe, input_path=str(mux_file)
            )
        except ValueError:
            error = DownloadError(
                DownloadErrorCode.MUXED_OUTPUT_INVALID,
                internal_reason="PROBE_ARGV_INVALID",
            )
            raise error from None
        probe = await self._run_supervised(
            snap,
            probe_argv,
            env=env,
            cwd=str(workspace.path),
            timeout_seconds=settings.media_muxing_timeout_seconds,
            stdout_limit_bytes=settings.media_muxing_max_stdout_bytes,
            stderr_limit_bytes=settings.media_muxing_max_stderr_bytes,
            capture_stdout=True,
            overflow_code=DownloadErrorCode.MUXED_OUTPUT_INVALID,
            spawn_code=DownloadErrorCode.MUXING_FAILED,
        )
        if probe.timed_out:
            raise_download_error(
                DownloadErrorCode.MUXING_TIMEOUT,
                internal_reason="PROBE_TIMEOUT",
            )
        if probe.signal is not None or probe.exit_code != 0:
            raise_download_error(
                DownloadErrorCode.MUXED_OUTPUT_INVALID,
                internal_reason="PROBE_NONZERO",
            )
        raw = probe.stdout
        try:
            parse_ffprobe_json(
                raw,
                expected_container=selection.container,
                expected_size_bytes=mux_file.stat().st_size,
                expected_duration_seconds=selection.expected_duration_seconds,
                duration_tolerance_seconds=settings.media_muxing_duration_tolerance_seconds,
                max_bytes=settings.media_download_max_bytes,
                max_json_bytes=settings.media_muxing_max_stdout_bytes,
            )
        finally:
            del raw
        if not await self.lease_still_owned(job_id=snap.job_id, fence=snap.fence):
            raise _LeaseLostError()
        self._store.stage_muxed_output(
            workspace, mux_file, container=selection.container
        )

    def _ensure_remaining_disk(self, *, peak_bytes: int, consumed_bytes: int) -> None:
        remaining = max(0, int(peak_bytes) - max(0, int(consumed_bytes)))
        self._store.ensure_min_free(
            self._settings.media_download_min_free_bytes + remaining
        )

    async def _run_exact_ytdlp(
        self,
        snap: DownloadClaimSnapshot,
        *,
        token: str,
        workspace: AttemptWorkspace,
        output_template: str,
        output_dir: str,
        max_bytes: int | None = None,
        min_free_headroom: int = 0,
    ) -> None:
        settings = self._settings
        cap = int(settings.media_download_max_bytes if max_bytes is None else max_bytes)
        if cap < 1:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_TOO_LARGE,
                internal_reason="STAGE_CAP_INVALID",
            )
        try:
            executable = validate_ytdlp_executable(settings.media_inspection_ytdlp_path)
        except InspectionError:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_TOOL_FAILED,
                internal_reason="EXECUTABLE_INVALID",
            )
        binding = self._inspection_registry.resolve(
            provider_id=snap.provider_id, hostname=snap.hostname
        )
        argv = build_ytdlp_download_argv(
            executable=executable,
            url=snap.canonical_provider_url,
            provider_format_token=token,
            allowed_extractors=binding.allowed_extractor_keys,
            socket_timeout=int(settings.media_download_socket_timeout_seconds),
            cache_dir=str(workspace.cache),
            output_template=output_template,
            max_filesize_bytes=cap,
        )
        env = build_sanitized_env(
            home_dir=str(workspace.home), tmp_dir=str(workspace.path)
        )
        assert_env_has_no_secrets(env)
        result = await self._run_supervised(
            snap,
            argv,
            env=env,
            cwd=str(workspace.path),
            timeout_seconds=settings.media_download_timeout_seconds,
            stdout_limit_bytes=settings.media_download_stdout_max_bytes,
            stderr_limit_bytes=settings.media_download_stderr_max_bytes,
            output_dir=output_dir,
            max_output_bytes=cap,
            min_free_bytes=(
                settings.media_download_min_free_bytes + max(0, int(min_free_headroom))
            ),
            root_for_disk=str(self._store.root),
        )
        if result.timed_out:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_TIMEOUT,
                internal_reason="PROCESS_TIMEOUT",
            )
        if result.signal is not None or result.exit_code != 0:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_TOOL_FAILED,
                internal_reason="PROCESS_NONZERO",
            )

    async def _lease_watch_wait(self) -> None:
        await asyncio.sleep(
            lease_watch_interval_seconds(self._settings.media_download_lease_seconds)
        )

    async def _watch_lease_during_run(
        self,
        snap: DownloadClaimSnapshot,
        run_task: asyncio.Task[Any],
        started: asyncio.Event,
    ) -> None:
        self._watchdog_started += 1
        try:
            start_wait = asyncio.create_task(started.wait())
            try:
                await asyncio.wait(
                    {run_task, start_wait},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                if not start_wait.done():
                    start_wait.cancel()
                    await asyncio.gather(start_wait, return_exceptions=True)
            if run_task.done():
                return
            while not run_task.done():
                await self._lease_watch_wait()
                if run_task.done():
                    return
                try:
                    owned = await self.lease_still_owned(
                        job_id=snap.job_id, fence=snap.fence
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("download_lease_watch_failed")
                    self._lease_lost = True
                    run_task.cancel()
                    return
                if not owned:
                    self._lease_lost = True
                    run_task.cancel()
                    return
        finally:
            self._watchdog_finished += 1

    async def _run_supervised(
        self,
        snap: DownloadClaimSnapshot,
        argv: list[str],
        **kwargs: Any,
    ) -> ProcessResult:
        started = kwargs.get("started")
        if not isinstance(started, asyncio.Event):
            started = asyncio.Event()
            kwargs["started"] = started
        run_task = asyncio.create_task(self._runner.run(argv, **kwargs))
        watchdog = asyncio.create_task(
            self._watch_lease_during_run(snap, run_task, started)
        )
        self._watchdog_tasks.add(watchdog)
        try:
            try:
                return await run_task
            except asyncio.CancelledError:
                if not run_task.done():
                    run_task.cancel()
                    await asyncio.gather(run_task, return_exceptions=True)
                current = asyncio.current_task()
                outer_cancelling = (
                    current is not None and current.cancelling() > 0
                )
                if self._lease_lost and not outer_cancelling:
                    raise _LeaseLostError() from None
                raise
        finally:
            if not watchdog.done():
                watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)
            self._watchdog_tasks.discard(watchdog)

    async def _fail(self, snap: DownloadClaimSnapshot, code: DownloadErrorCode) -> None:
        retryable = code in {
            DownloadErrorCode.DOWNLOAD_TIMEOUT,
            DownloadErrorCode.DOWNLOAD_TOOL_FAILED,
            DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
            DownloadErrorCode.INTERNAL_ERROR,
            DownloadErrorCode.MUXING_TIMEOUT,
            DownloadErrorCode.MUXING_FAILED,
        }
        async with self._session_factory() as session:
            repo = MediaDownloadJobRepository(session)
            now = await repo.database_now()
            if retryable:
                backoff = _retry_backoff_seconds(self._settings, snap.attempt_count)
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


def _stage_cap(approx: int | None, max_bytes: int) -> int:
    if approx is None or type(approx) is bool or approx < 1:
        return int(max_bytes)
    return int(min(max_bytes, approx))


def _mux_stage_byte_caps(
    selection: MuxedDownloadSelection, max_bytes: int
) -> tuple[int, int, int]:
    """Per-stage write caps; unknown/empty/invalid approx fails closed to max_bytes."""
    video_cap = _stage_cap(selection.video_approx_bytes, max_bytes)
    audio_cap = _stage_cap(selection.audio_approx_bytes, max_bytes)
    output_cap = min(max_bytes, video_cap + audio_cap)
    return int(video_cap), int(audio_cap), int(output_cap)


def _peak_reservation_bytes(selection: DownloadSelection, max_bytes: int) -> int:
    """Peak workspace bytes: inputs + muxed output, never final-only."""
    if isinstance(selection, MuxedDownloadSelection):
        video_cap, audio_cap, output_cap = _mux_stage_byte_caps(selection, max_bytes)
        return int(video_cap + audio_cap + output_cap)
    approx = selection.approx_bytes or max_bytes
    return int(approx)


def _retry_backoff_seconds(settings: Settings, attempt_count: int) -> float:
    base = settings.media_download_retry_base_seconds
    maximum = settings.media_download_retry_max_seconds
    exp = max(0, attempt_count - 1)
    delay = base * (2**exp)
    return float(min(maximum, max(base, delay)))
