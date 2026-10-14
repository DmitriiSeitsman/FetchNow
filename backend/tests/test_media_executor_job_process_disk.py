"""F2: production job_process disk final-check + pre-start regressions.

Offline proof of the monitor algorithm. Replaced Linux prerequisites:
- prctl(PR_SET_CHILD_SUBREAPER) via ctypes.CDLL mock
- cgroup.kill via write_cgroup_kill → SIGKILL of the real child
- identity-aware measure_tree_bytes → local walk (_tree_bytes monkeypatch
  optional); default tests use the module helper with a local walk stand-in

Real: child process, filesystem growth/tree, production monitor loop + final
post-reap check. NOT native Landlock/cgroup containment evidence.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from fetchnow.media_executor import job_process
from fetchnow.media_executor.runner import LocalRunner, _tree_bytes


def _patch_linux_prerequisites(
    monkeypatch: pytest.MonkeyPatch, attempt: Path
) -> list[subprocess.Popen[bytes]]:
    """Replace prctl/cgroup; keep real child + production monitor loop."""
    monkeypatch.setattr(
        job_process.ctypes, "CDLL", lambda *a, **kw: Mock(prctl=lambda *args: 0)
    )
    # Local walk stand-in for identity-aware measure_tree_bytes.
    monkeypatch.setattr(job_process, "_tree_bytes", _tree_bytes)
    # Force reliance on the post-reap final check (bypass that Codex reproduced).
    monkeypatch.setattr(job_process, "DISK_SAMPLE_INTERVAL_SECONDS", 3600.0)
    launched: list[subprocess.Popen[bytes]] = []
    original = subprocess.Popen

    def launch(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        proc = original(*args, **kwargs)
        launched.append(proc)
        return proc

    monkeypatch.setattr(job_process.subprocess, "Popen", launch)

    def terminate(_path: Path) -> None:
        for proc in launched:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(proc.pid, 9)

    monkeypatch.setattr(job_process, "write_cgroup_kill", terminate)
    return launched


def _run_execute(
    monkeypatch: pytest.MonkeyPatch,
    attempt: Path,
    command: list[str],
    *,
    max_growth: int,
    max_tree: int,
    min_free: int,
    seconds: float = 5.0,
    cancel: bool = False,
) -> tuple[dict[str, object], list[subprocess.Popen[bytes]]]:
    launched = _patch_linux_prerequisites(monkeypatch, attempt)
    read_fd, write_fd = os.pipe()
    try:
        if cancel:
            os.write(write_fd, b"c")
        result = job_process.execute(
            attempt / "cgroup-placeholder",
            read_fd,
            seconds,
            max_growth,
            max_tree,
            min_free,
            attempt,
            command,
        )
    finally:
        os.close(read_fd)
        os.close(write_fd)
    return result, launched


def test_fast_growth_over_ceiling_fails_via_final_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Child writes past growth and exits before periodic sample → failure."""
    attempt = tmp_path / "a"
    attempt.mkdir()
    out = attempt / "out.bin"
    script = (
        "from pathlib import Path\n"
        f"Path({str(out)!r}).write_bytes(b'x' * 8192)\n"
    )
    result, launched = _run_execute(
        monkeypatch,
        attempt,
        [sys.executable, "-c", script],
        max_growth=1024,
        max_tree=100_000,
        min_free=0,
    )
    assert result["exit_code"] == 1
    assert result["timed_out"] is False
    assert result["cancelled"] is False
    assert launched and launched[0].returncode is not None
    assert out.stat().st_size == 8192


def test_fast_tree_over_ceiling_fails_via_final_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = tmp_path / "a"
    attempt.mkdir()
    (attempt / "seed").write_bytes(b"s" * 2000)
    out = attempt / "more.bin"
    script = (
        "from pathlib import Path\n"
        f"Path({str(out)!r}).write_bytes(b'y' * 2000)\n"
    )
    result, _launched = _run_execute(
        monkeypatch,
        attempt,
        [sys.executable, "-c", script],
        max_growth=50_000,
        max_tree=3000,  # seed+output exceeds
        min_free=0,
    )
    assert result["exit_code"] == 1


def test_success_exit_with_violated_min_free_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = tmp_path / "a"
    attempt.mkdir()
    import shutil

    real_usage = shutil.disk_usage

    class _Usage:
        def __init__(self, free: int) -> None:
            self.total = 10**12
            self.used = 0
            self.free = free

    calls = {"n": 0}

    def fake_usage(path: object) -> object:
        calls["n"] += 1
        # Pre-start check must pass; final check must fail.
        if calls["n"] == 1:
            return _Usage(free=10_000)
        return _Usage(free=10)

    monkeypatch.setattr(job_process.shutil, "disk_usage", fake_usage)
    result, _launched = _run_execute(
        monkeypatch,
        attempt,
        [sys.executable, "-c", "print('ok')"],
        max_growth=0,
        max_tree=0,
        min_free=1000,
    )
    assert result["exit_code"] == 1
    del real_usage


def test_final_measurement_error_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = tmp_path / "a"
    attempt.mkdir()
    launched = _patch_linux_prerequisites(monkeypatch, attempt)
    calls = {"n": 0}
    real_tree = _tree_bytes

    def flaky(root: Path) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            return real_tree(root)  # baseline / pre-start
        raise OSError("stat_failed")

    monkeypatch.setattr(job_process, "_tree_bytes", flaky)
    read_fd, write_fd = os.pipe()
    try:
        result = job_process.execute(
            attempt / "cg",
            read_fd,
            5.0,
            10_000,
            10_000,
            0,
            attempt,
            [sys.executable, "-c", "print('ok')"],
        )
    finally:
        os.close(read_fd)
        os.close(write_fd)
    assert result["exit_code"] == 1
    assert launched and launched[0].returncode is not None


def test_short_process_within_limits_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = tmp_path / "a"
    attempt.mkdir()
    out = attempt / "tiny.bin"
    script = (
        "from pathlib import Path\n"
        f"Path({str(out)!r}).write_bytes(b'ok')\n"
    )
    result, _launched = _run_execute(
        monkeypatch,
        attempt,
        [sys.executable, "-c", script],
        max_growth=1024,
        max_tree=10_000,
        min_free=0,
    )
    assert result["exit_code"] == 0
    assert result["timed_out"] is False
    assert result["cancelled"] is False


def test_large_staged_inputs_plus_legal_output_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Staged inputs must not consume the growth budget."""
    attempt = tmp_path / "a"
    attempt.mkdir()
    (attempt / "input-video").write_bytes(b"v" * 8000)
    (attempt / "input-audio").write_bytes(b"a" * 8000)
    out = attempt / "output-mux"
    script = (
        "from pathlib import Path\n"
        f"Path({str(out)!r}).write_bytes(b'o' * 100)\n"
    )
    result, _launched = _run_execute(
        monkeypatch,
        attempt,
        [sys.executable, "-c", script],
        max_growth=1000,
        max_tree=20_000,
        min_free=0,
    )
    assert result["exit_code"] == 0


def test_timeout_remains_primary_when_disk_also_exceeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Timeout stays primary; post-reap disk failure only forces exit_code=1."""
    attempt = tmp_path / "a"
    attempt.mkdir()
    launched = _patch_linux_prerequisites(monkeypatch, attempt)
    checks = {"n": 0}

    def always_exceeded(**_kwargs: object) -> bool:
        checks["n"] += 1
        return True

    # Periodic samples disabled (3600s); only the final post-reap check runs.
    monkeypatch.setattr(job_process, "_disk_limits_exceeded", always_exceeded)
    read_fd, write_fd = os.pipe()
    try:
        result = job_process.execute(
            attempt / "cg",
            read_fd,
            0.2,
            1024,
            100_000,
            0,
            attempt,
            [sys.executable, "-c", "import time; time.sleep(30)"],
        )
    finally:
        os.close(read_fd)
        os.close(write_fd)
    assert result["timed_out"] is True
    assert result["cancelled"] is False
    assert result["exit_code"] == 1
    assert checks["n"] >= 1
    assert launched and launched[0].returncode is not None


def test_cancel_remains_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = tmp_path / "a"
    attempt.mkdir()
    result, _launched = _run_execute(
        monkeypatch,
        attempt,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        max_growth=0,
        max_tree=0,
        min_free=0,
        seconds=5.0,
        cancel=True,
    )
    assert result["cancelled"] is True
    assert result["timed_out"] is False


def test_cleanup_reaping_failure_does_not_publish_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = tmp_path / "a"
    attempt.mkdir()
    _patch_linux_prerequisites(monkeypatch, attempt)

    def boom(_proc: object, seconds: float = 5) -> None:
        raise OSError("exception cleanup reaping deadline")

    monkeypatch.setattr(job_process, "reap_children", boom)
    read_fd, write_fd = os.pipe()
    try:
        with pytest.raises(OSError, match="reaping"):
            job_process.execute(
                attempt / "cg",
                read_fd,
                5.0,
                10_000,
                10_000,
                0,
                attempt,
                [sys.executable, "-c", "print('ok')"],
            )
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_prestart_tree_ceiling_refuses_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = tmp_path / "a"
    attempt.mkdir()
    (attempt / "huge").write_bytes(b"h" * 5000)
    launched = _patch_linux_prerequisites(monkeypatch, attempt)
    read_fd, write_fd = os.pipe()
    try:
        with pytest.raises(OSError, match="tree exceeds ceiling before launch"):
            job_process.execute(
                attempt / "cg",
                read_fd,
                5.0,
                100_000,
                1000,
                0,
                attempt,
                [sys.executable, "-c", "print('should-not-run')"],
            )
    finally:
        os.close(read_fd)
        os.close(write_fd)
    assert launched == []


def test_prestart_min_free_refuses_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = tmp_path / "a"
    attempt.mkdir()
    launched = _patch_linux_prerequisites(monkeypatch, attempt)

    class _Usage:
        total = 10**12
        used = 10**12
        free = 10

    monkeypatch.setattr(
        job_process.shutil, "disk_usage", lambda _p: _Usage()
    )
    read_fd, write_fd = os.pipe()
    try:
        with pytest.raises(OSError, match="insufficient free space before launch"):
            job_process.execute(
                attempt / "cg",
                read_fd,
                5.0,
                0,
                0,
                1000,
                attempt,
                [sys.executable, "-c", "print('should-not-run')"],
            )
    finally:
        os.close(read_fd)
        os.close(write_fd)
    assert launched == []


def test_local_runner_final_check_matches_contract(tmp_path: Path) -> None:
    """LocalRunner: exit between polls still fails on oversize growth."""
    attempt = tmp_path / "a"
    attempt.mkdir()
    script = attempt / "write.py"
    script.write_text(
        "from pathlib import Path\nPath('out.bin').write_bytes(b'x' * 8192)\n"
    )
    outcome = LocalRunner().run(
        attempt=attempt,
        argv=[sys.executable, str(script)],
        timeout_seconds=5,
        cancel=threading.Event(),
        protected=[],
        max_growth_bytes=1024,
        max_tree_bytes=100_000,
        min_free_bytes=None,
    )
    assert outcome.exit_code == 1


def test_local_runner_prestart_tree_refuses(tmp_path: Path) -> None:
    attempt = tmp_path / "a"
    attempt.mkdir()
    (attempt / "huge").write_bytes(b"h" * 5000)
    marker = attempt / "ran"
    script = attempt / "x.py"
    script.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('x')\n"
    )
    outcome = LocalRunner().run(
        attempt=attempt,
        argv=[sys.executable, str(script)],
        timeout_seconds=5,
        cancel=threading.Event(),
        protected=[],
        max_growth_bytes=100_000,
        max_tree_bytes=1000,
        min_free_bytes=None,
    )
    assert outcome.exit_code == 1
    assert not marker.exists()
