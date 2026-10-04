"""Job directories and the Landlock path layout. No client-supplied paths."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from fetchnow.media_executor.constants import (
    EXECUTOR_UID,
    JOB_DIR_MODE,
    SOCKET_DIR_MODE,
    SOCKET_MODE,
    TOOL_UID,
    WORK_ROOT_MODE,
    WORKER_GID,
)
from fetchnow.media_executor.protocol import Request

_JOB_NAME = re.compile(
    r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}_[1-9][0-9]{0,3}_[1-9][0-9]{0,9}\Z"
)


def job_name(request: Request) -> str:
    return f"{request.job_id}_{request.attempt}_{request.fence}"


def job_directory(root: Path, request: Request) -> Path:
    """Return the only directory the tool may write for this attempt."""
    name = job_name(request)
    if _JOB_NAME.fullmatch(name) is None:
        raise ValueError("job name")
    root_abs = Path(os.path.abspath(root))
    path = root_abs / name
    if path.parent != root_abs or path.name != name:
        raise ValueError("job name")
    return path


def covers(root: str, child: str) -> bool:
    """True when ``root`` is ``child`` or a filesystem prefix of it."""
    root_real = os.path.realpath(root)
    child_real = os.path.realpath(child)
    if root_real == "/":
        return True
    prefix = root_real.rstrip("/")
    return child_real == prefix or child_real.startswith(prefix + "/")


def layout_rejection(read_only_roots: list[str], protected: list[str]) -> str | None:
    """Refuse a read-only root that would also grant the work tree."""
    for root in read_only_roots:
        for secret in protected:
            if covers(root, secret):
                return "ro_covers_protected"
    return None


def default_read_only_roots() -> list[str]:
    found: list[str] = []
    for candidate in ("/usr", "/bin", "/lib", "/lib64"):
        if os.path.isdir(candidate):
            found.append(candidate)
    return found


class IdentityError(OSError):
    pass


def _set_ids(uid: int, gid: int) -> None:
    setgid = getattr(os, "setresgid", None)
    setuid = getattr(os, "setresuid", None)
    if setgid is None or setuid is None:
        raise IdentityError("setresuid unavailable")
    setgid(gid, gid, 0)
    setuid(uid, uid, 0)


def _restore_root() -> None:
    setuid = getattr(os, "setresuid", None)
    setgid = getattr(os, "setresgid", None)
    if setuid is None or setgid is None:
        raise IdentityError("setresuid unavailable")
    setuid(0, 0, 0)
    setgid(0, 0, 0)


def make_owned_directory(path: Path, *, uid: int, gid: int, mode: int) -> None:
    """Change credentials in a fresh process, never in server threads."""
    if os.geteuid() != 0:
        raise IdentityError("root required")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            __name__,
            "mkdir",
            str(path),
            str(uid),
            str(gid),
            str(mode),
        ],
        cwd="/",
        capture_output=True,
        timeout=5,
        check=False,
    )
    if result.returncode:
        raise IdentityError("directory helper failed")


def remove_job_directory(path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", __name__, "remove", str(path)],
        cwd="/",
        capture_output=True,
        timeout=5,
        check=False,
    )
    if result.returncode:
        raise IdentityError("directory cleanup failed")


def make_work_root(path: Path) -> None:
    if os.geteuid() != 0:
        raise IdentityError("root required")
    if not path.exists():
        path.mkdir(mode=WORK_ROOT_MODE)
    # Named volumes are root-owned. chmod by the owner needs no CAP_FOWNER.
    os.chmod(path, WORK_ROOT_MODE)


def make_job_directory(path: Path) -> None:
    make_owned_directory(path, uid=TOOL_UID, gid=WORKER_GID, mode=JOB_DIR_MODE)


def make_socket_directory(path: Path) -> None:
    """Create the socket directory as uid 10002, group 10001.

    The parent is the shared volume. It is made sticky-world-writable so the
    setuid mkdir can succeed without CAP_CHOWN. The socket directory itself
    stays mode 0770.
    """
    if os.geteuid() != 0:
        raise IdentityError("root required")
    parent = path.parent
    if parent.exists():
        os.chmod(parent, WORK_ROOT_MODE)
    if path.is_dir():
        return
    make_owned_directory(path, uid=EXECUTOR_UID, gid=WORKER_GID, mode=SOCKET_DIR_MODE)


def bind_socket_as_executor(sock: object, path: Path) -> None:
    """Bind the control socket as uid 10002, group 10001, mode 0660."""
    if os.geteuid() != 0:
        raise IdentityError("root required")
    previous = os.umask(0)
    try:
        _set_ids(EXECUTOR_UID, WORKER_GID)
        if path.is_symlink():
            raise IdentityError("socket symlink")
        path.unlink(missing_ok=True)
        sock.bind(str(path))  # type: ignore[attr-defined]
        os.chmod(path, SOCKET_MODE)
    finally:
        _restore_root()
        os.umask(previous)


if __name__ == "__main__":
    # This helper has no RPC entrypoint. All arguments come from the supervisor.
    action, target, *ids = sys.argv[1:]
    directory = Path(target)
    os.setgroups([])
    if action == "mkdir":
        uid, gid, mode = map(int, ids)
        os.setgid(gid)
        os.setuid(uid)
        os.umask(0)
        directory.mkdir(mode=mode)
        directory.chmod(mode)
    elif action == "remove":
        os.setgid(WORKER_GID)
        os.setuid(TOOL_UID)
        if directory.is_symlink():
            raise IdentityError("job directory symlink")
        if directory.exists():
            # fd-relative unlink cannot follow a tool-created link outside the job.
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for name in os.listdir(fd):
                    os.unlink(name, dir_fd=fd)
            finally:
                os.close(fd)
            directory.rmdir()
    else:
        raise IdentityError("unknown helper operation")
