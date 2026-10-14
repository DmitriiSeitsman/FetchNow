"""Offline handshake regressions for F2 neighbor continuity (not native proof)."""

from __future__ import annotations

import importlib.util
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DISK_INSIDE = ROOT / "backend" / "tests" / "media_executor" / "disk_native_inside.py"

spec = importlib.util.spec_from_file_location("disk_native_inside", DISK_INSIDE)
assert spec and spec.loader
disk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(disk)


def _reap(proc: subprocess.Popen[bytes], *, grace: float = 1.0) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and proc.poll() is None:
        time.sleep(0.02)
    if proc.poll() is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        proc.wait(timeout=2)


def _wait_file(path: Path, *, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and path.stat().st_size > 0:
            return True
        time.sleep(0.02)
    return False


def test_slow_neighbor_startup_limited_waits_without_premature_growth(
    tmp_path: Path,
) -> None:
    limited = tmp_path / "n1"
    neighbor = tmp_path / "n2"
    limited.mkdir()
    neighbor.mkdir()
    seed = disk.NEIGHBOR_LIMITED_SEED_BYTES
    (limited / "grow.py").write_text(
        disk.limited_neighbor_writer_script(seed_bytes=seed, grow_gate_wait_s=5.0),
        encoding="utf-8",
    )
    (neighbor / "ok.py").write_text(
        disk.healthy_neighbor_script(ready_delay_s=0.6, finish_gate_wait_s=8.0),
        encoding="utf-8",
    )
    lim = subprocess.Popen(
        [sys.executable, str(limited / "grow.py")],
        cwd="/",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    nb = subprocess.Popen(
        [sys.executable, str(neighbor / "ok.py")],
        cwd="/",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert _wait_file(limited / "ready", timeout=2.0)
        assert (limited / "x").stat().st_size == seed
        # Neighbor deliberately slow: limited must stay at seed until grow_gate.
        time.sleep(0.35)
        assert (limited / "x").stat().st_size == seed
        assert not (limited / "grow_gate").exists()
        assert not _wait_file(neighbor / "ready", timeout=0.15)
        assert _wait_file(neighbor / "ready", timeout=2.0)
        assert _wait_file(neighbor / "hb", timeout=1.0)
        hb_before = int((neighbor / "hb").read_text(encoding="utf-8").strip())
        assert hb_before >= 1
        assert lim.poll() is None
        (limited / "grow_gate").write_text("1\n", encoding="utf-8")
        # After release, limited grows past seed (simulated disk growth).
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if (limited / "x").stat().st_size > seed:
                break
            time.sleep(0.02)
        assert (limited / "x").stat().st_size > seed
        # Simulated limited completion: stop limited, require neighbor hb to rise.
        size_at_stop = (limited / "x").stat().st_size
        hb_at_stop = int((neighbor / "hb").read_text(encoding="utf-8").strip())
        _reap(lim)
        deadline = time.monotonic() + 2.0
        grew = False
        while time.monotonic() < deadline:
            cur = int((neighbor / "hb").read_text(encoding="utf-8").strip())
            if cur > hb_at_stop:
                grew = True
                break
            time.sleep(0.05)
        assert grew
        assert (limited / "x").stat().st_size == size_at_stop
        # Neighbor must not finish until finish_gate.
        time.sleep(0.2)
        assert nb.poll() is None
        (neighbor / "finish_gate").write_text("1\n", encoding="utf-8")
        nb.wait(timeout=3)
        assert nb.returncode == 0
    finally:
        _reap(lim)
        _reap(nb)


def test_reverse_startup_order_both_ready_then_release(tmp_path: Path) -> None:
    limited = tmp_path / "n1"
    neighbor = tmp_path / "n2"
    limited.mkdir()
    neighbor.mkdir()
    (limited / "grow.py").write_text(
        disk.limited_neighbor_writer_script(ready_delay_s=0.5, seed_bytes=500),
        encoding="utf-8",
    )
    (neighbor / "ok.py").write_text(
        disk.healthy_neighbor_script(ready_delay_s=0.0),
        encoding="utf-8",
    )
    nb = subprocess.Popen(
        [sys.executable, str(neighbor / "ok.py")],
        cwd="/",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    lim = subprocess.Popen(
        [sys.executable, str(limited / "grow.py")],
        cwd="/",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert _wait_file(neighbor / "ready", timeout=2.0)
        assert _wait_file(neighbor / "hb", timeout=1.0)
        assert _wait_file(limited / "ready", timeout=2.0)
        assert (limited / "x").stat().st_size == 500
        (limited / "grow_gate").write_text("1\n", encoding="utf-8")
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and (limited / "x").stat().st_size <= 500:
            time.sleep(0.02)
        assert (limited / "x").stat().st_size > 500
        (neighbor / "finish_gate").write_text("1\n", encoding="utf-8")
        nb.wait(timeout=3)
        assert nb.returncode == 0
    finally:
        _reap(lim)
        _reap(nb)


def test_readiness_timeout_is_phase_fail_not_disk_pass() -> None:
    evidence = {
        "phase_error": "neighbor_readiness_timeout",
        "both_ready": False,
        "growth_released": False,
        "limited_below_ceiling_at_release": True,
        "failure_class_limited": "disk_limit",
        "limited_writer_started": True,
        "limited": 1,
        "limited_timed_out": False,
        "limited_recording_stopped": True,
        "neighbor": 0,
        "neighbor_timed_out": False,
        "neighbor_alive": True,
        "neighbor_concurrent": True,
        "neighbor_continued_after_limited": True,
        "neighbor_finish_released": True,
        "limited_heartbeat": 2,
        "limited_size": 4000,
        "neighbor_hb_at_limited_end": 3,
        "size_before_release": 500,
    }
    assert disk.evaluate_neighbor(evidence) is False


def test_gate_permission_failure_is_not_disk_pass(tmp_path: Path) -> None:
    attempt = tmp_path / "n1"
    attempt.mkdir()
    (attempt / "grow.py").write_text(
        disk.limited_neighbor_writer_script(grow_gate_wait_s=1.0, seed_bytes=500),
        encoding="utf-8",
    )
    # Unreadable grow_gate path collision: create a directory tool cannot treat as file.
    gate = attempt / "grow_gate"
    gate.mkdir()
    proc = subprocess.Popen(
        [sys.executable, str(attempt / "grow.py")],
        cwd="/",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert _wait_file(attempt / "ready", timeout=2.0)
        # Seed must not grow while gate is not a readable file.
        time.sleep(0.3)
        assert (attempt / "x").stat().st_size == 500
        stdout, stderr = proc.communicate(timeout=3)
        assert proc.returncode == 2
        assert b"grow_gate_timeout" in stderr
        assert (attempt / "x").stat().st_size == 500
        # Controllers must classify this as gate failure, not disk_limit.
        klass = disk.classify_tool_failure(
            outcome=type(
                "O",
                (),
                {
                    "exit_code": 2,
                    "timed_out": False,
                    "cancelled": False,
                    "stdout": stdout,
                    "stderr": stderr,
                },
            )(),
            bytes_written=500,
            expected_min_write=2500,
            raised=None,
        )
        assert klass != "disk_limit"
    finally:
        _reap(proc)


def test_failure_cleanup_reaps_children(tmp_path: Path) -> None:
    limited = tmp_path / "n1"
    neighbor = tmp_path / "n2"
    limited.mkdir()
    neighbor.mkdir()
    (limited / "grow.py").write_text(
        disk.limited_neighbor_writer_script(grow_gate_wait_s=10.0),
        encoding="utf-8",
    )
    (neighbor / "ok.py").write_text(
        disk.healthy_neighbor_script(finish_gate_wait_s=10.0),
        encoding="utf-8",
    )
    procs: list[subprocess.Popen[bytes]] = []
    stop = threading.Event()

    def launch() -> None:
        procs.append(
            subprocess.Popen(
                [sys.executable, str(limited / "grow.py")],
                cwd="/",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        )
        procs.append(
            subprocess.Popen(
                [sys.executable, str(neighbor / "ok.py")],
                cwd="/",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        )
        stop.wait(timeout=2)

    t = threading.Thread(target=launch)
    t.start()
    t.join(timeout=3)
    assert len(procs) == 2
    assert _wait_file(limited / "ready", timeout=2.0)
    assert _wait_file(neighbor / "ready", timeout=2.0)
    # Simulate controller failure path: unblock + reap.
    (limited / "grow_gate").write_text("1\n", encoding="utf-8")
    (neighbor / "finish_gate").write_text("1\n", encoding="utf-8")
    stop.set()
    for proc in procs:
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _reap(proc)
    assert all(p.poll() is not None for p in procs)
    assert not t.is_alive()


def test_evaluate_neighbor_requires_handshake_fields() -> None:
    base = {
        "phase_error": None,
        "both_ready": True,
        "growth_released": True,
        "limited_below_ceiling_at_release": True,
        "failure_class_limited": "disk_limit",
        "limited_writer_started": True,
        "limited": 1,
        "limited_timed_out": False,
        "limited_recording_stopped": True,
        "neighbor": 0,
        "neighbor_timed_out": False,
        "neighbor_alive": True,
        "neighbor_concurrent": True,
        "neighbor_continued_after_limited": True,
        "neighbor_finish_released": True,
        "limited_heartbeat": 2,
        "limited_size": 4000,
        "neighbor_hb_at_limited_end": 3,
        "size_before_release": 500,
    }
    assert disk.evaluate_neighbor(base) is True
    for key in (
        "both_ready",
        "growth_released",
        "neighbor_continued_after_limited",
        "neighbor_finish_released",
        "limited_below_ceiling_at_release",
    ):
        weak = dict(base)
        weak[key] = False
        assert disk.evaluate_neighbor(weak) is False, key
