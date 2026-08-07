"""Backup creation (atomic publish)."""

from __future__ import annotations

import os
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from . import (
    DEFAULT_COMPRESSION,
    DUMP_FILENAME,
    INCOMPLETE_PREFIX,
    MANIFEST_FILENAME,
    VERIFICATIONS_DIRNAME,
    __version__,
)
from .checksums import sha256_file
from .compose_target import ComposeTarget
from .free_space import evaluate_free_space
from .fs_safety import (
    PathSafetyError,
    atomic_publish_directory,
    chmod_dir_0700,
    chmod_file_0600,
    ensure_backup_root,
    fsync_dir,
    fsync_file,
    remove_directory_tree,
    validate_backup_root,
)
from .manifest import ManifestV1, StructuralValidation, write_manifest
from .postgres_ops import (
    RunFn,
    compose_postgres_healthy,
    compose_ps_postgres_running,
    dump_to_file,
    pg_restore_list,
    read_database_name,
    read_database_size_bytes,
    read_versions,
    require_utilities,
)
from .process_util import run_command
from .session import open_backup_root_session

if TYPE_CHECKING:
    from .session import BackupRootSession


@dataclass(frozen=True)
class CreateResult:
    backup_id: str
    backup_dir: Path
    manifest: ManifestV1


def _git_revision(repo_root: Path) -> str | None:
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    rev = proc.stdout.strip()
    return rev or None


def allocate_backup_id(backup_root: Path, git_revision: str | None) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rev = (git_revision or "unknown").replace("/", "_")
    base = f"{stamp}_{rev}"
    candidate = base
    n = 2
    while (backup_root / candidate).exists():
        candidate = f"{base}_{n}"
        n += 1
        if n > 1000:
            raise RuntimeError("could not allocate unique backup id")
    return candidate


def _create_backup_in_session(
    *,
    session: BackupRootSession,
    target: ComposeTarget,
    compression: str = DEFAULT_COMPRESSION,
    runner: RunFn = run_command,
    skip_free_space: bool = False,
) -> CreateResult:
    os.umask(0o077)
    root = session.backup_root
    repo_root = session.repo_root
    incomplete: Path | None = None
    compose_ps_postgres_running(target, runner=runner)
    compose_postgres_healthy(target, runner=runner)
    require_utilities(target, runner=runner)
    versions = read_versions(target, runner=runner)
    db_name = read_database_name(target, runner=runner)

    if not skip_free_space:
        db_size = read_database_size_bytes(target, runner=runner)
        usage = shutil.disk_usage(root)
        decision = evaluate_free_space(database_bytes=db_size, free_bytes=usage.free)
        if not decision.ok:
            raise PathSafetyError(decision.reason)

    git_rev = _git_revision(repo_root)
    backup_id = allocate_backup_id(root, git_rev)
    final_dir = root / backup_id
    if final_dir.exists():
        raise PathSafetyError(f"backup id already exists: {backup_id}")

    incomplete = root / f"{INCOMPLETE_PREFIX}{uuid.uuid4().hex}"
    incomplete.mkdir(mode=0o700)
    chmod_dir_0700(incomplete)
    (incomplete / VERIFICATIONS_DIRNAME).mkdir(mode=0o700)
    chmod_dir_0700(incomplete / VERIFICATIONS_DIRNAME)

    dump_path = incomplete / DUMP_FILENAME
    started = time.monotonic()
    try:
        dump_to_file(
            target,
            dump_path=dump_path,
            compression=compression,
            runner=runner if runner is not run_command else None,
        )
        chmod_file_0600(dump_path)
        fsync_file(dump_path)
        size = dump_path.stat().st_size
        if size <= 0:
            raise RuntimeError("dump file is empty")
        digest = sha256_file(dump_path)
        entry_count, _toc = pg_restore_list(
            target, dump_host_path=dump_path, runner=runner
        )
        structural = StructuralValidation(
            ok=True,
            tool="pg_restore -l",
            entry_count=entry_count,
        )
        duration = time.monotonic() - started
        manifest = ManifestV1(
            schema_version=1,
            backup_id=backup_id,
            created_at_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            compose_project=target.project_name,
            database_name=db_name,
            postgres_version=versions.server,
            pg_dump_version=versions.pg_dump,
            archive_format="custom",
            compression=compression,
            dump_filename=DUMP_FILENAME,
            dump_size_bytes=size,
            sha256=digest,
            git_revision=git_rev,
            structural_validation=structural,
            duration_seconds=round(duration, 3),
            tool_version=__version__,
        )
        manifest_path = incomplete / MANIFEST_FILENAME
        write_manifest(manifest_path, manifest)
        chmod_file_0600(manifest_path)
        fsync_file(manifest_path)
        fsync_dir(incomplete)
        atomic_publish_directory(incomplete_dir=incomplete, final_dir=final_dir)
        incomplete = None
        fsync_dir(root)
        return CreateResult(backup_id=backup_id, backup_dir=final_dir, manifest=manifest)
    finally:
        if incomplete is not None and incomplete.exists():
            remove_directory_tree(incomplete)


def create_backup(
    *,
    target: ComposeTarget,
    backup_root: Path,
    repo_root: Path,
    compression: str = DEFAULT_COMPRESSION,
    wait_lock: bool = False,
    runner: RunFn = run_command,
    skip_free_space: bool = False,
) -> CreateResult:
    ensure_backup_root(validate_backup_root(backup_root, repo_root=repo_root))
    with open_backup_root_session(
        backup_root=backup_root,
        repo_root=repo_root,
        wait_lock=wait_lock,
    ) as session:
        return session.create_backup(
            target=target,
            compression=compression,
            runner=runner,
            skip_free_space=skip_free_space,
        )
