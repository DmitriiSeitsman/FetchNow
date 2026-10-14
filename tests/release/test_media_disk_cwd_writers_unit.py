"""Real cwd='/' execution for F2 synthetic writers (catches relative-path bugs)."""

from __future__ import annotations

import importlib.util
import signal
import subprocess
import sys
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


def _wait_file(path: Path, *, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and path.stat().st_size > 0:
            return True
        time.sleep(0.02)
    return False


def test_relative_long_writer_fails_under_cwd_root(tmp_path: Path) -> None:
    """Regression: Path('grow.bin') with cwd=/ does not write inside attempt."""
    attempt = tmp_path / "long-rel"
    attempt.mkdir()
    script = attempt / "grow.py"
    script.write_text(disk.long_writer_script(relative=True), encoding="utf-8")
    cwd_root_grow = Path("/grow.bin")
    existed_before = cwd_root_grow.exists()
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        cwd="/",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(0.35)
    attempt_grew = (attempt / "grow.bin").exists()
    stderr = b""
    try:
        # Relative writer must not populate the attempt artifact.
        assert not attempt_grew
        # Either PermissionError on /grow.bin or no usable attempt output.
        if proc.poll() is not None:
            _stdout, stderr = proc.communicate(timeout=1)
            text = stderr.decode("utf-8", errors="replace").lower()
            assert (
                "permission" in text
                or "errno 13" in text
                or "read-only" in text
                or not (attempt / "grow.bin").exists()
            )
        else:
            assert not (attempt / "grow.bin").exists()
    finally:
        _reap(proc)
        if not existed_before and cwd_root_grow.exists():
            # Do not leave stray root-cwd artifacts from the regression.
            try:
                cwd_root_grow.unlink()
            except OSError:
                pass


def test_long_writer_cwd_independent_writes_in_attempt(tmp_path: Path) -> None:
    attempt = tmp_path / "long-abs"
    attempt.mkdir()
    script = attempt / "grow.py"
    script.write_text(disk.long_writer_script(relative=False), encoding="utf-8")
    grow = attempt / "grow.bin"
    hb = attempt / "heartbeat"
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        cwd="/",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert _wait_file(grow, timeout=2.0)
        assert _wait_file(hb, timeout=2.0)
        size = grow.stat().st_size
        assert size >= 2000
        hb_val = int(hb.read_text(encoding="utf-8").strip())
        assert hb_val >= 1
        # Read existing output while cwd remains irrelevant to the controller.
        assert grow.read_bytes()[:1] == b"z"
        assert not Path("/grow.bin").exists() or Path("/grow.bin").stat().st_size == 0
    finally:
        _reap(proc)
    # Recording must stop after reap.
    size_after = grow.stat().st_size
    time.sleep(0.2)
    assert grow.stat().st_size == size_after


def test_limited_and_neighbor_writers_cwd_independent(tmp_path: Path) -> None:
    limited_dir = tmp_path / "n1"
    neighbor_dir = tmp_path / "n2"
    limited_dir.mkdir()
    neighbor_dir.mkdir()
    seed = disk.NEIGHBOR_LIMITED_SEED_BYTES
    (limited_dir / "grow.py").write_text(
        disk.limited_neighbor_writer_script(relative=False, seed_bytes=seed),
        encoding="utf-8",
    )
    (neighbor_dir / "ok.py").write_text(
        disk.healthy_neighbor_script(relative=False), encoding="utf-8"
    )
    limited = subprocess.Popen(
        [sys.executable, str(limited_dir / "grow.py")],
        cwd="/",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    neighbor = subprocess.Popen(
        [sys.executable, str(neighbor_dir / "ok.py")],
        cwd="/",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert _wait_file(limited_dir / "ready", timeout=2.0)
        assert _wait_file(neighbor_dir / "ready", timeout=2.0)
        assert _wait_file(neighbor_dir / "hb", timeout=2.0)
        assert (limited_dir / "x").stat().st_size == seed
        assert int((neighbor_dir / "hb").read_text(encoding="utf-8").strip()) >= 1
        assert not (tmp_path / "x").exists()
        (limited_dir / "grow_gate").write_text("1\n", encoding="utf-8")
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if (limited_dir / "x").stat().st_size > seed:
                break
            time.sleep(0.02)
        assert (limited_dir / "x").stat().st_size > seed
        _reap(limited)
        (neighbor_dir / "finish_gate").write_text("1\n", encoding="utf-8")
        neighbor.wait(timeout=3)
    finally:
        _reap(limited)
        _reap(neighbor)
    assert neighbor.returncode == 0
    final_hb = int((neighbor_dir / "hb").read_text(encoding="utf-8").strip())
    assert final_hb >= 1


def test_scripts_declare_base_from_file_not_cwd() -> None:
    abs_long = disk.long_writer_script(relative=False)
    abs_lim = disk.limited_neighbor_writer_script(relative=False)
    abs_nb = disk.healthy_neighbor_script(relative=False)
    for text in (abs_long, abs_lim, abs_nb):
        assert "Path(__file__).resolve().parent" in text
        assert "Path('grow.bin')" not in text
        assert "Path('heartbeat')" not in text
    assert "base / 'x'" in abs_lim
    assert "base / 'grow_gate'" in abs_lim
    assert "base / 'finish_gate'" in abs_nb
    assert "base / 'ready'" in abs_lim and "base / 'ready'" in abs_nb
    rel = disk.long_writer_script(relative=True)
    assert "Path('grow.bin')" in rel
    assert "Path(__file__)" not in rel


def test_evaluate_neighbor_requires_continuity_not_just_exits() -> None:
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
    weak = dict(base)
    weak["neighbor_continued_after_limited"] = False
    assert disk.evaluate_neighbor(weak) is False
    weak2 = dict(base)
    weak2["failure_class_limited"] = "dac_or_no_write"
    assert disk.evaluate_neighbor(weak2) is False
    weak3 = dict(base)
    weak3["limited_recording_stopped"] = False
    assert disk.evaluate_neighbor(weak3) is False


def test_fixture_has_no_cwd_relative_writer_literals() -> None:
    text = DISK_INSIDE.read_text(encoding="utf-8")
    assert "long_writer_script()" in text
    assert "limited_neighbor_writer_script(" in text
    assert "healthy_neighbor_script(" in text
    assert "grow_gate" in text and "finish_gate" in text
    assert "both_ready" in text
    assert text.count("p = Path('grow.bin')") == 1
    assert "base / 'grow.bin'" in text
    assert "base / 'grow_gate'" in text
    assert "base / 'finish_gate'" in text
