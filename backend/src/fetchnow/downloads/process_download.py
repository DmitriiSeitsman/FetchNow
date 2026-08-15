"""Hardened subprocess runner for download tools (shell=False)."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

from fetchnow.downloads.artifacts import ArtifactStore
from fetchnow.downloads.diagnostics import (
    FailureClass,
    classify_tool_failure,
    process_exit_category,
)
from fetchnow.downloads.errors import DownloadErrorCode, raise_download_error
from fetchnow.media_inspection.errors import InspectionError
from fetchnow.media_inspection.process import (
    _terminate_process_group,
    assert_env_has_no_secrets,
    build_sanitized_env,
)
from fetchnow.media_inspection.protocols import ProcessResult


async def _terminate_download_group(
    proc: asyncio.subprocess.Process,
    *,
    pgid: int | None,
) -> None:
    """Terminate the process group; map inspection reap failures to download errors."""
    try:
        await _terminate_process_group(proc, pgid=pgid)
    except InspectionError:
        raise_download_error(
            DownloadErrorCode.INTERNAL_ERROR,
            internal_reason="PROCESS_REAP_FAILED",
        )


# Re-export sanitized env helpers for download adapters.
__all__ = [
    "CoalescingProgressWriter",
    "DownloadProcessRunner",
    "assert_env_has_no_secrets",
    "build_sanitized_env",
]


class CoalescingProgressWriter:
    """At most one persist task; new samples replace the pending observed value.

    The size/disk monitor must never await this writer. On success, callers must
    ``observe`` a final sample and ``await flush()`` before ``aclose()`` so the
    last observation is not cancelled. Shutdown ``aclose`` cancels any remaining
    in-flight task so it cannot outlive ``run()``.
    """

    def __init__(self, persist: Callable[[int], Awaitable[None]]) -> None:
        self._persist = persist
        self._latest: int | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self.spawn_count = 0
        self.observe_count = 0

    def observe(self, observed_bytes: int) -> None:
        """Record the latest observed size without waiting for persistence."""
        if self._closed:
            return
        if type(observed_bytes) is bool or not isinstance(observed_bytes, int):
            return
        value = max(0, observed_bytes)
        self.observe_count += 1
        if self._latest is None:
            self._latest = value
        else:
            self._latest = max(self._latest, value)
        if self._task is None or self._task.done():
            self.spawn_count += 1
            self._task = asyncio.create_task(
                self._drain(), name="download-progress-writer"
            )

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    async def flush(self) -> None:
        """Await pending persist without closing. Safe to call after final observe."""
        if self._closed:
            return
        if self._latest is not None and (self._task is None or self._task.done()):
            self.spawn_count += 1
            self._task = asyncio.create_task(
                self._drain(), name="download-progress-writer"
            )
        task = self._task
        if task is not None and not task.done():
            await task

    async def aclose(self) -> None:
        self._closed = True
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _drain(self) -> None:
        try:
            while self._latest is not None and not self._closed:
                value = self._latest
                self._latest = None
                try:
                    await self._persist(value)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Persistence failures must not disable size/runtime checks.
                    # Do not log callback details; they may contain paths.
                    return
        except asyncio.CancelledError:
            raise


async def _read_bounded(
    stream: asyncio.StreamReader | None,
    *,
    limit: int,
    which: str,
    overflow_code: DownloadErrorCode = DownloadErrorCode.DOWNLOAD_TOOL_FAILED,
) -> bytes:
    if stream is None:
        return b""
    chunks: list[bytes] = []
    total = 0
    while True:
        block = await stream.read(64 * 1024)
        if not block:
            break
        total += len(block)
        if total > limit:
            raise_download_error(
                overflow_code,
                internal_reason=f"{which.upper()}_LIMIT",
            )
        chunks.append(block)
    return b"".join(chunks)


class DownloadProcessRunner:
    """Unix process-group runner that never returns tool stdout/stderr bytes.

    Callers receive empty stdout/stderr so public error/log paths cannot leak
    tool output. Internal readers still enforce byte ceilings. Optional output
    tree / free-disk monitors kill the process group when budgets are exceeded.
    Progress persistence is coalesced off the safety-monitor loop.
    """

    progress_writer_spawns: int = 0

    async def run(
        self,
        argv: list[str],
        *,
        env: dict[str, str],
        cwd: str,
        timeout_seconds: float,
        stdout_limit_bytes: int,
        stderr_limit_bytes: int,
        output_dir: str | None = None,
        max_output_bytes: int | None = None,
        min_free_bytes: int | None = None,
        root_for_disk: str | None = None,
        extra_size_dirs: tuple[str, ...] = (),
        capture_stdout: bool = False,
        overflow_code: DownloadErrorCode = DownloadErrorCode.DOWNLOAD_TOOL_FAILED,
        too_large_code: DownloadErrorCode = DownloadErrorCode.DOWNLOAD_TOO_LARGE,
        storage_code: DownloadErrorCode = (
            DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE
        ),
        spawn_code: DownloadErrorCode = DownloadErrorCode.DOWNLOAD_TOOL_FAILED,
        started: asyncio.Event | None = None,
        on_observed_bytes: Callable[[int], Awaitable[None]] | None = None,
    ) -> ProcessResult:
        if not argv:
            raise_download_error(
                DownloadErrorCode.INTERNAL_ERROR,
                internal_reason="EMPTY_ARGV",
            )
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd,
                start_new_session=True,
            )
        except OSError:
            raise_download_error(
                spawn_code,
                internal_reason="PROCESS_SPAWN_FAILED",
            )

        pgid = proc.pid
        if started is not None:
            started.set()
        stdout_task: asyncio.Task[bytes] | None = None
        stderr_task: asyncio.Task[bytes] | None = None
        monitor_task: asyncio.Task[None] | None = None
        progress_writer = (
            CoalescingProgressWriter(on_observed_bytes)
            if on_observed_bytes is not None
            else None
        )
        self.progress_writer_spawns = 0
        failure_path = False
        limit_code: DownloadErrorCode | None = None
        captured_stdout = b""
        stdout_count = 0
        stderr_count = 0
        classified_failure = None
        watching = (
            (output_dir is not None or extra_size_dirs)
            and max_output_bytes is not None
            and max_output_bytes > 0
        ) or (
            min_free_bytes is not None
            and min_free_bytes > 0
            and root_for_disk is not None
        )
        size_roots = tuple(
            Path(p)
            for p in ((output_dir,) if output_dir is not None else ()) + extra_size_dirs
        )

        async def _communicate() -> None:
            nonlocal stdout_task, stderr_task, captured_stdout
            nonlocal stdout_count, stderr_count, classified_failure
            assert proc.stdout is not None
            assert proc.stderr is not None
            stdout_task = asyncio.create_task(
                _read_bounded(
                    proc.stdout,
                    limit=stdout_limit_bytes,
                    which="stdout",
                    overflow_code=overflow_code,
                )
            )
            stderr_task = asyncio.create_task(
                _read_bounded(
                    proc.stderr,
                    limit=stderr_limit_bytes,
                    which="stderr",
                    overflow_code=overflow_code,
                )
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.gather(
                    stdout_task, stderr_task
                )
                stdout_count = len(stdout_bytes)
                stderr_count = len(stderr_bytes)
                classified_failure = classify_tool_failure(
                    timed_out=False,
                    signal=None,
                    cancelled=False,
                    stderr=stderr_bytes,
                )
                if capture_stdout:
                    captured_stdout = stdout_bytes
                del stderr_bytes
            except Exception:
                stdout_task.cancel()
                stderr_task.cancel()
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
                raise
            await proc.wait()

        def _watched_bytes() -> int:
            return sum(ArtifactStore.output_tree_byte_size(path) for path in size_roots)

        async def _size_monitor() -> None:
            nonlocal limit_code
            while True:
                await asyncio.sleep(0.2)
                watched = _watched_bytes() if size_roots else 0
                if (
                    size_roots
                    and max_output_bytes is not None
                    and max_output_bytes > 0
                    and watched > max_output_bytes
                ):
                    limit_code = too_large_code
                    await _terminate_download_group(proc, pgid=pgid)
                    return
                if (
                    min_free_bytes is not None
                    and min_free_bytes > 0
                    and root_for_disk is not None
                ):
                    try:
                        free = int(shutil.disk_usage(root_for_disk).free)
                    except OSError:
                        limit_code = storage_code
                        await _terminate_download_group(proc, pgid=pgid)
                        return
                    if free < min_free_bytes:
                        limit_code = storage_code
                        await _terminate_download_group(proc, pgid=pgid)
                        return
                if progress_writer is not None and size_roots:
                    progress_writer.observe(watched)

        async def _cancel_aux() -> None:
            tasks = [
                t
                for t in (stdout_task, stderr_task, monitor_task)
                if t is not None and not t.done()
            ]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if progress_writer is not None:
                await progress_writer.aclose()
                self.progress_writer_spawns = progress_writer.spawn_count

        timed_out = False
        try:
            if watching:
                monitor_task = asyncio.create_task(_size_monitor())
            try:
                await asyncio.wait_for(_communicate(), timeout=timeout_seconds)
            except TimeoutError:
                failure_path = True
                timed_out = True
                await _cancel_aux()
                await _terminate_download_group(proc, pgid=pgid)
            except asyncio.CancelledError:
                failure_path = True
                await _cancel_aux()
                await _terminate_download_group(proc, pgid=pgid)
                raise
            except Exception:
                failure_path = True
                await _cancel_aux()
                await _terminate_download_group(proc, pgid=pgid)
                raise
            else:
                # Final budget check after the tool exits (monitor samples ~0.2s).
                final_watched = _watched_bytes() if size_roots else 0
                if (
                    size_roots
                    and max_output_bytes is not None
                    and max_output_bytes > 0
                    and final_watched > max_output_bytes
                ):
                    limit_code = too_large_code
                if (
                    min_free_bytes is not None
                    and min_free_bytes > 0
                    and root_for_disk is not None
                ):
                    try:
                        free = int(shutil.disk_usage(root_for_disk).free)
                    except OSError:
                        limit_code = storage_code
                    else:
                        if free < min_free_bytes:
                            limit_code = storage_code
                if limit_code is not None:
                    failure_path = True
                    await _cancel_aux()
                    await _terminate_download_group(proc, pgid=pgid)
                elif progress_writer is not None and size_roots:
                    # Final observation + drain before stage transition clears percent.
                    progress_writer.observe(final_watched)
                    await progress_writer.flush()
        finally:
            await _cancel_aux()
            if failure_path or proc.returncode is None:
                await _terminate_download_group(proc, pgid=pgid)

        if limit_code is not None:
            raise_download_error(
                limit_code,
                internal_reason=(
                    "OUTPUT_BUDGET" if limit_code is too_large_code else "DISK_HEADROOM"
                ),
            )

        stdout_out = captured_stdout if capture_stdout else b""
        signal_number: int | None = None
        code = proc.returncode
        if code is not None and code < 0:
            signal_number = -code
        category = process_exit_category(
            exit_code=code,
            timed_out=timed_out,
            signal=signal_number,
            cancelled=False,
        )
        failure = classified_failure
        if timed_out:
            failure = classify_tool_failure(timed_out=True, stderr=b"")
        elif signal_number is not None:
            failure = classify_tool_failure(signal=signal_number, stderr=b"")
        failure_token: str | None = None
        if category.value != "ZERO":
            failure_token = (failure or FailureClass.TOOL_EXIT_NONZERO).value
        return ProcessResult(
            exit_code=code,
            stdout=stdout_out,
            stderr=b"",
            timed_out=timed_out,
            cancelled=False,
            signal=signal_number,
            stdout_byte_count=stdout_count,
            stderr_byte_count=stderr_count,
            failure_class=failure_token,
            process_exit_category=category.value,
        )


def assert_download_env_safe(env: Mapping[str, str]) -> None:
    """Refuse known secret-bearing keys in the download tool environment."""
    assert_env_has_no_secrets(env)
