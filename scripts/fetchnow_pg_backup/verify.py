"""Restore verification into an isolated temporary database."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import DUMP_FILENAME, MANIFEST_FILENAME, __version__
from .attestation import (
    attestation_sha256,
    build_attestation_v2,
    write_attestation,
)
from .checksums import sha256_file
from .compose_target import ComposeTarget
from .fs_safety import PathSafetyError, reject_symlink_components
from .manifest import ManifestError, ManifestV1
from .migrations_authority import (
    MigrationsAuthorityError,
    resolve_static_migrations_authority,
    validate_expected_alembic_heads,
    validate_migrations_versions_dir,
)
from .postgres_ops import (
    RunFn,
    compose_postgres_healthy,
    compose_ps_postgres_running,
    create_database,
    drop_database,
    generate_restore_database_name,
    pg_restore_list,
    read_alembic_version_heads,
    read_database_name,
    read_versions,
    require_utilities,
    restore_archive_to_database,
)
from .process_util import CommandError, run_command
from .semantic import SemanticCheckResult, all_checks_passed, run_table_semantic_checks
from .session import open_backup_root_session

if TYPE_CHECKING:
    from .session import BackupRootSession


@dataclass(frozen=True)
class VerifyResult:
    backup_id: str
    passed: bool
    temporary_database: str | None
    attestation_path: Path | None
    failure_reason: str | None
    attestation_schema_version: int | None
    attestation_sha256: str | None
    expected_alembic_heads: tuple[str, ...] | None
    static_alembic_heads: tuple[str, ...] | None
    migrations_graph_sha256: str | None
    verified_restored_heads: tuple[str, ...] | None
    semantic_success: bool
    cleanup_success: bool
    exact_head_proof: bool


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


def _validate_archive_integrity(manifest: ManifestV1, dump_path: Path) -> None:
    size = dump_path.stat().st_size
    if size != manifest.dump_size_bytes:
        raise ManifestError(
            f"dump size mismatch: file={size} manifest={manifest.dump_size_bytes}"
        )
    digest = sha256_file(dump_path)
    if digest != manifest.sha256:
        raise ManifestError("SHA-256 mismatch; refusing restore")


def _sorted_tuple(values: frozenset[str] | set[str] | tuple[str, ...] | None) -> tuple[str, ...] | None:
    if values is None:
        return None
    return tuple(sorted(frozenset(values)))


def _publish_failed_v2(
    *,
    directory: Path,
    manifest: ManifestV1,
    expected: tuple[str, ...],
    static: tuple[str, ...],
    graph_sha: str,
    restored: frozenset[str] | None,
    failure: str,
    temporary_database: str | None,
    restore_duration_seconds: float | None,
    semantic_checks: list[SemanticCheckResult],
    cleanup_ok: bool,
    dropped_database: str | None,
    postgres_version: str,
    pg_restore_version: str,
) -> VerifyResult:
    attestation_path: Path | None = None
    attestation_digest: str | None = None
    try:
        att = build_attestation_v2(
            backup_id=manifest.backup_id,
            backup_sha256=manifest.sha256,
            postgres_version=postgres_version,
            pg_restore_version=pg_restore_version,
            temporary_database=temporary_database,
            restore_duration_seconds=restore_duration_seconds,
            expected_alembic_heads=expected,
            static_alembic_heads=static,
            migrations_graph_sha256=graph_sha,
            verified_restored_heads=_sorted_tuple(restored),
            semantic_checks=semantic_checks,
            cleanup_ok=cleanup_ok,
            dropped_database=dropped_database,
            result="failed",
            failure_reason=failure,
            tool_version=__version__,
        )
        attestation_path = write_attestation(directory, att)
        attestation_digest = attestation_sha256(attestation_path)
    except OSError:
        attestation_path = None
        attestation_digest = None
    return VerifyResult(
        backup_id=manifest.backup_id,
        passed=False,
        temporary_database=temporary_database,
        attestation_path=attestation_path,
        failure_reason=failure,
        attestation_schema_version=2 if attestation_path is not None else None,
        attestation_sha256=attestation_digest,
        expected_alembic_heads=expected,
        static_alembic_heads=static,
        migrations_graph_sha256=graph_sha,
        verified_restored_heads=_sorted_tuple(restored),
        semantic_success=all_checks_passed(semantic_checks),
        cleanup_success=cleanup_ok,
        exact_head_proof=False,
    )


def _verify_backup_in_session(
    *,
    session: BackupRootSession,
    target: ComposeTarget,
    backup_dir: Path,
    expected_alembic_heads: tuple[str, ...],
    migrations_versions_dir: Path,
    runner: RunFn = run_command,
) -> VerifyResult:
    root = session.backup_root
    directory, manifest, dump_path = _load_and_check_backup_dir(backup_dir)
    try:
        directory.relative_to(root)
    except ValueError as exc:
        raise PathSafetyError("backup directory is not under backup root") from exc

    migrations_dir = validate_migrations_versions_dir(migrations_versions_dir)
    expected = validate_expected_alembic_heads(expected_alembic_heads)
    try:
        static, graph_sha = resolve_static_migrations_authority(migrations_dir)
    except MigrationsAuthorityError:
        raise

    if frozenset(expected) != frozenset(static):
        return _publish_failed_v2(
            directory=directory,
            manifest=manifest,
            expected=expected,
            static=static,
            graph_sha=graph_sha,
            restored=None,
            failure=(
                "expected Alembic heads "
                f"{list(expected)} do not equal static migrations graph heads "
                f"{list(static)}"
            ),
            temporary_database=None,
            restore_duration_seconds=None,
            semantic_checks=[],
            cleanup_ok=False,
            dropped_database=None,
            postgres_version="unknown",
            pg_restore_version="unknown",
        )

    try:
        _validate_archive_integrity(manifest, dump_path)
    except (ManifestError, PathSafetyError) as exc:
        return _publish_failed_v2(
            directory=directory,
            manifest=manifest,
            expected=expected,
            static=static,
            graph_sha=graph_sha,
            restored=None,
            failure=str(exc),
            temporary_database=None,
            restore_duration_seconds=None,
            semantic_checks=[],
            cleanup_ok=False,
            dropped_database=None,
            postgres_version="unknown",
            pg_restore_version="unknown",
        )

    expected_set = frozenset(expected)
    temp_db: str | None = None
    cleanup_ok = False
    dropped: str | None = None
    semantic_results: list[SemanticCheckResult] = []
    restore_duration: float | None = None
    failure: str | None = None
    passed = False
    verified_heads: frozenset[str] | None = None
    versions = None

    try:
        compose_ps_postgres_running(target, runner=runner)
        compose_postgres_healthy(target, runner=runner)
        require_utilities(target, runner=runner)
        versions = read_versions(target, runner=runner)
        source_db = read_database_name(target, runner=runner)
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
            verified_heads = read_alembic_version_heads(
                target,
                temp_db,
                source_database=source_db,
                runner=runner,
            )
            if verified_heads != expected_set:
                raise CommandError(
                    "restored Alembic heads "
                    f"{sorted(verified_heads)} do not equal expected heads "
                    f"{sorted(expected_set)}"
                )
            semantic_results = run_table_semantic_checks(
                target,
                database=temp_db,
                source_database=source_db,
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
        return _publish_failed_v2(
            directory=directory,
            manifest=manifest,
            expected=expected,
            static=static,
            graph_sha=graph_sha,
            restored=verified_heads,
            failure=failure,
            temporary_database=temp_db,
            restore_duration_seconds=restore_duration,
            semantic_checks=semantic_results,
            cleanup_ok=cleanup_ok,
            dropped_database=dropped,
            postgres_version=versions.server if versions else "unknown",
            pg_restore_version=versions.pg_restore if versions else "unknown",
        )

    restored_tuple = tuple(sorted(verified_heads or ()))
    att_v2 = build_attestation_v2(
        backup_id=manifest.backup_id,
        backup_sha256=manifest.sha256,
        postgres_version=versions.server,
        pg_restore_version=versions.pg_restore,
        temporary_database=temp_db,
        restore_duration_seconds=restore_duration,
        expected_alembic_heads=expected,
        static_alembic_heads=static,
        migrations_graph_sha256=graph_sha,
        verified_restored_heads=restored_tuple,
        semantic_checks=semantic_results,
        cleanup_ok=cleanup_ok,
        dropped_database=dropped,
        result="passed",
        failure_reason=None,
        tool_version=__version__,
    )
    attestation_path = write_attestation(directory, att_v2)
    attestation_digest = attestation_sha256(attestation_path)
    return VerifyResult(
        backup_id=manifest.backup_id,
        passed=True,
        temporary_database=temp_db,
        attestation_path=attestation_path,
        failure_reason=None,
        attestation_schema_version=2,
        attestation_sha256=attestation_digest,
        expected_alembic_heads=expected,
        static_alembic_heads=static,
        migrations_graph_sha256=graph_sha,
        verified_restored_heads=restored_tuple,
        semantic_success=True,
        cleanup_success=True,
        exact_head_proof=True,
    )


def verify_backup(
    *,
    target: ComposeTarget,
    backup_dir: Path,
    backup_root: Path,
    repo_root: Path,
    migrations_versions_dir: Path,
    expected_alembic_heads: tuple[str, ...] | None = None,
    wait_lock: bool = False,
    runner: RunFn = run_command,
) -> VerifyResult:
    """Restore-verify a backup, acquiring its own backup-root session."""
    from .migrations_authority import static_alembic_heads_from_migrations_dir

    migrations_dir = validate_migrations_versions_dir(migrations_versions_dir)
    if expected_alembic_heads is None:
        resolved_expected = static_alembic_heads_from_migrations_dir(migrations_dir)
    else:
        resolved_expected = validate_expected_alembic_heads(expected_alembic_heads)

    with open_backup_root_session(
        backup_root=backup_root,
        repo_root=repo_root,
        wait_lock=wait_lock,
    ) as session:
        return session.verify_backup(
            target=target,
            backup_dir=backup_dir,
            expected_alembic_heads=resolved_expected,
            migrations_versions_dir=migrations_dir,
            runner=runner,
        )
