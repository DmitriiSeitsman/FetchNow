#!/usr/bin/env python3
"""Run pytest under a wall-clock budget with process-group cleanup (SEC-01).

Usage:
  PYTHONPATH=scripts python3 scripts/run_pytest_bounded.py -- \\
      -q tests/release/test_bounded_subprocess_unit.py

Environment:
  FETCHNOW_PYTEST_WALL_SECONDS  overall budget (default 600)

Signals:
  SIGTERM/SIGINT on this wrapper cancel the managed pytest process group via
  CancelToken (does not signal the parent agent session).
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from fetchnow_release.bounded_subprocess import (  # noqa: E402
    BoundedCancelledError,
    BoundedTimeoutError,
    CancelToken,
    run_bounded,
)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--":
        args = args[1:]
    wall_raw = os.environ.get("FETCHNOW_PYTEST_WALL_SECONDS", "600")
    try:
        wall = float(wall_raw)
    except ValueError:
        print(
            f"ERROR: invalid FETCHNOW_PYTEST_WALL_SECONDS={wall_raw!r}",
            file=sys.stderr,
        )
        return 2
    if wall <= 0:
        print(
            f"ERROR: FETCHNOW_PYTEST_WALL_SECONDS must be positive, got {wall}",
            file=sys.stderr,
        )
        return 2

    py = sys.executable
    cmd = [py, "-m", "pytest", *args]
    cancel = CancelToken()

    def _on_signal(signum: int, _frame: object) -> None:
        cancel.cancel()

    previous_term = signal.signal(signal.SIGTERM, _on_signal)
    previous_int = signal.signal(signal.SIGINT, _on_signal)
    try:
        try:
            result = run_bounded(
                cmd,
                timeout=wall,
                cwd=ROOT,
                max_stdout_bytes=16 * 1024 * 1024,
                max_stderr_bytes=16 * 1024 * 1024,
                cancel_token=cancel,
            )
        except BoundedTimeoutError:
            print(
                f"ERROR: pytest exceeded wall budget of {wall:.0f}s; "
                "process group terminated (SEC-01).",
                file=sys.stderr,
            )
            return 124
        except BoundedCancelledError:
            print(
                "ERROR: pytest cancelled by signal; process group terminated.",
                file=sys.stderr,
            )
            return 130
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)

    sys.stdout.buffer.write(result.stdout)
    sys.stderr.buffer.write(result.stderr)
    return int(result.returncode or 0)


if __name__ == "__main__":
    raise SystemExit(main())
