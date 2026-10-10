"""Opt-in Compose contract for SEC-09 media-net overlay."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_production_compose_does_not_define_media_net() -> None:
    text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "media-net-executor" not in text
    assert "MEDIA_NET_EXECUTOR_ENABLED" not in text
    assert "egress-proxy" not in text


def test_media_net_overlay_is_internal_opt_in_and_fail_closed() -> None:
    proc = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "compose.yaml",
            "-f",
            "compose.media-executor.yaml",
            "-f",
            "compose.media-net-executor.yaml",
            "--profile",
            "media-executor",
            "--profile",
            "media-net-executor",
            "config",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    text = proc.stdout
    assert "media-net" in text
    assert "internal: true" in text or "internal:true" in text
    assert "egress-proxy" in text
    assert "media-net-executor" in text
    assert "privileged: true" not in text
    assert "network_mode: host" not in text
    # Offline executor remains netless.
    assert "network_mode: none" in text
    # New services must not publish host ports.
    net_block = text.split("media-net-executor:", 1)[1].split(
        "\n  worker:", 1
    )[0]
    assert "ports:" not in net_block
    proxy_block = text.split("egress-proxy:", 1)[1].split(
        "\n  media-net-executor:", 1
    )[0]
    assert "ports:" not in proxy_block
    assert "DATABASE_URL" not in net_block
    assert "DATABASE_URL" not in proxy_block
    enabled = 'MEDIA_NET_EXECUTOR_ENABLED: "false"'
    plain = "MEDIA_NET_EXECUTOR_ENABLED: false"
    assert enabled in text or plain in text
