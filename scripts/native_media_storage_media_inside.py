"""Disposable S7 driver, executed as worker uid 10001 inside its real image.

Generation is fixture preparation. Mux/probe use the production UDS supervisor,
launcher and cgroup, not an unrestricted docker-exec ffmpeg stand-in.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

SOCKET = "/run/fetchnow-executor/ctrl/worker.sock"
WORK = Path("/var/lib/fetchnow/executor")
OUTPUTS = Path("/var/lib/fetchnow/tmp/acceptance-fixtures")


def rpc(payload: dict[str, Any]) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(90)
        sock.connect(SOCKET)
        sock.sendall(json.dumps(payload).encode() + b"\n")
        body = bytearray()
        while not body.endswith(b"\n"):
            chunk = sock.recv(8192)
            if not chunk:
                raise RuntimeError("S7 RPC unexpected EOF")
            body.extend(chunk)
            if len(body) > 400_000:
                raise RuntimeError("S7 RPC response too large")
    result = json.loads(body)
    if not isinstance(result, dict) or result.get("ok") is not True:
        code = result.get("code") if isinstance(result, dict) else "shape"
        raise RuntimeError(f"S7 RPC failed: {code}")
    return result


def run_media(
    *,
    call: Callable[..., dict[str, Any]] = rpc,
    run: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    if os.getuid() != 10001 or os.getgid() != 10001:
        raise RuntimeError("S7 must run as the worker identity")
    OUTPUTS.mkdir(parents=True, exist_ok=False)
    outputs = []
    for container, video_codec, audio_codec in (
        ("mp4", "libx264", "aac"),
        ("webm", "libvpx", "libopus"),
    ):
        key = {"v": 1, "job_id": str(uuid.uuid4()), "attempt": 1, "fence": 1}
        directory = WORK / f"{key['job_id']}_1_1"
        reserved = call({**key, "op": "reserve"})
        if reserved.get("code") != "reserved":
            raise RuntimeError("S7 reserve not acknowledged")
        try:
            for name, source, codec, kind in (
                ("input-video", "color=black:s=64x64:d=0.2:r=10", video_codec, "v"),
                ("input-audio", "sine=frequency=440:duration=0.2", audio_codec, "a"),
            ):
                run(
                    [
                        "/usr/bin/ffmpeg",
                        "-v",
                        "error",
                        "-f",
                        "lavfi",
                        "-i",
                        source,
                        f"-c:{kind}",
                        codec,
                        "-f",
                        container,
                        str(directory / name),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
            mux = call({**key, "op": "mux_copy", "container": container})
            probe = call({**key, "op": "ffprobe_validate"})
            for result in (mux, probe):
                if (
                    result.get("exit_code") != 0
                    or result.get("timed_out")
                    or result.get("cancelled")
                ):
                    raise RuntimeError("S7 media operation did not succeed")
            parsed = json.loads(base64.b64decode(probe["stdout_b64"], validate=True))
            if {s["codec_type"] for s in parsed["streams"]} != {"video", "audio"}:
                raise RuntimeError("S7 mux output streams mismatch")
            target = OUTPUTS / f"clip-copy.{container}"
            shutil.copyfile(directory / "output-mux", target)
            if target.stat().st_size <= 0:
                raise RuntimeError("S7 empty published fixture")
            outputs.append(str(target))
        finally:
            # Release never races a still-running operation, including on error.
            call({**key, "op": "cancel"})
            released = call({**key, "op": "release"})
            if released.get("code") != "released":
                raise RuntimeError("S7 release not acknowledged")
    return {
        "ok": True,
        "uid": os.getuid(),
        "gid": os.getgid(),
        "outputs": outputs,
        "operations": ["mp4_mux_probe", "webm_mux_probe"],
        "transport": "production_uds",
    }


if __name__ == "__main__":
    print(json.dumps(run_media(), sort_keys=True))
