"""Durable media-job worker with PR4 inspection and PR6 downloads."""

from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from dataclasses import dataclass
from types import FrameType
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fetchnow.core.config import Settings
from fetchnow.db.session import create_engine, create_session_factory
from fetchnow.downloads.executor import (
    DownloadClaimSnapshot,
    DownloadExecutor,
    snapshot_from_job,
)
from fetchnow.downloads.repository import MediaDownloadJobRepository
from fetchnow.jobs.errors import JobError, JobErrorCode
from fetchnow.jobs.metadata_codec import media_metadata_to_jsonable
from fetchnow.jobs.repository import MediaJobRepository
from fetchnow.jobs.target_rebuild import rebuild_resolution_result
from fetchnow.media_inspection.defaults import build_default_inspection_registry
from fetchnow.media_inspection.errors import InspectionError, InspectionErrorKind
from fetchnow.media_inspection.registry import InspectionExtractorRegistry
from fetchnow.media_inspection.service import MediaInspectionService
from fetchnow.network.client import SafeHTTPClient
from fetchnow.url.dns import DnsResolver, SystemDnsResolver
from fetchnow.url.providers import ProviderRegistry
from fetchnow.url.validate import URLValidator

logger = logging.getLogger("fetchnow.jobs.worker")

_RETRYABLE_KINDS = frozenset(
    {
        InspectionErrorKind.INSPECTION_TIMEOUT,
        InspectionErrorKind.INSPECTION_TOOL_UNAVAILABLE,
        InspectionErrorKind.INSPECTION_MEDIA_UNAVAILABLE,
        InspectionErrorKind.INTERNAL_INSPECTION_ERROR,
    }
)


def _retry_backoff_seconds(settings: Settings, attempt_count: int) -> float:
    """Exponential backoff bounded by configured min/max."""
    base = settings.media_job_retry_base_seconds
    maximum = settings.media_job_retry_max_seconds
    exp = max(0, attempt_count - 1)
    delay = base * (2**exp)
    return float(min(maximum, max(base, delay)))


class _LeaseLostError(Exception):
    """Internal control-flow signal; never exposed outside the worker."""


@dataclass(slots=True)
class _ClaimedSnapshot:
    job_id: uuid.UUID
    fence: int
    provider_id: str
    canonical_provider_url: str
    media_id: str
    hostname: str
    path: str
    scheme: str
    port: int | None
    attempt_count: int


@dataclass(slots=True)
class _ActiveWork:
    task: asyncio.Task[None]
    job_id: uuid.UUID
    fence: int
    kind: str


class MediaJobWorkerRunner:
    """Poll PostgreSQL for media/download jobs and run work under leases."""

    def __init__(
        self,
        settings: Settings,
        *,
        stop_event: asyncio.Event | None = None,
        engine: AsyncEngine | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        dns_resolver: DnsResolver | None = None,
        provider_registry: ProviderRegistry | None = None,
        validator: URLValidator | None = None,
        http_client: SafeHTTPClient | None = None,
        inspection_service: MediaInspectionService | None = None,
        inspection_registry: InspectionExtractorRegistry | None = None,
        download_executor: DownloadExecutor | None = None,
    ) -> None:
        self._settings = settings
        self._stop_event = stop_event or asyncio.Event()
        self._worker_id = secrets.token_urlsafe(24)
        self._owns_engine = engine is None
        self._engine = engine or create_engine(settings)
        self._session_factory = session_factory or create_session_factory(self._engine)
        self._dns = dns_resolver or SystemDnsResolver(
            timeout_seconds=settings.dns_resolution_timeout_seconds
        )
        self._providers = provider_registry or ProviderRegistry.from_settings(settings)
        self._validator = validator or URLValidator(
            settings, registry=self._providers, resolver=self._dns
        )
        self._owns_http = http_client is None
        self._http = http_client or SafeHTTPClient(
            settings, validator=self._validator, resolver=self._dns
        )
        if inspection_registry is not None:
            self._inspection_registry = inspection_registry
        else:
            self._inspection_registry = build_default_inspection_registry(
                settings, self._providers
            )
        if inspection_service is not None:
            self._inspection = inspection_service
        else:
            self._inspection = MediaInspectionService(
                settings=settings, registry=self._inspection_registry
            )
        self._owns_download_executor = download_executor is None
        self._download_executor = download_executor
        self._inspection_semaphore = asyncio.Semaphore(settings.worker_concurrency)
        self._download_semaphore = asyncio.Semaphore(
            settings.media_download_concurrency
        )
        self._active_inspection: dict[asyncio.Task[None], _ActiveWork] = {}
        self._active_download: dict[asyncio.Task[None], _ActiveWork] = {}
        self._closed = False

    def _ensure_download_executor(self) -> DownloadExecutor:
        if self._download_executor is None:
            self._download_executor = DownloadExecutor(
                self._settings,
                session_factory=self._session_factory,
                inspection_service=self._inspection,
                inspection_registry=self._inspection_registry,
                provider_registry=self._providers,
                validator=self._validator,
                worker_id=self._worker_id,
            )
        return self._download_executor

    @property
    def worker_id(self) -> str:
        return self._worker_id

    @property
    def stop_event(self) -> asyncio.Event:
        return self._stop_event

    def request_stop(self, signum: int | None = None) -> None:
        if signum is not None:
            logger.info("Received signal %s; initiating graceful shutdown", signum)
        self._stop_event.set()

    def install_signal_handlers(self) -> None:
        import signal

        def _handle(signum: int, _frame: FrameType | None) -> None:
            self.request_stop(signum)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _handle, sig, None)
            except NotImplementedError:
                signal.signal(sig, _handle)

    async def run(self) -> None:
        """Run poll loop until stop_event; close resources exactly once."""
        logger.info(
            "Media job worker started env=%s poll_interval=%s "
            "inspection_concurrency=%s download_concurrency=%s "
            "media_jobs_enabled=%s media_inspection_enabled=%s "
            "media_downloads_enabled=%s",
            self._settings.app_env,
            self._settings.worker_poll_interval_seconds,
            self._settings.worker_concurrency,
            self._settings.media_download_concurrency,
            self._settings.media_jobs_enabled,
            self._settings.media_inspection_enabled,
            self._settings.media_downloads_enabled,
        )
        try:
            while not self._stop_event.is_set():
                try:
                    await self._poll_once()
                except Exception:
                    logger.exception("Worker poll iteration failed")
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._settings.worker_poll_interval_seconds,
                    )
                except TimeoutError:
                    continue
            await self._shutdown_active()
        finally:
            await self.aclose()
            logger.info("Media job worker shut down cleanly")

    async def _poll_once(self) -> None:
        if self._settings.media_jobs_enabled:
            async with self._session_factory() as session:
                repo = MediaJobRepository(session)
                now = await repo.database_now()
                await repo.reclaim_expired_leases(now)
                await repo.expire_due_jobs(now)
                await session.commit()

        if self._settings.media_downloads_enabled:
            await self._hygiene_download_jobs()

        if (
            self._settings.media_jobs_enabled
            and self._settings.media_inspection_enabled
        ):
            await self._claim_inspection_jobs()

        if (
            self._settings.media_downloads_enabled
            and self._settings.media_inspection_enabled
        ):
            await self._claim_download_jobs()

    async def _hygiene_download_jobs(self) -> None:
        executor = self._ensure_download_executor()
        store = executor.artifact_store
        async with self._session_factory() as session:
            repo = MediaDownloadJobRepository(session)
            now = await repo.database_now()
            await repo.reclaim_expired_leases(now)
            expired_artifacts = await repo.expire_due_jobs(now)
            active_fences = await repo.list_active_fences()
            referenced_artifacts = await repo.list_referenced_artifact_ids()
            await session.commit()

        for artifact_id in expired_artifacts:
            try:
                store.delete_artifact(artifact_id)
            except Exception:
                logger.warning(
                    "download_artifact_delete_failed artifact_kind=expired"
                )

        # Protect in-memory claims that may not yet be visible as DOWNLOADING
        # in a concurrent reader's snapshot (same-process race is still covered).
        in_memory = frozenset(
            (work.job_id, work.fence) for work in self._active_download.values()
        )
        try:
            removed = store.reconcile(
                referenced_artifact_ids=referenced_artifacts,
                active_attempts=active_fences | in_memory,
                limit=64,
            )
            if removed:
                logger.info("download_orphan_sweep_removed count=%s", removed)
        except Exception:
            logger.exception("download_orphan_sweep_failed")

    async def _claim_inspection_jobs(self) -> None:
        free_slots = self._settings.worker_concurrency - len(self._active_inspection)
        if free_slots < 1:
            return

        async with self._session_factory() as session:
            repo = MediaJobRepository(session)
            now = await repo.database_now()
            claimed = await repo.claim_next(
                worker_id=self._worker_id,
                lease_seconds=self._settings.media_job_lease_seconds,
                now=now,
                limit=free_slots,
            )
            await session.commit()
            snapshots = [
                _ClaimedSnapshot(
                    job_id=job.id,
                    fence=int(job.fence_token),
                    provider_id=job.provider_id,
                    canonical_provider_url=job.canonical_provider_url,
                    media_id=job.media_id,
                    hostname=job.hostname,
                    path=job.path,
                    scheme=job.scheme,
                    port=job.port,
                    attempt_count=int(job.attempt_count),
                )
                for job in claimed
            ]

        for snap in snapshots:
            await self._inspection_semaphore.acquire()
            task = asyncio.create_task(
                self._run_claimed(snap),
                name=f"media-job-{snap.job_id}",
            )
            self._active_inspection[task] = _ActiveWork(
                task=task,
                job_id=snap.job_id,
                fence=snap.fence,
                kind="inspection",
            )
            task.add_done_callback(self._on_inspection_done)
            logger.info(
                "media_inspection_claimed job_id=%s fence=%s",
                snap.job_id,
                snap.fence,
            )

    async def _claim_download_jobs(self) -> None:
        free_slots = (
            self._settings.media_download_concurrency - len(self._active_download)
        )
        if free_slots < 1:
            return

        executor = self._ensure_download_executor()
        store = executor.artifact_store
        if hasattr(store, "check_free_space") and not store.check_free_space(
            self._settings.media_download_min_free_bytes
        ):
            logger.info(
                "download_claim_skipped reason=disk_headroom "
                "active_downloads=%s",
                len(self._active_download),
            )
            return

        async with self._session_factory() as session:
            repo = MediaDownloadJobRepository(session)
            now = await repo.database_now()
            claimed = await repo.claim_next(
                worker_id=self._worker_id,
                lease_seconds=self._settings.media_download_lease_seconds,
                now=now,
                limit=free_slots,
            )
            await session.commit()
            snapshots = [snapshot_from_job(job) for job in claimed]

        for snap in snapshots:
            await self._download_semaphore.acquire()
            task = asyncio.create_task(
                self._run_download_claimed(snap),
                name=f"media-download-{snap.job_id}",
            )
            self._active_download[task] = _ActiveWork(
                task=task,
                job_id=snap.job_id,
                fence=snap.fence,
                kind="download",
            )
            task.add_done_callback(self._on_download_done)
            logger.info(
                "media_download_claimed job_id=%s fence=%s",
                snap.job_id,
                snap.fence,
            )

    def _on_inspection_done(self, task: asyncio.Task[None]) -> None:
        work = self._active_inspection.pop(task, None)
        if work is None:
            return
        self._inspection_semaphore.release()
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "Media inspection task failed job_id=%s error_type=%s",
                work.job_id,
                type(exc).__name__,
            )

    def _on_download_done(self, task: asyncio.Task[None]) -> None:
        work = self._active_download.pop(task, None)
        if work is None:
            return
        self._download_semaphore.release()
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "Media download task failed job_id=%s error_type=%s",
                work.job_id,
                type(exc).__name__,
            )

    async def _run_claimed(self, snap: _ClaimedSnapshot) -> None:
        work_task: asyncio.Task[dict[str, Any]] | None = None
        heartbeat_task: asyncio.Task[bool] | None = None
        try:
            if self._stop_event.is_set():
                await self._release_cancel(snap.job_id, snap.fence)
                return

            work_task = asyncio.create_task(
                self._execute_inspection(snap),
                name=f"media-job-inspection-{snap.job_id}",
            )
            heartbeat_task = asyncio.create_task(
                self._lease_heartbeat(snap),
                name=f"media-job-lease-{snap.job_id}",
            )
            done, _ = await asyncio.wait(
                {work_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done and not heartbeat_task.result():
                work_task.cancel()
                await asyncio.gather(work_task, return_exceptions=True)
                raise _LeaseLostError
            payload = await work_task
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            async with self._session_factory() as session:
                repo = MediaJobRepository(session)
                now = await repo.database_now()
                applied = await repo.complete(
                    job_id=snap.job_id,
                    owner=self._worker_id,
                    fence=snap.fence,
                    metadata_json=payload,
                    now=now,
                )
                await session.commit()
            if not applied:
                logger.warning(
                    "Stale lease prevented completion job_id=%s",
                    snap.job_id,
                )
        except _LeaseLostError:
            logger.warning(
                "Lease lost; inspection result discarded job_id=%s", snap.job_id
            )
        except asyncio.CancelledError:
            for task in (work_task, heartbeat_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (work_task, heartbeat_task) if task is not None),
                return_exceptions=True,
            )
            await self._release_cancel(snap.job_id, snap.fence)
            raise
        except InspectionError as exc:
            if exc.kind == InspectionErrorKind.INSPECTION_CANCELLED:
                await self._release_cancel(snap.job_id, snap.fence)
                return
            await self._handle_inspection_failure(
                job_id=snap.job_id,
                fence=snap.fence,
                kind=exc.kind,
                attempt_count=snap.attempt_count,
            )
        except JobError:
            await self._fail_permanent(snap.job_id, snap.fence)
        except Exception:
            logger.exception(
                "Unexpected media job failure job_id=%s",
                snap.job_id,
            )
            await self._handle_inspection_failure(
                job_id=snap.job_id,
                fence=snap.fence,
                kind=InspectionErrorKind.INTERNAL_INSPECTION_ERROR,
                attempt_count=snap.attempt_count,
            )
        finally:
            for task in (work_task, heartbeat_task):
                if task is not None and not task.done():
                    task.cancel()
            if work_task is not None or heartbeat_task is not None:
                await asyncio.gather(
                    *(task for task in (work_task, heartbeat_task) if task is not None),
                    return_exceptions=True,
                )

    async def _run_download_claimed(self, snap: DownloadClaimSnapshot) -> None:
        work_task: asyncio.Task[None] | None = None
        heartbeat_task: asyncio.Task[bool] | None = None
        executor = self._ensure_download_executor()
        try:
            if self._stop_event.is_set():
                await self._release_download_cancel(snap.job_id, snap.fence)
                return

            work_task = asyncio.create_task(
                executor.run_claimed(snap),
                name=f"media-download-exec-{snap.job_id}",
            )
            heartbeat_task = asyncio.create_task(
                self._download_lease_heartbeat(snap),
                name=f"media-download-lease-{snap.job_id}",
            )
            done, _ = await asyncio.wait(
                {work_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done and not heartbeat_task.result():
                work_task.cancel()
                await asyncio.gather(work_task, return_exceptions=True)
                logger.warning(
                    "Lease lost; download result discarded job_id=%s",
                    snap.job_id,
                )
                return
            await work_task
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        except asyncio.CancelledError:
            for task in (work_task, heartbeat_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (work_task, heartbeat_task) if task is not None),
                return_exceptions=True,
            )
            raise
        except Exception:
            logger.exception(
                "Unexpected download worker failure job_id=%s",
                snap.job_id,
            )
        finally:
            for task in (work_task, heartbeat_task):
                if task is not None and not task.done():
                    task.cancel()
            if work_task is not None or heartbeat_task is not None:
                await asyncio.gather(
                    *(task for task in (work_task, heartbeat_task) if task is not None),
                    return_exceptions=True,
                )

    async def _execute_inspection(
        self, snap: _ClaimedSnapshot
    ) -> dict[str, Any]:
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
        metadata = await self._inspection.inspect(resolution)
        return media_metadata_to_jsonable(
            metadata,
            max_bytes=self._settings.media_job_result_max_bytes,
        )

    async def _lease_heartbeat(self, snap: _ClaimedSnapshot) -> bool:
        interval = max(1.0, self._settings.media_job_lease_seconds / 3.0)
        while True:
            await asyncio.sleep(interval)
            try:
                async with self._session_factory() as session:
                    repo = MediaJobRepository(session)
                    now = await repo.database_now()
                    renewed = await repo.renew_lease(
                        job_id=snap.job_id,
                        owner=self._worker_id,
                        fence=snap.fence,
                        lease_seconds=self._settings.media_job_lease_seconds,
                        now=now,
                    )
                    await session.commit()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Lease renewal failed job_id=%s", snap.job_id)
                return False
            if not renewed:
                return False

    async def _download_lease_heartbeat(self, snap: DownloadClaimSnapshot) -> bool:
        interval = max(1.0, self._settings.media_download_lease_seconds / 3.0)
        while True:
            await asyncio.sleep(interval)
            try:
                async with self._session_factory() as session:
                    repo = MediaDownloadJobRepository(session)
                    now = await repo.database_now()
                    renewed = await repo.renew_lease(
                        job_id=snap.job_id,
                        owner=self._worker_id,
                        fence=snap.fence,
                        lease_seconds=self._settings.media_download_lease_seconds,
                        now=now,
                    )
                    await session.commit()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Download lease renewal failed job_id=%s", snap.job_id
                )
                return False
            if not renewed:
                return False

    async def _handle_inspection_failure(
        self,
        *,
        job_id: uuid.UUID,
        fence: int,
        kind: InspectionErrorKind,
        attempt_count: int,
    ) -> None:
        public_code = JobErrorCode.MEDIA_INSPECTION_FAILED.value
        retryable = kind in _RETRYABLE_KINDS
        async with self._session_factory() as session:
            repo = MediaJobRepository(session)
            now = await repo.database_now()
            if retryable:
                backoff = _retry_backoff_seconds(self._settings, attempt_count)
                await repo.fail_retry(
                    job_id=job_id,
                    owner=self._worker_id,
                    fence=fence,
                    now=now,
                    backoff_seconds=backoff,
                    public_error_code=public_code,
                )
            else:
                await repo.fail_permanent(
                    job_id=job_id,
                    owner=self._worker_id,
                    fence=fence,
                    public_error_code=public_code,
                    now=now,
                )
            await session.commit()

    async def _fail_permanent(self, job_id: uuid.UUID, fence: int) -> None:
        async with self._session_factory() as session:
            repo = MediaJobRepository(session)
            now = await repo.database_now()
            await repo.fail_permanent(
                job_id=job_id,
                owner=self._worker_id,
                fence=fence,
                public_error_code=JobErrorCode.MEDIA_INSPECTION_FAILED.value,
                now=now,
            )
            await session.commit()

    async def _release_cancel(self, job_id: uuid.UUID, fence: int) -> None:
        try:
            async with self._session_factory() as session:
                repo = MediaJobRepository(session)
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
                "Failed to release cancelled job job_id=%s",
                job_id,
            )

    async def _release_download_cancel(
        self, job_id: uuid.UUID, fence: int
    ) -> None:
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

    async def _shutdown_active(self) -> None:
        grace = self._settings.media_job_shutdown_grace_seconds
        tasks = list(self._active_inspection.keys()) + list(
            self._active_download.keys()
        )
        if not tasks:
            return
        logger.info(
            "Cancelling %s active inspection and %s active download jobs "
            "(grace=%ss)",
            len(self._active_inspection),
            len(self._active_download),
            grace,
        )
        for task in tasks:
            task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=grace,
            )
        except TimeoutError:
            logger.warning("Shutdown grace elapsed with active jobs remaining")
            for task in tasks:
                if not task.done():
                    task.cancel()
            # Resource closure is forbidden while a job may still use the DB or
            # subprocess runner. Bounded cancellation/reaping is required.
            await asyncio.gather(*tasks, return_exceptions=True)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_download_executor and self._download_executor is not None:
            await self._download_executor.aclose()
        if self._owns_http:
            close = getattr(self._http, "aclose", None)
            if close is not None:
                result = close()
                if asyncio.iscoroutine(result):
                    await result
        if self._owns_engine:
            await self._engine.dispose()
