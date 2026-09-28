"""Bounded subprocess runner for release tooling (SEC-01 / FN-05).

Design notes
------------
* Uses a fresh process group (`start_new_session=True`) so timeout/cancel can
  signal only this command's descendants — never the agent/session group and
  never by process name.
* Reads stdout/stderr with a hard byte cap **per stream** while the process
  runs (avoids unbounded memory growth and pipe deadlocks).
* On timeout/cancel/output-limit: SIGTERM to the process group → bounded grace
  (always waited, even if the direct child already exited) → SIGKILL, then
  reap the direct child. Descendant cleanup is best-effort via killpg of the
  *spawn-time* pgid only.
* Stopping the Docker CLI does **not** prove the Docker daemon finished a
  mutating operation. Callers must map timeout/cancel to failure/uncertain
  journal outcomes and use canonical recovery — never auto-retry migrations.

Limitations
-----------
* Detached processes that leave the process group (setsid again, daemonize)
  are outside this runner's cleanup guarantee.
* After the entire group exits, kernel pgid reuse could theoretically make a
  late killpg target an unrelated group; we only signal the captured spawn
  pgid during failure/cleanup paths and stop once the direct child is reaped
  and reader threads have joined (or hit their join deadline).
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

# Probe defaults — short Docker/Compose diagnostics (ps/inspect/info/config).
DOCKER_PROBE_TIMEOUT_SECONDS = 20.0
DOCKER_PROBE_MIN_REMAINING_SECONDS = 0.25
# Per-stream cap (stdout and stderr independently), not a shared total.
DOCKER_PROBE_OUTPUT_LIMIT_BYTES = 2 * 1024 * 1024
# Grace after SIGTERM before SIGKILL for the managed process group.
TERMINATE_GRACE_SECONDS = 2.0
# How long to wait for the direct child to exit after SIGKILL.
REAP_WAIT_SECONDS = 2.0
# How long to wait for reader threads after process end / kill.
READER_JOIN_SECONDS = 3.0


class BoundedSubprocessError(RuntimeError):
    """Base error for bounded subprocess failures."""


class BudgetExhaustedError(BoundedSubprocessError):
    """No remaining time to start another probe under the parent deadline."""


class BoundedTimeoutError(BoundedSubprocessError):
    """Command exceeded its allotted timeout; process group was terminated."""


class BoundedCancelledError(BoundedSubprocessError):
    """Command was cancelled; process group was terminated."""


class OutputLimitExceededError(BoundedSubprocessError):
    """Stdout or stderr exceeded the configured byte limit."""


@dataclass(frozen=True)
class BoundedResult:
    argv: tuple[str, ...]
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    cancelled: bool = False

    @property
    def stdout_text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")

    @property
    def stderr_text(self) -> str:
        return self.stderr.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class DeadlineBudget:
    """Monotonic wall budget shared by a parent operation and nested probes."""

    deadline_monotonic: float
    clock: Callable[[], float] = time.monotonic

    @classmethod
    def from_duration(
        cls,
        seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> DeadlineBudget:
        if seconds < 0:
            raise ValueError("deadline duration must be non-negative")
        return cls(deadline_monotonic=clock() + seconds, clock=clock)

    def remaining(self) -> float:
        return max(0.0, self.deadline_monotonic - self.clock())

    def exhausted(
        self, *, min_required: float = DOCKER_PROBE_MIN_REMAINING_SECONDS
    ) -> bool:
        return self.remaining() < min_required

    def probe_timeout(
        self,
        cap_seconds: float,
        *,
        min_required: float = DOCKER_PROBE_MIN_REMAINING_SECONDS,
    ) -> float:
        """Return nested timeout = min(cap, remaining), or raise if too little left."""
        if cap_seconds <= 0:
            raise ValueError("cap_seconds must be positive")
        rem = self.remaining()
        if rem < min_required:
            raise BudgetExhaustedError(
                f"deadline exhausted ({rem:.3f}s remaining; need {min_required:.3f}s)"
            )
        return min(cap_seconds, rem)

    def sleep_budget(
        self,
        requested: float,
        *,
        min_required: float = DOCKER_PROBE_MIN_REMAINING_SECONDS,
    ) -> float:
        """Return sleep duration capped by remaining budget (0 if exhausted)."""
        rem = self.remaining()
        if rem < min_required:
            return 0.0
        return min(max(0.0, requested), rem)


class CancelToken:
    """Cooperative cancellation flag for a managed subprocess."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float) -> bool:
        return self._event.wait(timeout)


def _kill_process_group(pgid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return
    except PermissionError:
        try:
            os.kill(pgid, sig)
        except ProcessLookupError:
            return


def _terminate_group(
    proc: subprocess.Popen[bytes],
    *,
    pgid: int | None,
    grace_seconds: float,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> None:
    """Graceful then forced kill of the spawn-time process group.

    Grace is always waited after SIGTERM even if the direct child already
    exited, so SIGTERM-ignoring in-group descendants still get a window before
    SIGKILL. Cleanup does not stop solely because ``proc.poll()`` is set.
    """
    if pgid is not None:
        _kill_process_group(pgid, signal.SIGTERM)
    deadline = clock() + max(0.0, grace_seconds)
    while clock() < deadline:
        sleeper(min(0.05, max(0.0, deadline - clock())))
    if pgid is not None:
        _kill_process_group(pgid, signal.SIGKILL)
        # Re-issue: leader may have exited while descendants still held pipes.
        _kill_process_group(pgid, signal.SIGKILL)
    if proc.poll() is None:
        try:
            proc.wait(timeout=REAP_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            if pgid is not None:
                _kill_process_group(pgid, signal.SIGKILL)
            try:
                proc.wait(timeout=REAP_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                pass


def _read_stream_bounded(
    stream,
    *,
    limit: int,
    which: str,
    sink: list[bytes],
    error_box: list[BaseException],
    abort: threading.Event,
) -> None:
    """Read until EOF, abort, or byte limit. Does not unbounded-drain on limit."""
    try:
        total = 0
        while not abort.is_set():
            chunk = stream.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                keep = max(0, limit - (total - len(chunk)))
                if keep:
                    sink.append(chunk[:keep])
                error_box.append(
                    OutputLimitExceededError(f"{which} exceeded {limit} bytes")
                )
                # Stop reading; caller must terminate the writer process group
                # so pipes close. Avoid an unbounded drain loop here.
                abort.set()
                return
            sink.append(chunk)
    except Exception as exc:  # noqa: BLE001 — surface to launcher thread
        error_box.append(exc)
        abort.set()


def run_bounded(
    argv: Sequence[str],
    *,
    timeout: float,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    input_bytes: bytes | None = None,
    max_stdout_bytes: int = DOCKER_PROBE_OUTPUT_LIMIT_BYTES,
    max_stderr_bytes: int = DOCKER_PROBE_OUTPUT_LIMIT_BYTES,
    cancel_token: CancelToken | None = None,
    grace_seconds: float = TERMINATE_GRACE_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> BoundedResult:
    """Run argv without a shell under a hard timeout and process-group cleanup."""
    if not argv:
        raise BoundedSubprocessError("empty argv")
    if isinstance(argv, (str, bytes)):
        raise BoundedSubprocessError(
            "argv must be a sequence of strings, not a shell string"
        )
    if timeout <= 0:
        raise BudgetExhaustedError("timeout must be positive")

    argv_list = [str(part) for part in argv]
    proc = subprocess.Popen(  # noqa: S603 — argv list, no shell
        argv_list,
        cwd=str(cwd) if cwd is not None else None,
        env=dict(env) if env is not None else None,
        stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    pgid = proc.pid
    assert proc.stdout is not None
    assert proc.stderr is not None

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    reader_errors: list[BaseException] = []
    abort = threading.Event()
    out_thread = threading.Thread(
        target=_read_stream_bounded,
        kwargs={
            "stream": proc.stdout,
            "limit": max_stdout_bytes,
            "which": "stdout",
            "sink": stdout_chunks,
            "error_box": reader_errors,
            "abort": abort,
        },
        name="fetchnow-bounded-stdout",
        daemon=True,
    )
    err_thread = threading.Thread(
        target=_read_stream_bounded,
        kwargs={
            "stream": proc.stderr,
            "limit": max_stderr_bytes,
            "which": "stderr",
            "sink": stderr_chunks,
            "error_box": reader_errors,
            "abort": abort,
        },
        name="fetchnow-bounded-stderr",
        daemon=True,
    )
    out_thread.start()
    err_thread.start()

    if input_bytes is not None and proc.stdin is not None:
        try:
            proc.stdin.write(input_bytes)
            proc.stdin.close()
        except BrokenPipeError:
            pass

    deadline = clock() + timeout
    timed_out = False
    cancelled = False
    output_limited = False
    failure_cleanup = False

    def _needs_group_cleanup() -> bool:
        return timed_out or cancelled or output_limited or abort.is_set()

    try:
        while True:
            if cancel_token is not None and cancel_token.cancelled:
                cancelled = True
                failure_cleanup = True
                break
            if clock() >= deadline:
                timed_out = True
                failure_cleanup = True
                break
            if any(isinstance(e, OutputLimitExceededError) for e in reader_errors):
                output_limited = True
                failure_cleanup = True
                break
            if proc.poll() is not None:
                # Leader exited. Descendants may still hold pipes — wait briefly
                # for readers; if they do not finish, treat as cleanup-needed.
                out_thread.join(timeout=READER_JOIN_SECONDS)
                err_thread.join(timeout=READER_JOIN_SECONDS)
                if out_thread.is_alive() or err_thread.is_alive():
                    failure_cleanup = True
                break
            sleeper(0.05)

        if failure_cleanup or _needs_group_cleanup():
            abort.set()
            _terminate_group(
                proc,
                pgid=pgid,
                grace_seconds=grace_seconds,
                clock=clock,
                sleeper=sleeper,
            )
        else:
            try:
                proc.wait(timeout=REAP_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                failure_cleanup = True
                abort.set()
                _terminate_group(
                    proc,
                    pgid=pgid,
                    grace_seconds=grace_seconds,
                    clock=clock,
                    sleeper=sleeper,
                )
    finally:
        abort.set()
        out_thread.join(timeout=READER_JOIN_SECONDS + grace_seconds)
        err_thread.join(timeout=READER_JOIN_SECONDS + grace_seconds)
        for stream in (proc.stdout, proc.stderr, proc.stdin):
            if stream is not None:
                try:
                    stream.close()
                except Exception:  # noqa: BLE001
                    pass
        if proc.poll() is None:
            _terminate_group(
                proc,
                pgid=pgid,
                grace_seconds=grace_seconds,
                clock=clock,
                sleeper=sleeper,
            )
        # If leader already exited but readers were stuck, ensure group kill
        # already happened above when failure_cleanup was set. If descendants
        # somehow remain after a "clean" leader exit with finished readers,
        # we do not killpg (pgid may already be free) — documented limit.

    stdout = b"".join(stdout_chunks)
    stderr = b"".join(stderr_chunks)
    result = BoundedResult(
        argv=tuple(argv_list),
        returncode=proc.returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        cancelled=cancelled,
    )

    # Prefer original failure causes over cleanup side-effects.
    if cancelled:
        raise BoundedCancelledError(
            f"command cancelled: {' '.join(argv_list[:3])}…"
            if len(argv_list) > 3
            else f"command cancelled: {' '.join(argv_list)}"
        )
    if timed_out:
        raise BoundedTimeoutError(
            f"command timed out after {timeout:.3f}s: {' '.join(argv_list[:6])}"
        )
    for err in reader_errors:
        if isinstance(err, OutputLimitExceededError):
            raise err
        raise BoundedSubprocessError(str(err)) from err
    if failure_cleanup:
        # Forced group cleanup after leader exit with stuck readers, or unreaped
        # child — never report this as a clean success.
        raise BoundedSubprocessError(
            "managed process group required forced cleanup: "
            + " ".join(argv_list[:6])
        )
    return result


def run_docker_probe(
    argv: Sequence[str],
    *,
    budget: DeadlineBudget | None = None,
    probe_cap_seconds: float = DOCKER_PROBE_TIMEOUT_SECONDS,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    cancel_token: CancelToken | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> BoundedResult:
    """Run a short Docker/Compose diagnostic under an optional parent budget."""
    if budget is None:
        timeout = probe_cap_seconds
    else:
        timeout = budget.probe_timeout(probe_cap_seconds)
    return run_bounded(
        argv,
        timeout=timeout,
        cwd=cwd,
        env=env,
        cancel_token=cancel_token,
        clock=clock,
        sleeper=sleeper,
    )
