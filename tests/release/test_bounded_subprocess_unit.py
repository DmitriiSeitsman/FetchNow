"""SEC-01 bounded subprocess / deadline regressions (acceptance-strength)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fetchnow_release.bounded_subprocess import (  # noqa: E402
    BudgetExhaustedError,
    BoundedCancelledError,
    BoundedSubprocessError,
    BoundedTimeoutError,
    CancelToken,
    DeadlineBudget,
    OutputLimitExceededError,
    run_bounded,
    run_docker_probe,
)


def _py_script(tmp_path: Path, body: str, name: str = "stub.py") -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_successful_command_returns_output(tmp_path: Path) -> None:
    script = _py_script(
        tmp_path,
        """
        import sys
        sys.stdout.buffer.write(b"hello-out")
        sys.stderr.buffer.write(b"hello-err")
        """,
    )
    result = run_bounded([sys.executable, str(script)], timeout=5.0)
    assert result.returncode == 0
    assert result.stdout == b"hello-out"
    assert result.stderr == b"hello-err"
    assert not result.timed_out
    assert not result.cancelled


def test_nonzero_exit_is_not_success(tmp_path: Path) -> None:
    script = _py_script(tmp_path, "import sys; sys.exit(7)\n")
    result = run_bounded([sys.executable, str(script)], timeout=5.0)
    assert result.returncode == 7
    assert not result.timed_out


def test_hanging_stub_times_out_and_reaps(tmp_path: Path) -> None:
    script = _py_script(tmp_path, "import time; time.sleep(60)\n")
    started = time.monotonic()
    with pytest.raises(BoundedTimeoutError):
        run_bounded([sys.executable, str(script)], timeout=0.4, grace_seconds=0.2)
    assert time.monotonic() - started < 5.0


def test_cancellation_token_terminates_group(tmp_path: Path) -> None:
    script = _py_script(tmp_path, "import time; time.sleep(60)\n")
    token = CancelToken()

    def _cancel_soon() -> None:
        time.sleep(0.15)
        token.cancel()

    threading.Thread(target=_cancel_soon, daemon=True).start()
    with pytest.raises(BoundedCancelledError):
        run_bounded(
            [sys.executable, str(script)],
            timeout=5.0,
            cancel_token=token,
            grace_seconds=0.2,
        )


def test_real_sigterm_to_wrapper_subprocess(tmp_path: Path) -> None:
    """SIGTERM the wrapper process in a child — never the agent/pytest runner."""
    wrapper = ROOT / "scripts" / "run_pytest_bounded.py"
    hang = _py_script(
        tmp_path,
        """
        import time
        def test_hang():
            time.sleep(60)
        """,
        name="test_hang_only.py",
    )
    env = {
        **os.environ,
        "FETCHNOW_PYTEST_WALL_SECONDS": "30",
        "PYTHONPATH": str(SCRIPTS),
    }
    proc = subprocess.Popen(
        [sys.executable, str(wrapper), "--", "-q", str(hang)],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    time.sleep(0.5)
    assert proc.poll() is None
    os.kill(proc.pid, signal.SIGTERM)
    try:
        _out, err = proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate(timeout=5)
        pytest.fail("wrapper did not exit after SIGTERM")
    assert proc.returncode == 130
    assert b"cancelled by signal" in err or b"cancelled" in err.lower()
    assert not _alive(proc.pid)


def test_descendant_ignoring_sigterm_is_force_killed(tmp_path: Path) -> None:
    marker = tmp_path / "child.pid"
    script = _py_script(
        tmp_path,
        f"""
        import os
        import signal
        import time
        from pathlib import Path

        pid = os.fork()
        if pid == 0:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            Path({str(marker)!r}).write_text(str(os.getpid()), encoding="utf-8")
            time.sleep(60)
            os._exit(0)
        time.sleep(60)
        """,
    )
    started = time.monotonic()
    with pytest.raises(BoundedTimeoutError):
        run_bounded(
            [sys.executable, str(script)],
            timeout=0.5,
            grace_seconds=0.2,
        )
    assert time.monotonic() - started < 5.0
    # Give the OS a beat to reap; child must not remain.
    deadline = time.monotonic() + 2.0
    child_pid = None
    while time.monotonic() < deadline:
        if marker.exists():
            child_pid = int(marker.read_text(encoding="utf-8").strip())
            if not _alive(child_pid):
                break
        time.sleep(0.05)
    assert child_pid is not None, "descendant never wrote pid marker"
    assert not _alive(child_pid), f"SIGTERM-ignoring descendant {child_pid} still alive"


def test_leader_exits_descendant_holds_pipe(tmp_path: Path) -> None:
    """Leader exits; in-group child keeps FDs open — cleanup must kill the group.

    Session-leader exit can deliver SIGHUP; the child ignores SIGHUP/SIGTERM so
    cleanup must still reach SIGKILL rather than relying on poll()-only logic.
    """
    marker = tmp_path / "writer.pid"
    script = _py_script(
        tmp_path,
        f"""
        import os
        import signal
        import sys
        import time
        from pathlib import Path

        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            os.close(r)
            Path({str(marker)!r}).write_text(str(os.getpid()), encoding="utf-8")
            try:
                while True:
                    os.write(w, b"x" * 1024)
                    time.sleep(0.05)
            except BrokenPipeError:
                # Keep holding inherited stdout/stderr even if private pipe breaks.
                while True:
                    time.sleep(1)
            os._exit(0)
        os.close(w)
        for _ in range(100):
            if Path({str(marker)!r}).exists():
                break
            time.sleep(0.01)
        sys.exit(0)
        """,
    )
    started = time.monotonic()
    with pytest.raises(
        (BoundedTimeoutError, OutputLimitExceededError, BoundedSubprocessError)
    ):
        run_bounded(
            [sys.executable, str(script)],
            timeout=2.0,
            grace_seconds=0.3,
            max_stdout_bytes=64 * 1024,
            max_stderr_bytes=64 * 1024,
        )
    assert time.monotonic() - started < 10.0
    deadline = time.monotonic() + 2.0
    child_pid = None
    while time.monotonic() < deadline:
        if marker.exists():
            child_pid = int(marker.read_text(encoding="utf-8").strip())
            if not _alive(child_pid):
                break
        time.sleep(0.05)
    assert child_pid is not None
    assert not _alive(child_pid), f"pipe-holding descendant {child_pid} still alive"


def test_simultaneous_stdout_and_stderr(tmp_path: Path) -> None:
    script = _py_script(
        tmp_path,
        """
        import sys
        for i in range(100):
            sys.stdout.buffer.write(b"O" * 100)
            sys.stderr.buffer.write(b"E" * 100)
            sys.stdout.buffer.flush()
            sys.stderr.buffer.flush()
        """,
    )
    result = run_bounded([sys.executable, str(script)], timeout=5.0)
    assert result.returncode == 0
    assert len(result.stdout) == 10000
    assert len(result.stderr) == 10000


def test_output_limit_is_per_stream_not_shared(tmp_path: Path) -> None:
    script = _py_script(
        tmp_path,
        """
        import sys
        sys.stdout.buffer.write(b"a" * 5000)
        sys.stderr.buffer.write(b"b" * 5000)
        """,
    )
    result = run_bounded(
        [sys.executable, str(script)],
        timeout=5.0,
        max_stdout_bytes=8000,
        max_stderr_bytes=8000,
    )
    assert result.returncode == 0
    assert len(result.stdout) == 5000
    assert len(result.stderr) == 5000


def test_filling_one_pipe_does_not_block_other(tmp_path: Path) -> None:
    """Large stdout under limit while stderr stays small must complete."""
    script = _py_script(
        tmp_path,
        """
        import sys
        sys.stderr.buffer.write(b"err-ok")
        sys.stderr.buffer.flush()
        sys.stdout.buffer.write(b"o" * (512 * 1024))
        sys.stdout.buffer.flush()
        """,
    )
    result = run_bounded(
        [sys.executable, str(script)],
        timeout=5.0,
        max_stdout_bytes=1024 * 1024,
        max_stderr_bytes=1024 * 1024,
    )
    assert result.returncode == 0
    assert result.stderr == b"err-ok"
    assert len(result.stdout) == 512 * 1024


def test_large_stdout_limit_not_success(tmp_path: Path) -> None:
    script = _py_script(
        tmp_path,
        """
        import sys
        chunk = b"x" * 65536
        for _ in range(200):
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
        """,
    )
    with pytest.raises(OutputLimitExceededError):
        run_bounded(
            [sys.executable, str(script)],
            timeout=5.0,
            max_stdout_bytes=100_000,
            max_stderr_bytes=100_000,
            grace_seconds=0.3,
        )


def test_invalid_utf8_is_replace_not_crash(tmp_path: Path) -> None:
    script = _py_script(
        tmp_path,
        """
        import sys
        sys.stdout.buffer.write(b"ok\\xff\\xfeend")
        """,
    )
    result = run_bounded([sys.executable, str(script)], timeout=5.0)
    assert result.returncode == 0
    text = result.stdout_text
    assert "ok" in text and "end" in text
    assert "\ufffd" in text


def test_process_exits_as_timeout_fires(tmp_path: Path) -> None:
    script = _py_script(tmp_path, "import time; time.sleep(0.2)\n")
    try:
        result = run_bounded(
            [sys.executable, str(script)], timeout=0.2, grace_seconds=0.1
        )
        assert result.returncode is not None
    except BoundedTimeoutError:
        pass


def test_foreign_process_other_group_survives(tmp_path: Path) -> None:
    """Control: unrelated process group must not be killed by cleanup."""
    foreign = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        script = _py_script(tmp_path, "import time; time.sleep(60)\n")
        with pytest.raises(BoundedTimeoutError):
            run_bounded(
                [sys.executable, str(script)],
                timeout=0.4,
                grace_seconds=0.2,
            )
        assert _alive(foreign.pid)
    finally:
        foreign.send_signal(signal.SIGKILL)
        foreign.wait(timeout=5)


def test_nested_probe_respects_remaining_budget() -> None:
    budget = DeadlineBudget.from_duration(1.0)
    time.sleep(0.4)
    timeout = budget.probe_timeout(20.0)
    assert timeout < 1.0
    assert timeout <= 0.65
    assert timeout >= 0.25


def test_exhausted_budget_does_not_start_probe() -> None:
    budget = DeadlineBudget.from_duration(0.0)
    with pytest.raises(BudgetExhaustedError):
        run_docker_probe([sys.executable, "-c", "print(1)"], budget=budget)


def test_zero_timeout_rejected() -> None:
    with pytest.raises(BudgetExhaustedError):
        run_bounded([sys.executable, "-c", "print(1)"], timeout=0.0)


def test_fake_clock_advances_with_sleeper() -> None:
    state = {"now": 1000.0}

    def clock() -> float:
        return state["now"]

    def sleeper(seconds: float) -> None:
        state["now"] += seconds

    budget = DeadlineBudget.from_duration(1.0, clock=clock)
    sleeper(0.6)
    assert budget.remaining() == pytest.approx(0.4, abs=0.001)
    sleeper(0.5)
    with pytest.raises(BudgetExhaustedError):
        budget.probe_timeout(20.0)


def test_shell_string_argv_rejected() -> None:
    with pytest.raises(Exception):
        run_bounded("echo hi", timeout=1.0)  # type: ignore[arg-type]


def test_timeout_is_not_success(tmp_path: Path) -> None:
    script = _py_script(tmp_path, "import time; time.sleep(30)\n")
    with pytest.raises(BoundedTimeoutError):
        run_bounded([sys.executable, str(script)], timeout=0.3, grace_seconds=0.1)


def test_cancel_is_not_success(tmp_path: Path) -> None:
    script = _py_script(tmp_path, "import time; time.sleep(30)\n")
    token = CancelToken()
    token.cancel()
    with pytest.raises(BoundedCancelledError):
        run_bounded(
            [sys.executable, str(script)],
            timeout=5.0,
            cancel_token=token,
            grace_seconds=0.1,
        )


def test_exception_messages_omit_env_secrets(tmp_path: Path) -> None:
    script = _py_script(tmp_path, "import time; time.sleep(30)\n")
    secret = "SUPER_SECRET_TOKEN_XYZ"
    with pytest.raises(BoundedTimeoutError) as ei:
        run_bounded(
            [sys.executable, str(script)],
            timeout=0.3,
            grace_seconds=0.1,
            env={**os.environ, "FETCHNOW_TEST_SECRET": secret},
        )
    assert secret not in str(ei.value)
