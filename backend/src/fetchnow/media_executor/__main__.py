"""PID 1 entrypoint. Refuses to start when isolation preflight fails."""

from __future__ import annotations

import ctypes
import os
import socket
import sys
from pathlib import Path

from fetchnow.media_executor.layout import (
    bind_socket_as_executor,
    make_job_directory,
    make_socket_directory,
    make_work_root,
    remove_job_directory,
)
from fetchnow.media_executor.runner import (
    CGROUP_ROOT,
    LandlockRunner,
    sweep_own_cgroups,
)
from fetchnow.media_executor.server import ExecutorApp

_FORBIDDEN_ENV = (
    "DATABASE_URL",
    "POSTGRES_PASSWORD",
    "ROBOKASSA_TEST_PASSWORD1",
    "ROBOKASSA_TEST_PASSWORD2",
    "DOCKER_HOST",
)


def _required_path(name: str) -> Path:
    value = os.environ.get(name, "").strip()
    if not value or not os.path.isabs(value):
        raise OSError(f"{name} must be absolute")
    return Path(value)


def preflight(launcher: Path) -> LandlockRunner:
    if os.geteuid() != 0:
        raise OSError("supervisor must be root")
    for key in _FORBIDDEN_ENV:
        if os.environ.get(key):
            raise OSError("forbidden environment")
    if not CGROUP_ROOT.joinpath("cgroup.controllers").is_file():
        raise OSError("cgroup v2 controllers missing")
    probe = CGROUP_ROOT / "fn-0000000000000000"
    try:
        probe.mkdir()
    except OSError as exc:
        raise OSError("cgroup hierarchy is not writable") from exc
    else:
        probe.rmdir()
    runner = LandlockRunner(launcher)
    runner.identity_abi()
    if sys.platform != "linux":
        raise OSError("linux required")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:
        raise OSError("child subreaper failed")
    return runner


def main() -> int:
    try:
        launcher = _required_path("MEDIA_EXECUTOR_LAUNCHER")
        work_root = _required_path("MEDIA_EXECUTOR_WORK_ROOT")
        socket_path = _required_path("MEDIA_EXECUTOR_SOCKET")
        ffmpeg = str(_required_path("MEDIA_MUXING_FFMPEG_PATH"))
        ffprobe = str(_required_path("MEDIA_MUXING_FFPROBE_PATH"))
        runner = preflight(launcher)
        sweep_own_cgroups()
        make_work_root(work_root)
        make_socket_directory(socket_path.parent)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bind_socket_as_executor(sock, socket_path)
        app = ExecutorApp(
            work_root=work_root,
            runner=runner,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            socket_dir=socket_path.parent,
            make_job=make_job_directory,
            remove_job=remove_job_directory,
        )
        app.serve(sock)
    except OSError as exc:
        print(f"media executor preflight failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
