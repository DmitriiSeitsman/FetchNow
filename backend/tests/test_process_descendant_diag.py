"""Unit tests for process_descendant_diag (offline; no Docker)."""

from __future__ import annotations

import os
import subprocess
import time

from process_descendant_diag import snapshot_descendant


def test_snapshot_gone_pid() -> None:
    # PID 1 may exist; use a definitely unused high PID unlikely to be live.
    snap = snapshot_descendant(2_147_483_646, role="gone")
    assert snap["pid"] == 2_147_483_646
    assert snap["role"] == "gone"
    assert snap["kill0"] in {"gone", "permission_denied"}
    assert snap["kill0_implies_runnable_code"] is False


def test_snapshot_live_sleep_helper() -> None:
    proc = subprocess.Popen(  # noqa: S603
        ["/bin/sleep", "30"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        time.sleep(0.05)
        snap = snapshot_descendant(proc.pid, role="unit_sleep")
        assert snap["pid"] == proc.pid
        assert snap["observer_uid"] == os.getuid()
        assert snap["kill0"] == "alive_or_zombie"
        assert snap["zombie"] is False
        assert snap["cmdline"] is None or "sleep" in (snap["cmdline"] or "")
        assert snap["kill0_implies_runnable_code"] is False
    finally:
        proc.kill()
        proc.wait(timeout=5)
