"""Opt-in Compose contract for the media executor overlay."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_production_compose_does_not_define_the_executor() -> None:
    text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "media-executor" not in text
    assert "MEDIA_EXECUTOR_ENABLED" not in text
    assert "writable-cgroups" not in text


def test_overlay_is_opt_in_and_fail_closed() -> None:
    proc = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "compose.yaml",
            "-f",
            "compose.media-executor.yaml",
            "--profile",
            "media-executor",
            "config",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    text = proc.stdout
    assert "network_mode: none" in text
    assert "writable-cgroups=true" in text
    assert "no-new-privileges:true" in text
    assert "cgroup: private" in text
    assert "cgroup_parent: fetchnow-media-executor.slice" in text
    assert "SETUID" in text and "SETGID" in text and "SETPCAP" in text
    assert "privileged: true" not in text
    executor = text.split("media-executor:", 1)[1].split("\n  postgres:", 1)[0]
    assert "DATABASE_URL" not in executor
    assert "MEDIA_EXECUTOR_ENABLED" not in executor
    enabled = 'MEDIA_EXECUTOR_ENABLED: "false"'
    plain = "MEDIA_EXECUTOR_ENABLED: false"
    assert enabled in text or plain in text
