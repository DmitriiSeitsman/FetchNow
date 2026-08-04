"""Exclusive lock for release preparation under a deployment root."""

from __future__ import annotations

import fcntl
import os
import time
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from .c2_constants import LOCK_FILENAME
from .deploy_root import DeployRootError, ensure_deploy_layout


class ReleaseLockError(RuntimeError):
    """Raised when the release-root lock cannot be acquired."""


@dataclass
class ReleaseRootLock:
    deploy_root: Path
    wait: bool = False
    wait_timeout_seconds: float | None = 30.0
    _fd: int | None = None

    def acquire(self) -> None:
        _, locks = ensure_deploy_layout(self.deploy_root)
        lock_path = locks / LOCK_FILENAME
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.chmod(lock_path, 0o600)
        flags = fcntl.LOCK_EX
        if not self.wait:
            flags |= fcntl.LOCK_NB
            try:
                fcntl.flock(fd, flags)
            except BlockingIOError as exc:
                os.close(fd)
                raise ReleaseLockError(
                    f"release root is locked by another writer: {self.deploy_root}"
                ) from exc
        else:
            deadline = None
            if self.wait_timeout_seconds is not None:
                deadline = time.monotonic() + self.wait_timeout_seconds
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if deadline is not None and time.monotonic() >= deadline:
                        os.close(fd)
                        raise ReleaseLockError(
                            f"timed out waiting for release-root lock: {self.deploy_root}"
                        ) from None
                    time.sleep(0.1)
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> ReleaseRootLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


def assert_same_filesystem(src: Path, dst_parent: Path) -> None:
    """Refuse cross-device rename (atomic publication requires same FS)."""
    if not src.exists() or not dst_parent.exists():
        return
    if os.stat(src).st_dev != os.stat(dst_parent).st_dev:
        raise DeployRootError(
            "incomplete release and releases/ must share the same filesystem "
            "for atomic rename"
        )
