"""Exclusive rollout lock (separate from PRD1C2 prepare lock)."""

from __future__ import annotations

import fcntl
import os
import time
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from .c3_constants import ROLLOUT_LOCK_FILENAME
from .deploy_root import ensure_deploy_layout
from .journal import ensure_rollout_layout


class RolloutLockError(RuntimeError):
    """Raised when the rollout lock cannot be acquired."""


@dataclass
class RolloutLock:
    deploy_root: Path
    wait: bool = False
    wait_timeout_seconds: float | None = 30.0
    _fd: int | None = None

    def acquire(self) -> None:
        ensure_deploy_layout(self.deploy_root)
        ensure_rollout_layout(self.deploy_root)
        _, locks = ensure_deploy_layout(self.deploy_root)
        lock_path = locks / ROLLOUT_LOCK_FILENAME
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.chmod(lock_path, 0o600)
        if not self.wait:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                os.close(fd)
                raise RolloutLockError(
                    f"rollout lock held by another writer: {self.deploy_root}"
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
                        raise RolloutLockError(
                            f"timed out waiting for rollout lock: {self.deploy_root}"
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

    def __enter__(self) -> RolloutLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
