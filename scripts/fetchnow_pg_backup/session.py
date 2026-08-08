"""Backup-root session holding one continuous BackupRootLock (PRD1C3B2B1)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from . import DEFAULT_COMPRESSION, HOLD_SCHEMA_VERSION
from .compose_target import ComposeTarget
from .fs_safety import validate_backup_root
from .locking import BackupRootLock
from .postgres_ops import RunFn
from .process_util import run_command

if TYPE_CHECKING:
    from .create import CreateResult
    from .retention_hold import RetentionHoldAcquired
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

    def acquire_retention_hold(
        self,
        *,
        backup_dir: Path,
        migration_id: str,
        verified: VerifyResult,
    ) -> RetentionHoldAcquired:
        self._require_active()
        from . import MANIFEST_FILENAME, VERIFICATIONS_DIRNAME, __version__
        from .manifest import ManifestV1
        from .retention_hold import (
            RetentionHoldAcquired,
            RetentionHoldError,
            new_hold_id,
            publish_acquired_hold,
        )

        if not verified.passed or not verified.exact_head_proof:
            raise RetentionHoldError(
                "retention hold requires passed v2 exact-head verification"
            )
        if verified.attestation_schema_version != 2:
            raise RetentionHoldError("retention hold requires attestation schema v2")
        if verified.attestation_path is None or verified.attestation_sha256 is None:
            raise RetentionHoldError("retention hold requires attestation identity")
        if verified.expected_alembic_heads is None:
            raise RetentionHoldError("retention hold requires expected heads")
        if verified.static_alembic_heads is None:
            raise RetentionHoldError("retention hold requires static heads")
        if verified.verified_restored_heads is None:
            raise RetentionHoldError("retention hold requires restored heads")
        if verified.migrations_graph_sha256 is None:
            raise RetentionHoldError("retention hold requires migrations graph SHA-256")

        directory = backup_dir.resolve()
        manifest = ManifestV1.load(directory / MANIFEST_FILENAME)
        rel_attestation = verified.attestation_path.relative_to(directory)
        if VERIFICATIONS_DIRNAME not in rel_attestation.parts:
            raise RetentionHoldError("attestation must live under verifications/")
        hold_id = new_hold_id()
        acquired = RetentionHoldAcquired(
            schema_version=HOLD_SCHEMA_VERSION,
            hold_id=hold_id,
            backup_id=manifest.backup_id,
            backup_manifest_sha256=manifest.sha256,
            backup_dump_sha256=manifest.sha256,
            migration_id=migration_id,
            attestation_schema_version=2,
            attestation_path=str(rel_attestation),
            attestation_sha256=verified.attestation_sha256,
            expected_alembic_heads=verified.expected_alembic_heads,
            static_alembic_heads=verified.static_alembic_heads,
            verified_restored_heads=verified.verified_restored_heads,
            migrations_graph_sha256=verified.migrations_graph_sha256,
            acquired_at_utc=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            tool_version=__version__,
        )
        publish_acquired_hold(backup_root=self.backup_root, acquired=acquired)
        return acquired

    def release_retention_hold(self, *, hold_id: str, reason: str) -> None:
        self._require_active()
        from . import __version__
        from .retention_hold import RetentionHoldReleased, publish_released_hold

        released = RetentionHoldReleased(
            schema_version=HOLD_SCHEMA_VERSION,
            hold_id=hold_id,
            released_at_utc=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            reason=reason,
            tool_version=__version__,
        )
        publish_released_hold(backup_root=self.backup_root, released=released)


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
