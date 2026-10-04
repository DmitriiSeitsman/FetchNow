"""Acceptance driver inside the actual executor container (stdlib only).

All RPCs use a fresh uid 10001 process. No mocked peercred, runner or cgroup.
The synthetic-tool phase uses a separate container with a test-only mount.
"""

from __future__ import annotations

import base64
import concurrent.futures
import errno
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

WORK = Path("/var/lib/fetchnow/executor")
SOCKET = "/run/fetchnow-executor/ctrl/worker.sock"
HERE = Path(__file__).resolve()
JOBS = [
    f"{digit * 8}-{digit * 4}-4{digit * 3}-8{digit * 3}-{digit * 12}"
    for digit in "1234"
]


def request(op: str, index: int = 0, **extra: object) -> dict[str, object]:
    return dict(v=1, op=op, job_id=JOBS[index], attempt=1, fence=1, **extra)


def wire(payload: dict[str, object], timeout: float = 20) -> dict[str, object]:
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(timeout)
        sock.connect(SOCKET)
        sock.sendall(json.dumps(payload).encode() + b"\n")
        chunks = bytearray()
        while not chunks.endswith(b"\n"):
            part = sock.recv(8192)
            if not part:
                raise OSError("unexpected EOF")
            chunks.extend(part)
            if len(chunks) > 400000:
                raise OSError("oversized response")
    return json.loads(chunks)


def worker(payload: dict[str, object]) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, str(HERE), "rpc", json.dumps(payload)],
        user=10001,
        group=10001,
        extra_groups=[],
        capture_output=True,
        text=True,
        timeout=25,
        check=True,
    )
    return json.loads(result.stdout)


def job(index: int) -> Path:
    return WORK / f"{JOBS[index]}_1_1"


def wait_for(predicate: object, seconds: float = 8) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("bounded readiness condition failed")


def reserve(index: int) -> None:
    assert worker(request("reserve", index))["code"] == "reserved"


def write_as_worker(path: Path, text: str) -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; "
            "Path(sys.argv[1]).write_text(sys.argv[2])",
            str(path),
            text,
        ],
        user=10001,
        group=10001,
        extra_groups=[],
        check=True,
        timeout=5,
    )


def media() -> dict[str, object]:
    checks = []
    # Root has the worker group for DAC but must be rejected by SO_PEERCRED.
    assert wire(request("reserve"))["code"] == "unauthorized"
    checks.append("real_peercred_root_denied")
    for index, (container, video_codec, audio_codec) in enumerate(
        (
            ("mp4", "libx264", "aac"),
            ("webm", "libvpx", "libopus"),
        )
    ):
        reserve(index)
        for filename, source, codec, media_type in (
            ("input-video", "color=black:s=160x120:d=0.2:r=10", video_codec, "v"),
            ("input-audio", "sine=frequency=440:duration=0.2", audio_codec, "a"),
        ):
            subprocess.run(
                [
                    "/usr/bin/ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    source,
                    f"-c:{media_type}",
                    codec,
                    "-f",
                    container,
                    str(job(index) / filename),
                ],
                user=10001,
                group=10001,
                extra_groups=[],
                check=True,
                capture_output=True,
                timeout=15,
            )
        mux = worker(request("mux_copy", index, container=container))
        assert mux["ok"] is True, mux
        probe = worker(request("ffprobe_validate", index))
        assert probe["ok"] is True, probe
        parsed = json.loads(base64.b64decode(probe["stdout_b64"]))
        assert {s["codec_type"] for s in parsed["streams"]} == {"video", "audio"}
        # A completed operation is replayed without another tool invocation.
        assert worker(request("ffprobe_validate", index)) == probe
        checks.append(f"real_{container}_mux_probe_replay")
        assert worker(request("release", index))["code"] == "released"
        assert not job(index).exists()
    return {"checks": checks}


def lifecycle() -> dict[str, object]:
    checks = []
    for index in range(3):
        reserve(index)
        write_as_worker(job(index) / "input-video", "probe" if index == 0 else "hold")
    # Deliberately DAC-readable canaries: denial must come from Landlock.
    (job(1) / "canary").write_text("synthetic")
    (job(1) / "canary").chmod(0o666)
    published = WORK / "published"
    published.mkdir(mode=0o755)
    (published / "canary").write_text("synthetic")
    (published / "canary").chmod(0o666)
    (job(0) / "escape").symlink_to(job(1) / "canary")
    probe = worker(request("mux_copy", container="mp4"))
    observation = validate_isolation(probe)
    checks.append("tool_executed_identity_and_specific_denials")
    assert worker(request("release"))["code"] == "released"
    reserve(0)
    write_as_worker(job(0) / "input-video", "hold")
    return cancel_pair(checks, observation)


def validate_isolation(probe: dict[str, object]) -> dict[str, object]:
    assert probe["ok"] is True and probe["exit_code"] == 0, probe
    observation = json.loads(base64.b64decode(probe["stdout_b64"], validate=True))
    assert observation["uid"] == [10003] * 3
    assert observation["gid"] == [10003] * 3 and observation["groups"] == []
    assert not (set(observation["env"]) - {"PATH", "LANG", "LC_ALL", "LC_CTYPE"})
    assert set(observation["denied"]) == {
        "sibling",
        "published",
        "symlink",
        "proc",
        "cgroup",
        "socket",
    }
    assert all(v in (errno.EACCES, errno.EPERM) for v in observation["denied"].values())
    return observation


def cancel_pair(checks: list[str], observation: dict[str, object]) -> dict[str, object]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(worker, request("mux_copy", 0, container="mp4"))
        b = pool.submit(worker, request("mux_copy", 1, container="mp4"))
        try:
            # Existence alone races with write_text: wait for complete numeric PID
            # files for BOTH roles, not just the descendant's directory entry.
            wait_for(
                lambda: all(
                    ready_pid(job(i) / f"{role}.pid")
                    for i in (0, 1)
                    for role in ("parent", "descendant")
                )
            )
            identities = {
                f"{i}-{role}": int((job(i) / f"{role}.pid").read_text())
                for i in (0, 1)
                for role in ("parent", "descendant")
            }
            assert worker(request("mux_copy", 2, container="mp4"))["code"] == "overflow"
            assert worker(request("cancel", 0))["code"] == "cancelling"
            assert a.result(timeout=8)["code"] == "cancelled"
            # Reaping, not kill(pid,0) or empty cgroup alone.
            assert all(
                not Path(f"/proc/{identities[f'0-{role}']}").exists()
                for role in ("parent", "descendant")
            )
            before = [
                (job(1) / f"{role}.hb").stat().st_size
                for role in ("parent", "descendant")
            ]
            wait_for(
                lambda: all(
                    (job(1) / f"{role}.hb").stat().st_size > value
                    for role, value in zip(
                        ("parent", "descendant"), before, strict=True
                    )
                )
            )
            assert not b.done()
            checks += ["overflow", "cancel_tree_reaped", "other_job_continues"]
        finally:
            worker(request("cancel", 0))
            worker(request("cancel", 1))
        assert b.result(timeout=8)["code"] == "cancelled"
    assert not list(Path("/sys/fs/cgroup").glob("fn-*"))
    checks.append("job_cgroups_removed")
    return {"checks": checks, "isolation": observation, "pids": identities}


def ready_pid(path: Path) -> bool:
    try:
        value = path.read_text()
        return (
            value.isdecimal()
            and int(value) > 1
            and Path(f"/proc/{value}/stat").exists()
        )
    except OSError:
        return False


def after_restart() -> dict[str, object]:
    assert worker(request("ffprobe_validate"))["code"] == "not_reserved"
    assert worker(request("reserve"))["ok"] is False  # old epoch is never adopted
    assert worker(request("cancel"))["code"] == "not_running"
    assert not list(Path("/sys/fs/cgroup").glob("fn-*"))
    return {"checks": ["restart_no_cached_result_or_stale_reserve"]}


def ready() -> bool:
    try:
        return wire(request("reserve"), timeout=0.2).get("code") == "unauthorized"
    except OSError:
        return False


if __name__ == "__main__":
    if sys.argv[1] == "rpc":
        print(json.dumps(wire(json.loads(sys.argv[2]))))
    else:
        # Changing egid happens before any threads, retaining uid 0 + three caps.
        os.setegid(10001)
        wait_for(ready)
        result = {"media": media, "lifecycle": lifecycle, "restart": after_restart}[
            sys.argv[1]
        ]()
        print(json.dumps(result))
