"""Backup-root session holding one continuous BackupRootLock (PRD1C3B2B1)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import DEFAULT_COMPRESSION
from .compose_target import ComposeTarget
from .fs_safety import validate_backup_root
from .locking import BackupRootLock
from .postgres_ops import RunFn
from .process_util import run_command

if TYPE_CHECKING:
    from .create import CreateResult
    from .verify import VerifyResult


class BackupSessionError(RuntimeError):
    """Invalid backup-root session lifecycle or binding."""


class _SessionActivationToken:
    __slots__ = ()


_SESSION_TOKEN = _SessionActivationToken()


@dataclass
class BackupRootSession:
    """Capability-style backup root session; not constructible outside the context manager."""

    backup_root: Path
    repo_root: Path
    wait_lock: bool
    _lock: BackupRootLock
    _active: bool = False
    _token: _SessionActivationToken | None = None

    def __post_init__(self) -> None:
        if self._token is not _SESSION_TOKEN:
            raise BackupSessionError(
                "BackupRootSession cannot be constructed directly; "
                "use open_backup_root_session()"
            )

    def _require_active(self) -> None:
        if not self._active:
            raise BackupSessionError(
                "backup-root session is not active; enter open_backup_root_session first"
            )

    def create_backup(
        self,
        *,
        target: ComposeTarget,
        compression: str | None = None,
        runner: RunFn = run_command,
        skip_free_space: bool = False,
    ) -> CreateResult:
        self._require_active()
        from .create import _create_backup_in_session

        return _create_backup_in_session(
            session=self,
            target=target,
            compression=compression or DEFAULT_COMPRESSION,
            runner=runner,
            skip_free_space=skip_free_space,
        )

    def verify_backup(
        self,
        *,
        target: ComposeTarget,
        backup_dir: Path,
        expected_alembic_heads: tuple[str, ...],
        migrations_versions_dir: Path,
        runner: RunFn = run_command,
    ) -> VerifyResult:
        self._require_active()
        from .verify import _verify_backup_in_session

        return _verify_backup_in_session(
            session=self,
            target=target,
            backup_dir=backup_dir,
            expected_alembic_heads=expected_alembic_heads,
            migrations_versions_dir=migrations_versions_dir,
            runner=runner,
        )


@contextmanager
def open_backup_root_session(
    *,
    backup_root: Path,
    repo_root: Path,
    wait_lock: bool = False,
) -> Iterator[BackupRootSession]:
    """Acquire one backup-root lock for the lifetime of the context."""
    root = validate_backup_root(backup_root, repo_root=repo_root)
    lock = BackupRootLock(root, wait=wait_lock)
    session = BackupRootSession(
        backup_root=root,
        repo_root=repo_root,
        wait_lock=wait_lock,
        _lock=lock,
        _token=_SESSION_TOKEN,
    )
    lock.acquire()
    session._active = True
    try:
        yield session
    finally:
        session._active = False
        lock.release()
