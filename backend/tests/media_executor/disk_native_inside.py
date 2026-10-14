"""Trusted F2 disk probe run INSIDE the media-executor container.

Uses production LandlockRunner + job_process (real prctl/cgroup.kill/Landlock
identity-aware measurement). Attempt dirs are created with production layout
helpers (make_work_root / make_job_directory) — never root-owned Path.mkdir
alone, never chmod 0777 / CAP_DAC_OVERRIDE.

Limits are passed by this trusted controller — NOT proof of untrusted RPC
ceiling mapping (covered offline).

Fixture trust root: this file is mounted read-only at /opt/sec08-tests/.
Synthetic writers are /usr/bin/python3 -c only (no provider downloads).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from fetchnow.media_executor.constants import (
    JOB_DIR_MODE,
    TOOL_UID,
    WORK_ROOT_MODE,
    WORKER_GID,
    WORKER_UID,
)
from fetchnow.media_executor.layout import make_job_directory, make_work_root
from fetchnow.media_executor.runner import ToolOutcome

_SAFE_ERR = re.compile(
    r"(?:password|secret|token|postgres)\s*[:=]\s*\S+|http[s]?://\S+",
    re.I,
)


def _runner():
    from fetchnow.media_executor.runner import LandlockRunner

    launcher = Path(os.environ["MEDIA_EXECUTOR_LAUNCHER"])
    return LandlockRunner(launcher)


def _sanitize_text(raw: bytes | str, *, limit: int = 400) -> str:
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    text = _SAFE_ERR.sub("<redacted>", text)
    return text[:limit]


def _outcome_diag(
    outcome: ToolOutcome | None, *, error: str | None = None
) -> dict[str, Any]:
    if outcome is None:
        return {
            "exit_code": None,
            "timed_out": None,
            "cancelled": None,
            "stdout": "",
            "stderr": _sanitize_text(error or ""),
            "error": error,
        }
    return {
        "exit_code": outcome.exit_code,
        "timed_out": bool(outcome.timed_out),
        "cancelled": bool(outcome.cancelled),
        "stdout": _sanitize_text(outcome.stdout),
        "stderr": _sanitize_text(outcome.stderr),
        "error": error,
    }


def _path_identity(path: Path) -> dict[str, int]:
    """Stat path; prefer tool identity when CAP_DAC_OVERRIDE is absent."""
    try:
        st = path.stat()
        return {
            "uid": int(st.st_uid),
            "gid": int(st.st_gid),
            "mode": int(stat.S_IMODE(st.st_mode)),
        }
    except PermissionError:
        proc = _tool_helper(
            "import os, stat, sys; "
            "st = os.stat(sys.argv[1]); "
            "print(st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode))",
            str(path),
        )
        if proc.returncode != 0:
            raise
        uid_s, gid_s, mode_s = proc.stdout.decode().split()
        return {"uid": int(uid_s), "gid": int(gid_s), "mode": int(mode_s)}


def _write_as_worker(path: Path, data: bytes) -> None:
    """Stage inputs as worker group (JOB_DIR_MODE group-write). Fresh process."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; "
            "Path(sys.argv[1]).write_bytes(bytes.fromhex(sys.argv[2]))",
            str(path),
            data.hex(),
        ],
        user=WORKER_UID,
        group=WORKER_GID,
        extra_groups=[],
        capture_output=True,
        timeout=5,
        check=False,
    )
    if result.returncode != 0:
        raise OSError(
            "worker stage write failed: "
            + _sanitize_text(result.stderr or result.stdout or b"")
        )


def _write_as_tool(path: Path, data: bytes) -> None:
    """Write as TOOL_UID:WORKER_GID in a fresh process (no thread credential change)."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; "
            "Path(sys.argv[1]).write_bytes(bytes.fromhex(sys.argv[2]))",
            str(path),
            data.hex(),
        ],
        user=TOOL_UID,
        group=WORKER_GID,
        extra_groups=[],
        capture_output=True,
        timeout=5,
        check=False,
    )
    if result.returncode != 0:
        raise OSError(
            "tool uid write failed: "
            + _sanitize_text(result.stderr or result.stdout or b"")
        )


def _tool_helper(code: str, *args: str) -> subprocess.CompletedProcess[bytes]:
    """Run a short helper as TOOL_UID (no CAP_DAC_OVERRIDE required)."""
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        user=TOOL_UID,
        group=WORKER_GID,
        extra_groups=[],
        capture_output=True,
        timeout=5,
        check=False,
    )


def _file_size(path: Path) -> int:
    proc = _tool_helper(
        "from pathlib import Path; import sys; "
        "p = Path(sys.argv[1]); "
        "print(p.stat().st_size if p.is_file() else -1)",
        str(path),
    )
    if proc.returncode != 0:
        return -1
    text = proc.stdout.decode("ascii", errors="replace").strip()
    return int(text) if text.lstrip("-").isdigit() else -1


def _file_exists(path: Path) -> bool:
    proc = _tool_helper(
        "from pathlib import Path; import sys; "
        "raise SystemExit(0 if Path(sys.argv[1]).exists() else 1)",
        str(path),
    )
    return proc.returncode == 0


def _read_bytes(path: Path) -> bytes:
    proc = _tool_helper(
        "from pathlib import Path; import sys; "
        "sys.stdout.buffer.write(Path(sys.argv[1]).read_bytes())",
        str(path),
    )
    if proc.returncode != 0:
        return b""
    return proc.stdout


def _read_text(path: Path) -> str:
    return _read_bytes(path).decode("utf-8", errors="replace")


def _run(
    runner,
    *,
    attempt: Path,
    argv: list[str],
    max_growth: int,
    max_tree: int,
    min_free: int | None,
    timeout: float = 8.0,
) -> ToolOutcome:
    return runner.run(
        attempt=attempt,
        argv=argv,
        timeout_seconds=timeout,
        cancel=threading.Event(),
        protected=[],
        max_growth_bytes=max_growth,
        max_tree_bytes=max_tree,
        min_free_bytes=min_free,
    )


def classify_tool_failure(
    *,
    outcome: ToolOutcome | None,
    bytes_written: int,
    expected_min_write: int,
    raised: BaseException | None = None,
) -> str:
    """Distinguish DAC/launcher/timeout from disk rejection. Never invent PASS."""
    if raised is not None:
        text = f"{type(raised).__name__}:{raised}"
        if "Permission" in text or "EACCES" in text or "dac" in text.lower():
            return "dac_error"
        if "launcher" in text.lower() or "landlock" in text.lower():
            return "launcher_error"
        if "supervisor" in text.lower():
            return "prestart_or_supervisor"
        return "inconclusive"
    if outcome is None:
        return "inconclusive"
    stderr = _sanitize_text(outcome.stderr).lower()
    if outcome.timed_out:
        return "timeout"
    if "permission denied" in stderr or "eacces" in stderr:
        return "dac_error"
    if bytes_written < expected_min_write and outcome.exit_code not in {0}:
        # Non-success with no (or insufficient) writes is not disk enforcement.
        return "dac_or_no_write"
    if outcome.exit_code == 1 and bytes_written >= expected_min_write:
        return "disk_limit"
    if outcome.exit_code == 0:
        return "success"
    return "inconclusive"


def evaluate_fast_growth(evidence: dict[str, Any]) -> bool:
    return bool(
        evidence.get("ok_disk")
        and evidence.get("failure_class") == "disk_limit"
        and evidence.get("writer_started")
        and int(evidence.get("size") or 0) == int(evidence.get("expected_size") or -1)
        and evidence.get("exit_code") == 1
        and not evidence.get("timed_out")
    )


def evaluate_positive_control(evidence: dict[str, Any]) -> bool:
    return bool(
        evidence.get("layout_ok")
        and evidence.get("tool_marker_ok")
        and evidence.get("controller_read_ok")
        and evidence.get("launcher_exit_code") == 0
        and not evidence.get("timed_out")
        and evidence.get("failure_class") == "success"
    )


def evaluate_long_writer(evidence: dict[str, Any]) -> bool:
    return bool(
        evidence.get("failure_class") == "disk_limit"
        and evidence.get("writer_started")
        and evidence.get("exit_code") == 1
        and not evidence.get("timed_out")
        and int(evidence.get("size") or 0) > 0
        and int(evidence.get("size") or 0) <= 40_000
        and int(evidence.get("heartbeat") or 0) >= 1
        and evidence.get("recording_stopped")
        and float(evidence.get("elapsed") or 99.0) < 9.0
    )


def evaluate_neighbor(evidence: dict[str, Any]) -> bool:
    """Require handshake both-ready → release → disk stop → neighbor progress."""
    return bool(
        not evidence.get("phase_error")
        and evidence.get("both_ready")
        and evidence.get("growth_released")
        and evidence.get("limited_below_ceiling_at_release")
        and evidence.get("failure_class_limited") == "disk_limit"
        and evidence.get("limited_writer_started")
        and evidence.get("limited") == 1
        and not evidence.get("limited_timed_out")
        and evidence.get("limited_recording_stopped")
        and evidence.get("neighbor") == 0
        and not evidence.get("neighbor_timed_out")
        and evidence.get("neighbor_alive")
        and evidence.get("neighbor_concurrent")
        and evidence.get("neighbor_continued_after_limited")
        and evidence.get("neighbor_finish_released")
        and int(evidence.get("limited_heartbeat") or 0) >= 1
        and int(evidence.get("limited_size") or 0) > 0
        and int(evidence.get("neighbor_hb_at_limited_end") or 0) >= 1
        and int(evidence.get("size_before_release") or -1) >= 0
    )


def _attempt_base_preamble() -> str:
    """Absolute attempt dir for scripts launched by absolute filename (cwd may be /)."""
    return "from pathlib import Path\nbase = Path(__file__).resolve().parent\n"


def long_writer_script(*, relative: bool = False) -> str:
    """Growing writer for F2 long-writer. relative=True is the known-bad form."""
    if relative:
        return (
            "import time\n"
            "from pathlib import Path\n"
            "p = Path('grow.bin')\n"
            "h = Path('heartbeat')\n"
            "n = 0\n"
            "while True:\n"
            "    cur = p.read_bytes() if p.exists() else b''\n"
            "    p.write_bytes(cur + b'z' * 2000)\n"
            "    n += 1\n"
            "    h.write_text(str(n))\n"
            "    time.sleep(0.02)\n"
        )
    return (
        "import time\n" + _attempt_base_preamble() + "p = base / 'grow.bin'\n"
        "h = base / 'heartbeat'\n"
        "n = 0\n"
        "while True:\n"
        "    cur = p.read_bytes() if p.exists() else b''\n"
        "    p.write_bytes(cur + b'z' * 2000)\n"
        "    n += 1\n"
        "    h.write_text(str(n))\n"
        "    time.sleep(0.02)\n"
    )


def limited_neighbor_writer_script(
    *,
    relative: bool = False,
    chunk: int = 2000,
    sleep_s: float = 0.05,
    seed_bytes: int = 500,
    ready_delay_s: float = 0.0,
    grow_gate_wait_s: float = 25.0,
) -> str:
    """Limited writer: ready+seed, wait own grow_gate, then grow until killed."""
    if relative:
        return (
            "import time\n"
            "from pathlib import Path\n"
            "p = Path('x')\n"
            "h = Path('hb')\n"
            "ready = Path('ready')\n"
            "gate = Path('grow_gate')\n"
            f"time.sleep({ready_delay_s})\n"
            f"p.write_bytes(b'y' * {seed_bytes})\n"
            "h.write_text('0')\n"
            "ready.write_text('1')\n"
            "t0 = time.monotonic()\n"
            "while not gate.is_file():\n"
            f"    if time.monotonic() - t0 > {grow_gate_wait_s}:\n"
            "        print('grow_gate_timeout', file=__import__('sys').stderr)\n"
            "        raise SystemExit(2)\n"
            "    time.sleep(0.02)\n"
            "n = 0\n"
            "while True:\n"
            "    cur = p.read_bytes() if p.exists() else b''\n"
            f"    p.write_bytes(cur + b'y' * {chunk})\n"
            "    n += 1\n"
            "    h.write_text(str(n))\n"
            f"    time.sleep({sleep_s})\n"
        )
    return (
        "import time\n" + _attempt_base_preamble() + "p = base / 'x'\n"
        "h = base / 'hb'\n"
        "ready = base / 'ready'\n"
        "gate = base / 'grow_gate'\n"
        f"time.sleep({ready_delay_s})\n"
        f"p.write_bytes(b'y' * {seed_bytes})\n"
        "h.write_text('0')\n"
        "ready.write_text('1')\n"
        "t0 = time.monotonic()\n"
        "while not gate.is_file():\n"
        f"    if time.monotonic() - t0 > {grow_gate_wait_s}:\n"
        "        print('grow_gate_timeout', file=__import__('sys').stderr)\n"
        "        raise SystemExit(2)\n"
        "    time.sleep(0.02)\n"
        "n = 0\n"
        "while True:\n"
        "    cur = p.read_bytes() if p.exists() else b''\n"
        f"    p.write_bytes(cur + b'y' * {chunk})\n"
        "    n += 1\n"
        "    h.write_text(str(n))\n"
        f"    time.sleep({sleep_s})\n"
    )


def healthy_neighbor_script(
    *,
    relative: bool = False,
    ready_delay_s: float = 0.0,
    hb_sleep_s: float = 0.05,
    finish_gate_wait_s: float = 30.0,
) -> str:
    """Neighbor: ready+heartbeat on own files; exit only after own finish_gate."""
    if relative:
        return (
            "import time\n"
            "from pathlib import Path\n"
            "h = Path('hb')\n"
            "ready = Path('ready')\n"
            "gate = Path('finish_gate')\n"
            f"time.sleep({ready_delay_s})\n"
            "ready.write_text('1')\n"
            "n = 0\n"
            "t0 = time.monotonic()\n"
            "while not gate.is_file():\n"
            "    n += 1\n"
            "    h.write_text(str(n))\n"
            f"    if time.monotonic() - t0 > {finish_gate_wait_s}:\n"
            "        print('finish_gate_timeout', file=__import__('sys').stderr)\n"
            "        raise SystemExit(2)\n"
            f"    time.sleep({hb_sleep_s})\n"
            "print('ok')\n"
        )
    return (
        "import time\n" + _attempt_base_preamble() + "h = base / 'hb'\n"
        "ready = base / 'ready'\n"
        "gate = base / 'finish_gate'\n"
        f"time.sleep({ready_delay_s})\n"
        "ready.write_text('1')\n"
        "n = 0\n"
        "t0 = time.monotonic()\n"
        "while not gate.is_file():\n"
        "    n += 1\n"
        "    h.write_text(str(n))\n"
        f"    if time.monotonic() - t0 > {finish_gate_wait_s}:\n"
        "        print('finish_gate_timeout', file=__import__('sys').stderr)\n"
        "        raise SystemExit(2)\n"
        f"    time.sleep({hb_sleep_s})\n"
        "print('ok')\n"
    )


# Neighbor handshake ceilings (fixture-local; not production RPC ceilings).
NEIGHBOR_LIMITED_MAX_GROWTH = 10_000
NEIGHBOR_LIMITED_SEED_BYTES = 500
NEIGHBOR_PHASE_LIMITED_READY_S = 8.0
NEIGHBOR_PHASE_NEIGHBOR_READY_S = 8.0
NEIGHBOR_PHASE_GROWTH_S = 12.0
NEIGHBOR_PHASE_CONTINUITY_S = 5.0
NEIGHBOR_PHASE_FINISH_S = 8.0
NEIGHBOR_PHASE_JOIN_S = 5.0


def _hb_int(path: Path) -> int:
    text = _read_text(path).strip()
    return int(text) if text.isdigit() else 0


def _wait_phase(
    *,
    phase: str,
    deadline_s: float,
    predicate,
    sleep_s: float = 0.02,
) -> str | None:
    """Return None on success, or phase_error name on timeout."""
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if predicate():
            return None
        time.sleep(sleep_s)
    return f"{phase}_timeout"


def prepare_work_root(executor_root: Path, work: Path) -> dict[str, Any]:
    """Production sticky work root + probe subdirectory (sticky, not 0777)."""
    make_work_root(executor_root)
    if not work.exists():
        work.mkdir(mode=WORK_ROOT_MODE)
    os.chmod(work, WORK_ROOT_MODE)
    root_id = _path_identity(executor_root)
    work_id = _path_identity(work)
    layout_ok = (
        root_id["mode"] == WORK_ROOT_MODE
        and work_id["mode"] == WORK_ROOT_MODE
        and os.access(work, os.W_OK)
    )
    return {
        "executor_root": root_id,
        "work": work_id,
        "layout_ok": layout_ok,
    }


def make_attempt(work: Path, name: str) -> tuple[Path, dict[str, Any]]:
    attempt = work / name
    make_job_directory(attempt)
    identity = _path_identity(attempt)
    ok = (
        identity["uid"] == TOOL_UID
        and identity["gid"] == WORKER_GID
        and identity["mode"] == JOB_DIR_MODE
    )
    return attempt, {"identity": identity, "ok": ok}


def case_positive_control(work: Path) -> dict[str, object]:
    """Must PASS before any negative disk case is trusted."""
    attempt, attempt_meta = make_attempt(work, "positive")
    marker = attempt / "marker.txt"
    expected = b"dac-ok\n"
    # Prove tool uid can write via the real launcher (generous ceilings).
    raised: BaseException | None = None
    outcome: ToolOutcome | None = None
    try:
        outcome = _run(
            _runner(),
            attempt=attempt,
            argv=[
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    f"Path({str(marker)!r}).write_bytes({expected!r})"
                ),
            ],
            max_growth=100_000,
            max_tree=100_000,
            min_free=None,
            timeout=8.0,
        )
    except BaseException as exc:  # noqa: BLE001 — classify, never hide
        raised = exc
    size = max(0, _file_size(marker))
    controller_read = _read_bytes(marker)
    controller_read_ok = controller_read == expected
    failure_class = classify_tool_failure(
        outcome=outcome,
        bytes_written=size,
        expected_min_write=len(expected),
        raised=raised,
    )
    evidence: dict[str, object] = {
        "name": "f2-disk-positive-control",
        "layout_ok": bool(attempt_meta["ok"]),
        "attempt": attempt_meta,
        "tool_marker_ok": size == len(expected) and controller_read_ok,
        "controller_read_ok": controller_read_ok,
        "expected_size": len(expected),
        "size": size,
        "failure_class": failure_class,
        "launcher_exit_code": None if outcome is None else outcome.exit_code,
        "timed_out": None if outcome is None else outcome.timed_out,
        "diag": _outcome_diag(
            outcome, error=None if raised is None else type(raised).__name__
        ),
    }
    evidence["ok"] = evaluate_positive_control(evidence)
    return evidence


def case_fast_growth(work: Path) -> dict[str, object]:
    attempt, attempt_meta = make_attempt(work, "growth")
    out = attempt / "out.bin"
    expected = 8192
    argv = [
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(out)!r}).write_bytes(b'x'*{expected})",
    ]
    raised: BaseException | None = None
    outcome: ToolOutcome | None = None
    try:
        outcome = _run(
            _runner(),
            attempt=attempt,
            argv=argv,
            max_growth=1024,
            max_tree=100_000,
            min_free=None,
        )
    except BaseException as exc:  # noqa: BLE001
        raised = exc
    size = max(0, _file_size(out))
    failure_class = classify_tool_failure(
        outcome=outcome,
        bytes_written=size,
        expected_min_write=expected,
        raised=raised,
    )
    evidence: dict[str, object] = {
        "name": "f2-disk-fast-growth-fail",
        "attempt": attempt_meta,
        "writer_started": size >= expected,
        "expected_size": expected,
        "size": size,
        "exit_code": None if outcome is None else outcome.exit_code,
        "timed_out": None if outcome is None else outcome.timed_out,
        "failure_class": failure_class,
        "ok_disk": failure_class == "disk_limit",
        "diag": _outcome_diag(
            outcome, error=None if raised is None else type(raised).__name__
        ),
    }
    evidence["ok"] = evaluate_fast_growth(evidence)
    return evidence


def case_fast_tree(work: Path) -> dict[str, object]:
    attempt, attempt_meta = make_attempt(work, "tree")
    seed = attempt / "seed"
    _write_as_worker(seed, b"s" * 2000)
    out = attempt / "more.bin"
    expected = 2000
    argv = [
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(out)!r}).write_bytes(b'y'*{expected})",
    ]
    raised: BaseException | None = None
    outcome: ToolOutcome | None = None
    try:
        outcome = _run(
            _runner(),
            attempt=attempt,
            argv=argv,
            max_growth=50_000,
            max_tree=3000,
            min_free=None,
        )
    except BaseException as exc:  # noqa: BLE001
        raised = exc
    size = max(0, _file_size(out))
    seed_size = max(0, _file_size(seed))
    failure_class = classify_tool_failure(
        outcome=outcome,
        bytes_written=size,
        expected_min_write=expected,
        raised=raised,
    )
    ok = (
        failure_class == "disk_limit"
        and size == expected
        and (outcome is not None and outcome.exit_code == 1)
        and not (outcome.timed_out if outcome else True)
        and seed_size == 2000
    )
    return {
        "name": "f2-disk-fast-tree-fail",
        "ok": ok,
        "attempt": attempt_meta,
        "writer_started": size >= expected,
        "size": size,
        "seed_size": seed_size,
        "exit_code": None if outcome is None else outcome.exit_code,
        "timed_out": None if outcome is None else outcome.timed_out,
        "failure_class": failure_class,
        "diag": _outcome_diag(
            outcome, error=None if raised is None else type(raised).__name__
        ),
    }


def case_staged_inputs_ok(work: Path) -> dict[str, object]:
    attempt, attempt_meta = make_attempt(work, "mux")
    _write_as_worker(attempt / "input-video", b"v" * 8000)
    _write_as_worker(attempt / "input-audio", b"a" * 8000)
    out = attempt / "output-mux"
    expected = b"o" * 100
    argv = [
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(out)!r}).write_bytes({expected!r})",
    ]
    raised: BaseException | None = None
    outcome: ToolOutcome | None = None
    try:
        outcome = _run(
            _runner(),
            attempt=attempt,
            argv=argv,
            max_growth=1000,
            max_tree=20_000,
            min_free=None,
        )
    except BaseException as exc:  # noqa: BLE001
        raised = exc
    size = max(0, _file_size(out))
    content_ok = _file_exists(out) and _read_bytes(out) == expected
    failure_class = classify_tool_failure(
        outcome=outcome,
        bytes_written=size,
        expected_min_write=len(expected),
        raised=raised,
    )
    ok = (
        failure_class == "success"
        and outcome is not None
        and outcome.exit_code == 0
        and content_ok
        and not outcome.timed_out
    )
    return {
        "name": "f2-disk-staged-inputs-success",
        "ok": ok,
        "attempt": attempt_meta,
        "size": size,
        "content_ok": content_ok,
        "exit_code": None if outcome is None else outcome.exit_code,
        "timed_out": None if outcome is None else outcome.timed_out,
        "failure_class": failure_class,
        "diag": _outcome_diag(
            outcome, error=None if raised is None else type(raised).__name__
        ),
    }


def case_prestart_refuse(work: Path) -> dict[str, object]:
    attempt, attempt_meta = make_attempt(work, "prestart")
    _write_as_worker(attempt / "huge", b"h" * 5000)
    marker = attempt / "ran"
    argv = [
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(marker)!r}).write_text('x')",
    ]
    raised = False
    raised_type: str | None = None
    exit_code: int | None = None
    timed_out = False
    outcome: ToolOutcome | None = None
    try:
        outcome = _run(
            _runner(),
            attempt=attempt,
            argv=argv,
            max_growth=100_000,
            max_tree=1000,
            min_free=None,
        )
        exit_code = outcome.exit_code
        timed_out = bool(outcome.timed_out)
    except OSError as exc:
        raised = True
        raised_type = type(exc).__name__
    setup_ok = bool(attempt_meta["ok"]) and max(0, _file_size(attempt / "huge")) == 5000
    tool_started = _file_exists(marker)
    # Pre-start refuse: setup good, initial tree already over limit, tool never ran.
    ok = setup_ok and not tool_started and not timed_out and (raised or exit_code == 1)
    return {
        "name": "f2-disk-prestart-refuse",
        "ok": ok,
        "attempt": attempt_meta,
        "setup_ok": setup_ok,
        "raised": raised,
        "raised_type": raised_type,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "marker_exists": tool_started,
        "failure_class": "prestart_refuse" if ok else "inconclusive",
        "diag": _outcome_diag(outcome, error=raised_type),
    }


def case_long_writer_kill_reap(work: Path) -> dict[str, object]:
    attempt, attempt_meta = make_attempt(work, "long")
    _write_as_worker(attempt / "grow.py", long_writer_script().encode())
    t0 = time.monotonic()
    raised: BaseException | None = None
    outcome: ToolOutcome | None = None
    try:
        outcome = _run(
            _runner(),
            attempt=attempt,
            argv=[sys.executable, str(attempt / "grow.py")],
            max_growth=6_000,
            max_tree=100_000,
            min_free=None,
            timeout=10.0,
        )
    except BaseException as exc:  # noqa: BLE001
        raised = exc
    elapsed = time.monotonic() - t0
    grow = attempt / "grow.bin"
    hb = attempt / "heartbeat"
    size = max(0, _file_size(grow))
    hb_text = _read_text(hb).strip()
    hb_value = int(hb_text) if hb_text.isdigit() else 0
    size_after = max(0, _file_size(grow))
    time.sleep(0.15)
    size_stable = max(0, _file_size(grow))
    recording_stopped = size_stable == size_after
    failure_class = classify_tool_failure(
        outcome=outcome,
        bytes_written=size,
        expected_min_write=2000,
        raised=raised,
    )
    evidence: dict[str, object] = {
        "name": "f2-disk-long-writer-kill-reap",
        "attempt": attempt_meta,
        "writer_started": size > 0 and hb_value >= 1,
        "heartbeat": hb_value,
        "size": size,
        "recording_stopped": recording_stopped,
        "exit_code": None if outcome is None else outcome.exit_code,
        "elapsed": elapsed,
        "timed_out": None if outcome is None else outcome.timed_out,
        "failure_class": failure_class,
        "diag": _outcome_diag(
            outcome, error=None if raised is None else type(raised).__name__
        ),
    }
    evidence["ok"] = evaluate_long_writer(evidence)
    return evidence


def case_neighbor_continues(work: Path) -> dict[str, object]:
    """Deterministic handshake: both ready → release growth → disk stop → continue."""
    a1, meta1 = make_attempt(work, "n1")
    a2, meta2 = make_attempt(work, "n2")
    _write_as_worker(
        a1 / "grow.py",
        limited_neighbor_writer_script(seed_bytes=NEIGHBOR_LIMITED_SEED_BYTES).encode(),
    )
    _write_as_worker(a2 / "ok.py", healthy_neighbor_script().encode())
    results: dict[str, Any] = {
        "phases": [],
        "phase_error": None,
        "timestamps": {},
    }
    runner = _runner()
    ceiling = NEIGHBOR_LIMITED_MAX_GROWTH

    def _mark(phase: str) -> None:
        results["phases"].append(phase)
        results["timestamps"][phase] = time.monotonic()

    def _unblock_gates() -> None:
        # Best-effort: allow waiting tools to exit on any controller path.
        with contextlib.suppress(OSError):
            _write_as_worker(a1 / "grow_gate", b"1\n")
        with contextlib.suppress(OSError):
            _write_as_worker(a2 / "finish_gate", b"1\n")

    def limited() -> None:
        try:
            results["limited_outcome"] = _run(
                runner,
                attempt=a1,
                argv=[sys.executable, str(a1 / "grow.py")],
                max_growth=ceiling,
                max_tree=50_000,
                min_free=None,
                timeout=25.0,
            )
        except BaseException as exc:  # noqa: BLE001
            results["limited_error"] = type(exc).__name__
        finally:
            results["neighbor_hb_at_limited_end"] = _hb_int(a2 / "hb")
            results["limited_end_mono"] = time.monotonic()
            _mark("limited_end")

    def neighbor() -> None:
        try:
            results["neighbor_outcome"] = _run(
                runner,
                attempt=a2,
                argv=[sys.executable, str(a2 / "ok.py")],
                max_growth=1_000_000,
                max_tree=1_000_000,
                min_free=None,
                timeout=35.0,
            )
        except BaseException as exc:  # noqa: BLE001
            results["neighbor_error"] = type(exc).__name__
        results["neighbor_hb_after"] = _hb_int(a2 / "hb")
        _mark("neighbor_end")

    t1 = threading.Thread(target=limited, name="f2-limited")
    t2 = threading.Thread(target=neighbor, name="f2-neighbor")
    _mark("tools_launch")
    t1.start()
    t2.start()

    phase_error = _wait_phase(
        phase="limited_readiness",
        deadline_s=NEIGHBOR_PHASE_LIMITED_READY_S,
        predicate=lambda: _file_exists(a1 / "ready")
        and max(0, _file_size(a1 / "x")) == NEIGHBOR_LIMITED_SEED_BYTES,
    )
    if phase_error is None:
        _mark("limited_ready")
        results["limited_ready_marker"] = True
        results["size_at_limited_ready"] = max(0, _file_size(a1 / "x"))
    else:
        results["phase_error"] = phase_error

    if results["phase_error"] is None:
        phase_error = _wait_phase(
            phase="neighbor_readiness",
            deadline_s=NEIGHBOR_PHASE_NEIGHBOR_READY_S,
            predicate=lambda: _file_exists(a2 / "ready") and _hb_int(a2 / "hb") >= 1,
        )
        if phase_error is None:
            _mark("neighbor_ready")
            results["neighbor_ready_marker"] = True
            results["neighbor_hb_at_ready"] = _hb_int(a2 / "hb")
        else:
            results["phase_error"] = phase_error

    both_ready = (
        results["phase_error"] is None
        and bool(results.get("limited_ready_marker"))
        and bool(results.get("neighbor_ready_marker"))
        and t1.is_alive()
        and "limited_outcome" not in results
    )
    size_before_release = max(0, _file_size(a1 / "x"))
    below_ceiling = size_before_release < ceiling
    results["both_ready"] = both_ready
    results["size_before_release"] = size_before_release
    results["limited_below_ceiling_at_release"] = below_ceiling
    results["neighbor_hb_before_release"] = _hb_int(a2 / "hb")

    growth_released = False
    if both_ready and below_ceiling:
        try:
            _write_as_worker(a1 / "grow_gate", b"1\n")
            # Prove tool can observe its own gate (identity-aware exists check).
            if not _file_exists(a1 / "grow_gate"):
                results["phase_error"] = "grow_gate_not_visible_to_tool"
            else:
                growth_released = True
                _mark("growth_release")
        except OSError as exc:
            results["phase_error"] = f"grow_gate_write:{type(exc).__name__}"
    elif results["phase_error"] is None:
        results["phase_error"] = (
            "pre_release_invariant" if both_ready else "both_ready_failed"
        )

    results["growth_released"] = growth_released

    if growth_released and results["phase_error"] is None:
        t1.join(timeout=NEIGHBOR_PHASE_GROWTH_S)
        if t1.is_alive() or "limited_outcome" not in results:
            results["phase_error"] = "controlled_growth_timeout"
        else:
            _mark("growth_observed")

    continued = False
    neighbor_concurrent = False
    if results["phase_error"] is None:
        hb_at_end = int(results.get("neighbor_hb_at_limited_end") or 0)
        neighbor_concurrent = hb_at_end >= 1 and bool(results.get("both_ready"))
        results["neighbor_concurrent"] = neighbor_concurrent
        if not neighbor_concurrent:
            results["phase_error"] = "neighbor_not_alive_at_limited_end"
        else:
            phase_error = _wait_phase(
                phase="neighbor_continuity",
                deadline_s=NEIGHBOR_PHASE_CONTINUITY_S,
                predicate=lambda: _hb_int(a2 / "hb") > hb_at_end,
                sleep_s=0.05,
            )
            if phase_error is None:
                continued = True
                results["neighbor_hb_after_limited_growth"] = _hb_int(a2 / "hb")
                _mark("neighbor_continued")
            else:
                results["phase_error"] = phase_error
    results["neighbor_continued_after_limited"] = continued

    neighbor_finish_released = False
    try:
        if continued and results["phase_error"] is None:
            _write_as_worker(a2 / "finish_gate", b"1\n")
            if _file_exists(a2 / "finish_gate"):
                neighbor_finish_released = True
                _mark("neighbor_finish_release")
            else:
                results["phase_error"] = "finish_gate_not_visible_to_tool"
        else:
            _unblock_gates()
    except OSError as exc:
        results["phase_error"] = results["phase_error"] or (
            f"finish_gate_write:{type(exc).__name__}"
        )
        _unblock_gates()
    results["neighbor_finish_released"] = neighbor_finish_released

    if neighbor_finish_released and results["phase_error"] is None:
        t2.join(timeout=NEIGHBOR_PHASE_FINISH_S)
        if t2.is_alive():
            results["phase_error"] = "neighbor_finish_timeout"
    else:
        t2.join(timeout=NEIGHBOR_PHASE_JOIN_S)
    t1.join(timeout=NEIGHBOR_PHASE_JOIN_S)
    if t1.is_alive() or t2.is_alive():
        results["phase_error"] = results["phase_error"] or "join_timeout"
        _unblock_gates()
        t1.join(timeout=2.0)
        t2.join(timeout=2.0)

    limited_out: ToolOutcome | None = results.get("limited_outcome")
    neighbor_out: ToolOutcome | None = results.get("neighbor_outcome")
    limited_size = max(0, _file_size(a1 / "x"))
    limited_hb = _hb_int(a1 / "hb")
    size_after = limited_size
    time.sleep(0.15)
    size_stable = max(0, _file_size(a1 / "x"))
    limited_recording_stopped = size_stable == size_after
    neighbor_hb_after = int(results.get("neighbor_hb_after") or _hb_int(a2 / "hb"))
    neighbor_alive = neighbor_hb_after >= 1

    # Gate/Python timeouts must not be classified as disk_limit PASS.
    stderr_blob = ""
    if limited_out is not None:
        stderr_blob = _sanitize_text(limited_out.stderr).lower()
    if "grow_gate_timeout" in stderr_blob or results.get("limited_error") == "Timeout":
        limited_class = "gate_or_timeout"
    else:
        limited_class = classify_tool_failure(
            outcome=limited_out,
            bytes_written=limited_size,
            expected_min_write=max(NEIGHBOR_LIMITED_SEED_BYTES + 2000, 2000),
            raised=None
            if "limited_error" not in results
            else OSError(results["limited_error"]),
        )

    ts = results.get("timestamps") or {}
    ordering_ok = True
    order = [
        "tools_launch",
        "limited_ready",
        "neighbor_ready",
        "growth_release",
        "limited_end",
        "neighbor_continued",
        "neighbor_finish_release",
        "neighbor_end",
    ]
    prev = -1.0
    seen_order: list[str] = []
    for name in order:
        if name in ts:
            if float(ts[name]) < prev:
                ordering_ok = False
            prev = float(ts[name])
            seen_order.append(name)
    if results["phase_error"] is None and not ordering_ok:
        results["phase_error"] = "phase_ordering"

    evidence: dict[str, object] = {
        "name": "f2-disk-neighbor-continues",
        "attempt_limited": meta1,
        "attempt_neighbor": meta2,
        "limited": None if limited_out is None else limited_out.exit_code,
        "neighbor": None if neighbor_out is None else neighbor_out.exit_code,
        "limited_timed_out": None if limited_out is None else limited_out.timed_out,
        "neighbor_timed_out": None if neighbor_out is None else neighbor_out.timed_out,
        "limited_writer_started": limited_size > NEIGHBOR_LIMITED_SEED_BYTES
        and limited_hb >= 1,
        "limited_heartbeat": limited_hb,
        "limited_size": limited_size,
        "limited_recording_stopped": limited_recording_stopped,
        "neighbor_hb_at_limited_end": results.get("neighbor_hb_at_limited_end"),
        "neighbor_hb_after": neighbor_hb_after,
        "neighbor_hb_after_limited_growth": results.get(
            "neighbor_hb_after_limited_growth"
        ),
        "neighbor_hb_before_release": results.get("neighbor_hb_before_release"),
        "neighbor_alive": neighbor_alive,
        "neighbor_concurrent": bool(results.get("neighbor_concurrent")),
        "neighbor_continued_after_limited": continued,
        "both_ready": bool(results.get("both_ready")),
        "growth_released": growth_released,
        "neighbor_finish_released": neighbor_finish_released,
        "size_before_release": size_before_release,
        "limited_below_ceiling_at_release": below_ceiling,
        "phase_error": results.get("phase_error"),
        "phases": list(results.get("phases") or []),
        "phase_order": seen_order,
        "timestamps": dict(ts),
        "failure_class_limited": limited_class,
        "diag_limited": _outcome_diag(limited_out, error=results.get("limited_error")),
        "diag_neighbor": _outcome_diag(
            neighbor_out, error=results.get("neighbor_error")
        ),
    }
    evidence["ok"] = evaluate_neighbor(evidence)
    return evidence


def main() -> int:
    executor_root = Path(
        os.environ.get("MEDIA_EXECUTOR_WORK_ROOT", "/var/lib/fetchnow/executor")
    )
    work = Path(os.environ.get("DISK_PROBE_WORK", str(executor_root / "disk-probe")))
    layout = prepare_work_root(executor_root, work)
    cases: list[dict[str, object]] = [
        {
            "name": "f2-disk-layout-prepare",
            "ok": bool(layout["layout_ok"]),
            "layout": layout,
        }
    ]
    if not layout["layout_ok"]:
        payload = {
            "ok": False,
            "cases": cases,
            "trust_boundary": (
                "trusted controller passes small limits to LandlockRunner; "
                "not RPC max_bytes mapping proof"
            ),
            "fixture_roots": ["/opt/sec08-tests/disk_native_inside.py"],
            "positive_control_gate": False,
        }
        print(json.dumps(payload, sort_keys=True))
        return 1

    positive = case_positive_control(work)
    cases.append(positive)
    positive_ok = bool(positive.get("ok"))
    if positive_ok:
        cases.extend(
            [
                case_fast_growth(work),
                case_fast_tree(work),
                case_staged_inputs_ok(work),
                case_prestart_refuse(work),
                case_long_writer_kill_reap(work),
                case_neighbor_continues(work),
            ]
        )
    else:
        # Negative disk cases must not be declared PASS without positive control.
        for name in (
            "f2-disk-fast-growth-fail",
            "f2-disk-fast-tree-fail",
            "f2-disk-staged-inputs-success",
            "f2-disk-prestart-refuse",
            "f2-disk-long-writer-kill-reap",
            "f2-disk-neighbor-continues",
        ):
            cases.append(
                {
                    "name": name,
                    "ok": False,
                    "status": "NOT_RUN",
                    "reason": "positive_control_failed",
                }
            )

    payload = {
        "ok": positive_ok and all(bool(c.get("ok")) for c in cases),
        "cases": cases,
        "positive_control_gate": positive_ok,
        "trust_boundary": (
            "trusted controller passes small limits to LandlockRunner; "
            "not RPC max_bytes mapping proof"
        ),
        "fixture_roots": ["/opt/sec08-tests/disk_native_inside.py"],
        "image_platform": f"{os.uname().sysname}/{os.uname().machine}",
    }
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
