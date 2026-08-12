"""Hardened subprocess runner for metadata tools (shell=False)."""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Mapping

from fetchnow.media_inspection.errors import (
    InspectionErrorKind,
    raise_inspection_error,
)
from fetchnow.media_inspection.protocols import ProcessResult


def _kill_process_group(pgid: int) -> None:
    """SIGKILL the process group. Safe if the group is already gone."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        try:
            os.kill(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return


async def _read_bounded(
    stream: asyncio.StreamReader | None,
    *,
    limit: int,
    which: str,
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
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_OUTPUT_LIMIT_EXCEEDED,
                internal_reason=f"{which.upper()}_LIMIT",
            )
        chunks.append(block)
    return b"".join(chunks)


async def _terminate_process_group(
    proc: asyncio.subprocess.Process,
    *,
    pgid: int | None,
    rounds: int = 5,
    wait_seconds: float = 0.2,
) -> None:
    """Kill the original process group even if the direct child already exited.

    asyncio may set ``proc.returncode`` when the direct child exits while a
    descendant still holds inherited pipes. Returning early on a non-None
    returncode would leave that descendant alive — always SIGKILL the recorded
    process group first.

    Residual risk: if the kernel recycles the process-group ID after the entire
    group is gone, a later killpg could theoretically target an unrelated group.
    We only kill the pgid captured at spawn time and only during failure paths.
    """
    for _ in range(rounds):
        if pgid is not None:
            _kill_process_group(pgid)
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=wait_seconds)
            except TimeoutError:
                continue
        else:
            # Direct child already reaped — still re-issue killpg for descendants.
            if pgid is not None:
                _kill_process_group(pgid)
            await asyncio.sleep(0)
            return
    if pgid is not None:
        _kill_process_group(pgid)
    if proc.returncode is None:
        raise_inspection_error(
            InspectionErrorKind.INTERNAL_INSPECTION_ERROR,
            internal_reason="PROCESS_REAP_FAILED",
        )


class SubprocessProcessRunner:
    """Unix process-group runner with hard timeout and explicit group kill."""

    async def run(
        self,
        argv: list[str],
        *,
        env: dict[str, str],
        cwd: str,
        timeout_seconds: float,
        stdout_limit_bytes: int,
        stderr_limit_bytes: int,
    ) -> ProcessResult:
        if not argv:
            raise_inspection_error(
                InspectionErrorKind.INTERNAL_INSPECTION_ERROR,
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
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_TOOL_UNAVAILABLE,
                internal_reason="PROCESS_SPAWN_FAILED",
            )

        # Capture the process-group id at spawn (leader PID with start_new_session).
        pgid = proc.pid
        stdout_task: asyncio.Task[bytes] | None = None
        stderr_task: asyncio.Task[bytes] | None = None
        failure_path = False

        async def _communicate() -> tuple[bytes, bytes]:
            nonlocal stdout_task, stderr_task
            assert proc.stdout is not None
            assert proc.stderr is not None
            stdout_task = asyncio.create_task(
                _read_bounded(
                    proc.stdout, limit=stdout_limit_bytes, which="stdout"
                )
            )
            stderr_task = asyncio.create_task(
                _read_bounded(
                    proc.stderr, limit=stderr_limit_bytes, which="stderr"
                )
            )
            try:
                stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
            except Exception:
                stdout_task.cancel()
                stderr_task.cancel()
                await asyncio.gather(
                    stdout_task, stderr_task, return_exceptions=True
                )
                raise
            await proc.wait()
            return stdout, stderr

        async def _cancel_readers() -> None:
            tasks = [t for t in (stdout_task, stderr_task) if t is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        timed_out = False
        stdout = b""
        stderr = b""
        try:
            try:
                stdout, stderr = await asyncio.wait_for(
                    _communicate(), timeout=timeout_seconds
                )
            except TimeoutError:
                failure_path = True
                timed_out = True
                await _cancel_readers()
                await _terminate_process_group(proc, pgid=pgid)
            except asyncio.CancelledError:
                failure_path = True
                await _cancel_readers()
                await _terminate_process_group(proc, pgid=pgid)
                raise
            except Exception:
                failure_path = True
                await _cancel_readers()
                await _terminate_process_group(proc, pgid=pgid)
                raise
        finally:
            if failure_path or proc.returncode is None:
                await _cancel_readers()
                if failure_path or proc.returncode is None:
                    await _terminate_process_group(proc, pgid=pgid)

        if timed_out:
            return ProcessResult(
                exit_code=proc.returncode,
                stdout=b"",
                stderr=b"",
                timed_out=True,
                cancelled=False,
            )

        signal_number: int | None = None
        code = proc.returncode
        if code is not None and code < 0:
            signal_number = -code
        return ProcessResult(
            exit_code=code,
            stdout=stdout,
            stderr=stderr,
            timed_out=False,
            cancelled=False,
            signal=signal_number,
        )


def build_sanitized_env(*, home_dir: str, tmp_dir: str) -> dict[str, str]:
    """Minimal environment — no application/DB secrets forwarded."""
    return {
        "HOME": home_dir,
        "TMPDIR": tmp_dir,
        "TEMP": tmp_dir,
        "TMP": tmp_dir,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "NETRC": os.path.join(home_dir, "nonexistent_netrc"),
    }


def assert_env_has_no_secrets(env: Mapping[str, str]) -> None:
    """Refuse known secret-bearing or proxy keys in tool environment."""
    forbidden = {
        "DATABASE_URL",
        "POSTGRES_PASSWORD",
        "PASSWORD",
        "SECRET",
        "API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    }
    if forbidden.intersection(env):
        raise_inspection_error(
            InspectionErrorKind.INTERNAL_INSPECTION_ERROR,
            internal_reason="ENV_CONTAINS_SECRETS",
        )
