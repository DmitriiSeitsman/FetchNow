"""Worker helpers for SEC-09 network executor calls. Fail closed; no fallback."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from fetchnow.media_executor.client import ExecutorCallError, UnixExecutorClient
from fetchnow.media_executor.handoff import HandoffError, copy_regular
from fetchnow.media_executor.layout import job_directory
from fetchnow.media_executor.protocol import Request
from fetchnow.media_inspection.protocols import ProcessResult


class NetExecutorCleanupError(ExecutorCallError):
    """Release/cleanup failed after the operation outcome was known."""

    def __init__(self, code: str, *, primary: BaseException | None = None) -> None:
        super().__init__(code)
        self.primary = primary


async def _cancel_wait_release(
    client: UnixExecutorClient,
    *,
    job_id: str,
    attempt: int,
    fence: int,
    op_task: asyncio.Task[object] | None,
    wait_seconds: float,
) -> None:
    """Cancel remote tool, wait for confirmed completion, then release."""
    with contextlib.suppress(ExecutorCallError, asyncio.TimeoutError, OSError):
        await client.cancel(job_id=job_id, attempt=attempt, fence=fence)
    if op_task is not None and not op_task.done():
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(op_task), timeout=wait_seconds)
    deadline = asyncio.get_running_loop().time() + max(1.0, wait_seconds)
    while True:
        try:
            await client.release(job_id=job_id, attempt=attempt, fence=fence)
            return
        except ExecutorCallError as exc:
            if exc.code != "already_running":
                raise
            if asyncio.get_running_loop().time() >= deadline:
                raise ExecutorCallError("already_running") from exc
            await asyncio.sleep(0.05)


async def _release_finished(
    client: UnixExecutorClient,
    *,
    job_id: str,
    attempt: int,
    fence: int,
    primary: BaseException | None = None,
) -> None:
    """Release a finished reservation. Preserve primary error if cleanup fails."""
    try:
        await client.release(job_id=job_id, attempt=attempt, fence=fence)
    except ExecutorCallError:
        try:
            await _cancel_wait_release(
                client,
                job_id=job_id,
                attempt=attempt,
                fence=fence,
                op_task=None,
                wait_seconds=5.0,
            )
        except Exception as cleanup2:
            err = NetExecutorCleanupError(
                getattr(cleanup2, "code", "cleanup_failed"),
                primary=primary,
            )
            if primary is not None:
                raise err from primary
            raise err from cleanup2


async def run_inspect_metadata(
    *,
    client: UnixExecutorClient,
    work_root: Path,
    job_id: str,
    attempt: int,
    fence: int,
    url: str,
    provider_id: str,
    timeout_seconds: float,
) -> ProcessResult:
    request = Request(op="reserve", job_id=job_id, attempt=attempt, fence=fence)
    _ = job_directory(work_root, request)
    await client.reserve(job_id=job_id, attempt=attempt, fence=fence)
    op_task: asyncio.Task[ProcessResult] = asyncio.create_task(
        client.inspect_metadata(
            job_id=job_id,
            attempt=attempt,
            fence=fence,
            url=url,
            provider_id=provider_id,
            timeout_seconds=timeout_seconds,
        )
    )
    wait = min(30.0, timeout_seconds + 5.0)
    try:
        result = await op_task
    except asyncio.CancelledError:
        await _cancel_wait_release(
            client,
            job_id=job_id,
            attempt=attempt,
            fence=fence,
            op_task=op_task,
            wait_seconds=wait,
        )
        raise
    except Exception:
        await _cancel_wait_release(
            client,
            job_id=job_id,
            attempt=attempt,
            fence=fence,
            op_task=op_task,
            wait_seconds=wait,
        )
        raise
    await _release_finished(client, job_id=job_id, attempt=attempt, fence=fence)
    return result


async def run_download_to_dir(
    *,
    client: UnixExecutorClient,
    work_root: Path,
    dest_dir: Path,
    op: str,
    job_id: str,
    attempt: int,
    fence: int,
    url: str,
    provider_id: str,
    format_token: str,
    timeout_seconds: float,
    max_bytes: int,
    min_free_bytes: int,
) -> ProcessResult:
    request = Request(op="reserve", job_id=job_id, attempt=attempt, fence=fence)
    job_dir = job_directory(work_root, request)
    await client.reserve(job_id=job_id, attempt=attempt, fence=fence)
    op_task: asyncio.Task[dict[str, object]] = asyncio.create_task(
        client.download(
            op=op,
            job_id=job_id,
            attempt=attempt,
            fence=fence,
            url=url,
            provider_id=provider_id,
            format_token=format_token,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
            min_free_bytes=min_free_bytes,
        )
    )
    wait = min(30.0, timeout_seconds + 5.0)
    try:
        bundled = await op_task
    except asyncio.CancelledError:
        await _cancel_wait_release(
            client,
            job_id=job_id,
            attempt=attempt,
            fence=fence,
            op_task=op_task,
            wait_seconds=wait,
        )
        raise
    except Exception:
        await _cancel_wait_release(
            client,
            job_id=job_id,
            attempt=attempt,
            fence=fence,
            op_task=op_task,
            wait_seconds=wait,
        )
        raise

    primary: BaseException | None = None
    result: ProcessResult | None = None
    try:
        raw_result = bundled["result"]
        if not isinstance(raw_result, ProcessResult):
            raise ExecutorCallError("protocol")
        result = raw_result
        if (
            result.timed_out
            or result.cancelled
            or (result.exit_code is not None and result.exit_code != 0)
        ):
            return result
        payload = bundled["payload"]
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            return result
        name = bundled.get("artifact_name")
        size = bundled.get("artifact_bytes")
        if not isinstance(name, str) or type(size) is not int:
            raise ExecutorCallError("protocol")
        if size > max_bytes:
            raise ExecutorCallError("protocol")
        source = job_dir / name
        if not source.is_file() or source.is_symlink():
            raise HandoffError("artifact missing")
        if not name.startswith("output-artifact."):
            raise HandoffError("artifact name")
        dest_dir.mkdir(parents=True, exist_ok=True)
        copy_regular(source, dest_dir / name, max_bytes=max_bytes)
        return result
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            await _release_finished(
                client,
                job_id=job_id,
                attempt=attempt,
                fence=fence,
                primary=primary,
            )
        except NetExecutorCleanupError:
            # Always surface cleanup failure; primary remains on .primary.
            raise
