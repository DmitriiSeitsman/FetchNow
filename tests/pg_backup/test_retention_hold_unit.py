"""Unit tests for PRD1C3B2B2 backup retention holds."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fetchnow_pg_backup import DUMP_FILENAME, MANIFEST_FILENAME  # noqa: E402
from fetchnow_pg_backup.manifest import ManifestV1, StructuralValidation  # noqa: E402
from fetchnow_pg_backup.prune import prune_backups, select_prune_candidates  # noqa: E402
from fetchnow_pg_backup.retention_hold import (  # noqa: E402
    RetentionHoldAcquired,
    RetentionHoldError,
    RetentionHoldReleased,
    active_hold_for_migration,
    hold_is_active,
    new_hold_id,
    publish_acquired_hold,
    publish_released_hold,
)


def _manifest(backup_id: str) -> ManifestV1:
    return ManifestV1(
        schema_version=1,
        backup_id=backup_id,
        created_at_utc="2026-01-01T00:00:00Z",
        compose_project="fetchnow-migration-test-deadbeef",
        database_name="fetchnow",
        postgres_version="16.9",
        pg_dump_version="16.9",
        archive_format="custom",
        compression="gzip:6",
        dump_filename=DUMP_FILENAME,
        dump_size_bytes=4,
        sha256="a" * 64,
        git_revision="deadbeef",
        structural_validation=StructuralValidation(True, "pg_restore -l", 1),
        duration_seconds=1.0,
        tool_version="test",
    )


def _acquired(*, backup_id: str, migration_id: str) -> RetentionHoldAcquired:
    return RetentionHoldAcquired(
        schema_version=1,
        hold_id=new_hold_id(),
        backup_id=backup_id,
        backup_manifest_sha256="a" * 64,
        backup_dump_sha256="a" * 64,
        migration_id=migration_id,
        attestation_schema_version=2,
        attestation_path="verifications/test.json",
        attestation_sha256="b" * 64,
        expected_alembic_heads=("001",),
        static_alembic_heads=("001",),
        verified_restored_heads=("001",),
        migrations_graph_sha256="c" * 64,
        acquired_at_utc="2026-01-01T00:00:00Z",
        tool_version="test",
    )


def _write_backup(root: Path, backup_id: str) -> Path:
    directory = root / backup_id
    directory.mkdir(parents=True)
    manifest = _manifest(backup_id)
    (directory / MANIFEST_FILENAME).write_text(manifest.to_json(), encoding="utf-8")
    (directory / DUMP_FILENAME).write_bytes(b"data")
    return directory


def test_active_hold_blocks_prune_selection(tmp_path: Path) -> None:
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    older = "20260101T000000Z-test"
    newer = "20260101T000001Z-test"
    _write_backup(backup_root, older)
    _write_backup(backup_root, newer)
    acquired = _acquired(
        backup_id=older, migration_id="00000000-0000-4000-8000-000000000001"
    )
    publish_acquired_hold(backup_root=backup_root, acquired=acquired)
    assert hold_is_active(backup_root, acquired.hold_id)
    decisions = select_prune_candidates(
        backup_root=backup_root, repo_root=repo_root, keep=1
    )
    assert any(
        d.backup_id == older
        and d.action == "keep"
        and "active retention hold" in d.reason
        for d in decisions
    )


def test_released_hold_allows_prune_apply(tmp_path: Path) -> None:
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    older = "20260101T000001Z-test"
    newer = "20260101T000002Z-test"
    _write_backup(backup_root, older)
    _write_backup(backup_root, newer)
    acquired = _acquired(
        backup_id=older, migration_id="00000000-0000-4000-8000-000000000002"
    )
    publish_acquired_hold(backup_root=backup_root, acquired=acquired)
    publish_released_hold(
        backup_root=backup_root,
        released=RetentionHoldReleased(
            schema_version=1,
            hold_id=acquired.hold_id,
            released_at_utc="2026-01-01T00:01:00Z",
            reason="test",
            tool_version="test",
        ),
    )
    assert not hold_is_active(backup_root, acquired.hold_id)
    result = prune_backups(
        backup_root=backup_root, repo_root=repo_root, keep=1, apply=True
    )
    assert older in result.deleted
    assert newer not in result.deleted


def test_active_hold_for_migration(tmp_path: Path) -> None:
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    backup_id = "20260101T000002Z-test"
    _write_backup(backup_root, backup_id)
    migration_id = "00000000-0000-4000-8000-000000000003"
    acquired = _acquired(backup_id=backup_id, migration_id=migration_id)
    publish_acquired_hold(backup_root=backup_root, acquired=acquired)
    found = active_hold_for_migration(backup_root, migration_id)
    assert found is not None
    assert found.hold_id == acquired.hold_id


def test_hold_acquire_immutable(tmp_path: Path) -> None:
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    acquired = _acquired(
        backup_id="20260101T000003Z-test",
        migration_id="00000000-0000-4000-8000-000000000004",
    )
    publish_acquired_hold(backup_root=backup_root, acquired=acquired)
    with pytest.raises(RetentionHoldError, match="already exists"):
        publish_acquired_hold(backup_root=backup_root, acquired=acquired)
