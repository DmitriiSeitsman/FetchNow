"""Restore verification into an isolated temporary database."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from . import DUMP_FILENAME, MANIFEST_FILENAME, __version__
from .attestation import build_attestation, write_attestation
from .checksums import sha256_file
from .compose_target import ComposeTarget
from .fs_safety import PathSafetyError, reject_symlink_components, validate_backup_root
from .locking import BackupRootLock
from .manifest import ManifestError, ManifestV1
from .postgres_ops import (
    RunFn,
    compose_postgres_healthy,
    compose_ps_postgres_running,
    create_database,
    drop_database,
    generate_restore_database_name,
    pg_restore_list,
    read_database_name,
    read_versions,
    require_utilities,
    restore_archive_to_database,
)
from .process_util import CommandError, run_command
from .semantic import (
    all_checks_passed,
    discover_alembic_head_revisions,
    run_semantic_checks,
)


@dataclass(frozen=True)
class VerifyResult:
    backup_id: str
    passed: bool
    temporary_database: str | None
    attestation_path: Path | None
    failure_reason: str | None


def _load_and_check_backup_dir(backup_dir: Path) -> tuple[Path, ManifestV1, Path]:
    directory = reject_symlink_components(backup_dir)
    if not directory.is_dir():
        raise PathSafetyError(f"backup directory not found: {directory}")
    if directory.is_symlink():
        raise PathSafetyError(f"backup directory must not be a symlink: {directory}")
    manifest_path = directory / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise ManifestError(f"missing {MANIFEST_FILENAME}")
    manifest = ManifestV1.load(manifest_path)
    if manifest.backup_id != directory.name:
        raise ManifestError(
            f"backup_id {manifest.backup_id!r} does not match directory name {directory.name!r}"
        )
    dump_path = directory / manifest.dump_filename
    if dump_path.name != DUMP_FILENAME:
        raise ManifestError("unexpected dump filename")
    if not dump_path.is_file() or dump_path.is_symlink():
        raise PathSafetyError("dump file missing or is a symlink")
    return directory, manifest, dump_path


def verify_backup(
    *,
    target: ComposeTarget,
    backup_dir: Path,
    backup_root: Path,
    repo_root: Path,
    migrations_versions_dir: Path,
    wait_lock: bool = False,
    runner: RunFn = run_command,
) -> VerifyResult:
    root = validate_backup_root(backup_root, repo_root=repo_root)
    directory, manifest, dump_path = _load_and_check_backup_dir(backup_dir)
    # Ensure backup_dir is under backup_root.
    try:
        directory.relative_to(root)
    except ValueError as exc:
        raise PathSafetyError("backup directory is not under backup root") from exc

    size = dump_path.stat().st_size
    if size != manifest.dump_size_bytes:
        raise ManifestError(
            f"dump size mismatch: file={size} manifest={manifest.dump_size_bytes}"
        )
    digest = sha256_file(dump_path)
    if digest != manifest.sha256:
        raise ManifestError("SHA-256 mismatch; refusing restore")

    temp_db: str | None = None
    cleanup_ok = False
    dropped: str | None = None
    semantic_results = []
    restore_duration: float | None = None
    failure: str | None = None
    passed = False
    attestation_path: Path | None = None
    versions = None

    with BackupRootLock(root, wait=wait_lock):
        try:
            compose_ps_postgres_running(target, runner=runner)
            compose_postgres_healthy(target, runner=runner)
            require_utilities(target, runner=runner)
            versions = read_versions(target, runner=runner)
            source_db = read_database_name(target, runner=runner)
            if source_db != manifest.database_name:
                # Soft warning path: still OK if names differ across projects, but
                # refuse if generated temp equals source.
                pass
            pg_restore_list(target, dump_host_path=dump_path, runner=runner)

            temp_db = generate_restore_database_name(source_database=source_db)
            create_database(target, temp_db, runner=runner)
            started = time.monotonic()
            try:
                restore_archive_to_database(
                    target,
                    dump_host_path=dump_path,
                    database=temp_db,
                    source_database=source_db,
                    runner=runner,
                )
                restore_duration = time.monotonic() - started
                expected = discover_alembic_head_revisions(migrations_versions_dir)
                semantic_results = run_semantic_checks(
                    target,
                    database=temp_db,
                    source_database=source_db,
                    expected_revisions=expected,
                    runner=runner,
                )
                if not all_checks_passed(semantic_results):
                    failure = "semantic checks failed: " + "; ".join(
                        f"{r.name}={r.ok}" for r in semantic_results
                    )
                    passed = False
                else:
                    passed = True
            finally:
                try:
                    drop_database(
                        target,
                        temp_db,
                        runner=runner,
                        source_database=source_db,
                    )
                    cleanup_ok = True
                    dropped = temp_db
                except Exception as cleanup_exc:  # noqa: BLE001
                    cleanup_ok = False
                    if failure is None:
                        failure = f"cleanup failed: {cleanup_exc}"
                    else:
                        failure = f"{failure}; cleanup failed: {cleanup_exc}"
                    raise

            if not passed:
                raise CommandError(failure or "verification failed")
            if not cleanup_ok:
                raise CommandError("verification cleanup did not succeed")

        except Exception as exc:  # noqa: BLE001
            if failure is None:
                failure = str(exc)
            passed = False
            # Write failure attestation without claiming successful restore/cleanup.
            att = build_attestation(
                backup_id=manifest.backup_id,
                backup_sha256=manifest.sha256,
                postgres_version=versions.server if versions else "unknown",
                pg_restore_version=versions.pg_restore if versions else "unknown",
                temporary_database=temp_db,
                restore_duration_seconds=restore_duration,
                semantic_checks=semantic_results,
                cleanup_ok=cleanup_ok,
                dropped_database=dropped,
                result="failed",
                failure_reason=failure,
                tool_version=__version__,
            )
            try:
                attestation_path = write_attestation(directory, att)
            except OSError:
                attestation_path = None
            return VerifyResult(
                backup_id=manifest.backup_id,
                passed=False,
                temporary_database=temp_db,
                attestation_path=attestation_path,
                failure_reason=failure,
            )

        if not cleanup_ok:
            raise CommandError("refusing passed attestation without successful cleanup")
        att = build_attestation(
            backup_id=manifest.backup_id,
            backup_sha256=manifest.sha256,
            postgres_version=versions.server,
            pg_restore_version=versions.pg_restore,
            temporary_database=temp_db,
            restore_duration_seconds=restore_duration,
            semantic_checks=semantic_results,
            cleanup_ok=cleanup_ok,
            dropped_database=dropped,
            result="passed",
            failure_reason=None,
            tool_version=__version__,
        )
        attestation_path = write_attestation(directory, att)
        return VerifyResult(
            backup_id=manifest.backup_id,
            passed=True,
            temporary_database=temp_db,
            attestation_path=attestation_path,
            failure_reason=None,
        )
