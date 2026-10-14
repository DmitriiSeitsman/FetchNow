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
    text = (
        raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    )
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
    script = (
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
    _write_as_worker(attempt / "grow.py", script.encode())
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
    ok = (
        failure_class == "disk_limit"
        and outcome is not None
        and outcome.exit_code == 1
        and not outcome.timed_out
        and size > 0
        and size <= 40_000
        and hb_value >= 1
        and recording_stopped
        and elapsed < 9.0
    )
    return {
        "name": "f2-disk-long-writer-kill-reap",
        "ok": ok,
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


def case_neighbor_continues(work: Path) -> dict[str, object]:
    a1, meta1 = make_attempt(work, "n1")
    a2, meta2 = make_attempt(work, "n2")
    _write_as_worker(
        a1 / "grow.py",
        (
            b"import time\n"
            b"from pathlib import Path\n"
            b"p = Path('x')\n"
            b"h = Path('hb')\n"
            b"n = 0\n"
            b"while True:\n"
            b"    cur = p.read_bytes() if p.exists() else b''\n"
            b"    p.write_bytes(cur + b'y' * 3000)\n"
            b"    n += 1\n"
            b"    h.write_text(str(n))\n"
            b"    time.sleep(0.02)\n"
        ),
    )
    _write_as_worker(
        a2 / "ok.py",
        (
            b"import time\n"
            b"from pathlib import Path\n"
            b"h = Path('hb')\n"
            b"for i in range(1, 6):\n"
            b"    h.write_text(str(i))\n"
            b"    time.sleep(0.05)\n"
            b"print('ok')\n"
        ),
    )
    results: dict[str, Any] = {}
    runner = _runner()

    def limited() -> None:
        try:
            results["limited_outcome"] = _run(
                runner,
                attempt=a1,
                argv=[sys.executable, str(a1 / "grow.py")],
                max_growth=5_000,
                max_tree=50_000,
                min_free=None,
                timeout=8.0,
            )
        except BaseException as exc:  # noqa: BLE001
            results["limited_error"] = type(exc).__name__

    def neighbor() -> None:
        time.sleep(0.15)
        results["neighbor_hb_before"] = _read_text(a2 / "hb").strip()
        try:
            results["neighbor_outcome"] = _run(
                runner,
                attempt=a2,
                argv=[sys.executable, str(a2 / "ok.py")],
                max_growth=1_000_000,
                max_tree=1_000_000,
                min_free=None,
                timeout=5.0,
            )
        except BaseException as exc:  # noqa: BLE001
            results["neighbor_error"] = type(exc).__name__
        results["neighbor_hb_after"] = _read_text(a2 / "hb").strip()

    t1 = threading.Thread(target=limited)
    t2 = threading.Thread(target=neighbor)
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)
    limited_out: ToolOutcome | None = results.get("limited_outcome")
    neighbor_out: ToolOutcome | None = results.get("neighbor_outcome")
    limited_size = max(0, _file_size(a1 / "x"))
    limited_hb_text = _read_text(a1 / "hb").strip()
    limited_hb = int(limited_hb_text) if limited_hb_text.isdigit() else 0
    neighbor_hb_after = str(results.get("neighbor_hb_after") or "")
    neighbor_alive = neighbor_hb_after.isdigit() and int(neighbor_hb_after) >= 1
    limited_class = classify_tool_failure(
        outcome=limited_out,
        bytes_written=limited_size,
        expected_min_write=3000,
        raised=None
        if "limited_error" not in results
        else OSError(results["limited_error"]),
    )
    ok = (
        limited_class == "disk_limit"
        and limited_out is not None
        and limited_out.exit_code == 1
        and neighbor_out is not None
        and neighbor_out.exit_code == 0
        and not neighbor_out.timed_out
        and neighbor_alive
        and limited_hb >= 1
    )
    return {
        "name": "f2-disk-neighbor-continues",
        "ok": ok,
        "attempt_limited": meta1,
        "attempt_neighbor": meta2,
        "limited": None if limited_out is None else limited_out.exit_code,
        "neighbor": None if neighbor_out is None else neighbor_out.exit_code,
        "limited_heartbeat": limited_hb,
        "limited_size": limited_size,
        "neighbor_hb_after": neighbor_hb_after,
        "neighbor_alive": neighbor_alive,
        "failure_class_limited": limited_class,
        "diag_limited": _outcome_diag(limited_out, error=results.get("limited_error")),
        "diag_neighbor": _outcome_diag(
            neighbor_out, error=results.get("neighbor_error")
        ),
    }


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
