"""Trusted F2 disk probe run INSIDE the media-executor container.

Uses production LandlockRunner + job_process (real prctl/cgroup.kill/Landlock
identity-aware measurement). Limits are passed by this trusted controller —
NOT proof of untrusted RPC ceiling mapping (covered offline).

Fixture trust root: this file is mounted read-only at /opt/sec08-tests/.
Synthetic writers are /usr/bin/python3 -c only (no provider downloads).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path


def _runner():
    from fetchnow.media_executor.runner import LandlockRunner

    launcher = Path(os.environ["MEDIA_EXECUTOR_LAUNCHER"])
    return LandlockRunner(launcher)


def _run(
    runner,
    *,
    attempt: Path,
    argv: list[str],
    max_growth: int,
    max_tree: int,
    min_free: int | None,
    timeout: float = 8.0,
):
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


def case_fast_growth(work: Path) -> dict[str, object]:
    attempt = work / "growth"
    attempt.mkdir()
    out = attempt / "out.bin"
    argv = [
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(out)!r}).write_bytes(b'x'*8192)",
    ]
    # Sample interval is 200ms; this writer finishes in one shot before later
    # samples — final post-reap check must still fail.
    outcome = _run(
        _runner(),
        attempt=attempt,
        argv=argv,
        max_growth=1024,
        max_tree=100_000,
        min_free=None,
    )
    return {
        "name": "f2-disk-fast-growth-fail",
        "ok": outcome.exit_code == 1 and out.is_file() and out.stat().st_size == 8192,
        "exit_code": outcome.exit_code,
        "timed_out": outcome.timed_out,
        "size": out.stat().st_size if out.is_file() else 0,
    }


def case_fast_tree(work: Path) -> dict[str, object]:
    attempt = work / "tree"
    attempt.mkdir()
    (attempt / "seed").write_bytes(b"s" * 2000)
    out = attempt / "more.bin"
    argv = [
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(out)!r}).write_bytes(b'y'*2000)",
    ]
    outcome = _run(
        _runner(),
        attempt=attempt,
        argv=argv,
        max_growth=50_000,
        max_tree=3000,
        min_free=None,
    )
    return {
        "name": "f2-disk-fast-tree-fail",
        "ok": outcome.exit_code == 1,
        "exit_code": outcome.exit_code,
    }


def case_staged_inputs_ok(work: Path) -> dict[str, object]:
    attempt = work / "mux"
    attempt.mkdir()
    (attempt / "input-video").write_bytes(b"v" * 8000)
    (attempt / "input-audio").write_bytes(b"a" * 8000)
    out = attempt / "output-mux"
    argv = [
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(out)!r}).write_bytes(b'o'*100)",
    ]
    outcome = _run(
        _runner(),
        attempt=attempt,
        argv=argv,
        max_growth=1000,
        max_tree=20_000,
        min_free=None,
    )
    return {
        "name": "f2-disk-staged-inputs-success",
        "ok": outcome.exit_code == 0 and out.is_file(),
        "exit_code": outcome.exit_code,
    }


def case_prestart_refuse(work: Path) -> dict[str, object]:
    attempt = work / "prestart"
    attempt.mkdir()
    (attempt / "huge").write_bytes(b"h" * 5000)
    marker = attempt / "ran"
    argv = [
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(marker)!r}).write_text('x')",
    ]
    # LandlockRunner raises when job_process refuses pre-start (supervisor
    # non-zero). Treat raise or exit_code!=0 without marker as refuse.
    raised = False
    exit_code: int | None = None
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
    except OSError:
        raised = True
    return {
        "name": "f2-disk-prestart-refuse",
        "ok": (raised or exit_code == 1) and not marker.exists(),
        "raised": raised,
        "exit_code": exit_code,
        "marker_exists": marker.exists(),
    }


def case_long_writer_kill_reap(work: Path) -> dict[str, object]:
    attempt = work / "long"
    attempt.mkdir()
    script = (
        "import time\n"
        "from pathlib import Path\n"
        "p = Path('grow.bin')\n"
        "while True:\n"
        "    cur = p.read_bytes() if p.exists() else b''\n"
        "    p.write_bytes(cur + b'z' * 2000)\n"
        "    time.sleep(0.02)\n"
    )
    (attempt / "grow.py").write_text(script)
    t0 = time.monotonic()
    outcome = _run(
        _runner(),
        attempt=attempt,
        argv=[sys.executable, str(attempt / "grow.py")],
        max_growth=6_000,
        max_tree=100_000,
        min_free=None,
        timeout=10.0,
    )
    elapsed = time.monotonic() - t0
    grow = attempt / "grow.bin"
    size = grow.stat().st_size if grow.exists() else 0
    return {
        "name": "f2-disk-long-writer-kill-reap",
        "ok": outcome.exit_code == 1 and size <= 40_000 and elapsed < 9.0,
        "exit_code": outcome.exit_code,
        "size": size,
        "elapsed": elapsed,
        "timed_out": outcome.timed_out,
    }


def case_neighbor_continues(work: Path) -> dict[str, object]:
    a1 = work / "n1"
    a2 = work / "n2"
    a1.mkdir()
    a2.mkdir()
    (a1 / "grow.py").write_text(
        "import time\n"
        "from pathlib import Path\n"
        "p = Path('x')\n"
        "while True:\n"
        "    cur = p.read_bytes() if p.exists() else b''\n"
        "    p.write_bytes(cur + b'y' * 3000)\n"
        "    time.sleep(0.02)\n"
    )
    (a2 / "ok.py").write_text("print('ok')\n")
    results: dict[str, int | None] = {}
    runner = _runner()

    def limited() -> None:
        results["limited"] = _run(
            runner,
            attempt=a1,
            argv=[sys.executable, str(a1 / "grow.py")],
            max_growth=5_000,
            max_tree=50_000,
            min_free=None,
            timeout=8.0,
        ).exit_code

    def neighbor() -> None:
        time.sleep(0.15)
        results["neighbor"] = _run(
            runner,
            attempt=a2,
            argv=[sys.executable, str(a2 / "ok.py")],
            max_growth=1_000_000,
            max_tree=1_000_000,
            min_free=None,
            timeout=5.0,
        ).exit_code

    t1 = threading.Thread(target=limited)
    t2 = threading.Thread(target=neighbor)
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)
    return {
        "name": "f2-disk-neighbor-continues",
        "ok": results.get("limited") == 1 and results.get("neighbor") == 0,
        "limited": results.get("limited"),
        "neighbor": results.get("neighbor"),
    }


def main() -> int:
    default_work = "/var/lib/fetchnow/executor/disk-probe"
    work = Path(os.environ.get("DISK_PROBE_WORK", default_work))
    work.mkdir(parents=True, exist_ok=True)
    cases = [
        case_fast_growth(work),
        case_fast_tree(work),
        case_staged_inputs_ok(work),
        case_prestart_refuse(work),
        case_long_writer_kill_reap(work),
        case_neighbor_continues(work),
    ]
    payload = {
        "ok": all(bool(c.get("ok")) for c in cases),
        "cases": cases,
        "trust_boundary": (
            "trusted controller passes small limits to LandlockRunner; "
            "not RPC max_bytes mapping proof"
        ),
        "fixture_roots": ["/opt/sec08-tests/disk_native_inside.py"],
    }
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
