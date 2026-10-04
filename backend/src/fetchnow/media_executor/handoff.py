"""Copy attempt files without following links or accepting a returned path."""

from __future__ import annotations

import os
import stat
from pathlib import Path


class HandoffError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _open_regular(path: Path, *, max_bytes: int) -> tuple[int, int]:
    if ".." in path.parts or path.parent.is_symlink():
        raise HandoffError("parent")
    # A compromised tool can replace output-mux with a FIFO. Never block on it.
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags |= nofollow
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise HandoffError("open_failed") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise HandoffError("not_regular")
        if stat.S_ISLNK(st.st_mode):
            raise HandoffError("symlink")
        if st.st_nlink < 1 or st.st_size < 0 or st.st_size > max_bytes:
            raise HandoffError("size")
        return fd, st.st_size
    except HandoffError:
        os.close(fd)
        raise


def copy_regular(source: Path, dest: Path, *, max_bytes: int) -> int:
    """Copy one regular file to a new name in the destination directory."""
    if dest.name in {".", ".."} or "/" in dest.name or "\\" in dest.name:
        raise HandoffError("name")
    if ".." in dest.parts or dest.parent.is_symlink():
        raise HandoffError("parent")
    fd, size = _open_regular(source, max_bytes=max_bytes)
    out_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    out_flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        out = os.open(dest, out_flags, 0o644)
    except OSError as exc:
        os.close(fd)
        raise HandoffError("create_failed") from exc
    try:
        remaining = size
        while remaining:
            block = os.read(fd, min(1024 * 1024, remaining))
            if not block:
                raise HandoffError("short_read")
            view = memoryview(block)
            while view:
                written = os.write(out, view)
                if written <= 0:
                    raise HandoffError("short_write")
                view = view[written:]
            remaining -= len(block)
        os.fsync(out)
    except Exception:
        os.close(out)
        os.close(fd)
        dest.unlink(missing_ok=True)
        raise
    os.close(out)
    os.close(fd)
    return size


def assert_contained(directory: Path, candidate: Path) -> Path:
    """Reject a path that is not a direct regular file of ``directory``."""
    if candidate.is_symlink():
        raise HandoffError("symlink")
    parent = Path(os.path.abspath(directory))
    target = Path(os.path.abspath(candidate))
    if target.parent != parent:
        raise HandoffError("escape")
    if not target.is_file():
        raise HandoffError("not_regular")
    return target
