"""Exclusive lock for backup-root writers."""

from __future__ import annotations

import fcntl
import os
import time
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from . import LOCK_FILENAME
from .fs_safety import chmod_file_0600, ensure_backup_root


class LockError(RuntimeError):
    """Raised when the backup-root lock cannot be acquired."""


@dataclass
class BackupRootLock:
    backup_root: Path
    wait: bool = False
    wait_timeout_seconds: float | None = 30.0
    _fd: int | None = None

    def acquire(self) -> None:
        ensure_backup_root(self.backup_root)
        lock_path = self.backup_root / LOCK_FILENAME
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        chmod_file_0600(lock_path)
        flags = fcntl.LOCK_EX
        if not self.wait:
            flags |= fcntl.LOCK_NB
            try:
                fcntl.flock(fd, flags)
            except BlockingIOError as exc:
                os.close(fd)
                raise LockError(
                    f"backup root is locked by another writer: {self.backup_root}"
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
                        raise LockError(
                            f"timed out waiting for backup-root lock: {self.backup_root}"
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

    def __enter__(self) -> BackupRootLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
