"""Unit tests for PRD1C3B2B2 migration journal."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fetchnow_release.c3b2b2_constants import (  # noqa: E402
    MIGRATION_LEGAL_TRANSITIONS,
    STATUS_MIGRATION_BACKUP_STARTED,
    STATUS_MIGRATION_BACKUP_VERIFIED,
    STATUS_MIGRATION_COMMITTED,
    STATUS_MIGRATION_FAILED,
    STATUS_MIGRATION_HOLD_ACQUIRED,
    STATUS_MIGRATION_PLANNED,
    STATUS_MIGRATION_PRE_MIGRATION_REVALIDATED,
    STATUS_MIGRATION_REQUIRES_RECOVERY,
    STATUS_MIGRATION_STARTED,
)
from fetchnow_release.migration_journal import (  # noqa: E402
    MigrationJournalError,
    MigrationPlanDocument,
    append_migration_event,
    assert_legal_migration_transition,
    ensure_migration_layout,
    find_unresolved_migrations,
    migration_dir,
    write_migration_plan,
    write_migration_resolution,
    write_migration_result,
)
from fetchnow_release.migration_journal import MigrationResultDocument  # noqa: E402


def _plan(migration_id: str) -> MigrationPlanDocument:
    api = "sha256:" + ("a" * 64)
    return MigrationPlanDocument(
        schema_version=1,
        migration_id=migration_id,
        target_revision="b" * 40,
        current_state_sha256="c" * 64,
        source_application_revision="d" * 40,
        source_application_image_ids={
            "api": api,
            "worker": api,
            "web": "sha256:" + ("e" * 64),
            "gateway": "sha256:" + ("f" * 64),
        },
        source_database_release_revision="1" * 40,
        source_database_heads=["001"],
        source_migrations_graph_sha256="2" * 64,
        target_database_release_revision="3" * 40,
        target_database_heads=["002"],
        target_migrations_graph_sha256="4" * 64,
        included_revisions=["002"],
        compatibility_contract_sha256="5" * 64,
        target_api_image_id=api,
        backup_root_identity_sha256="6" * 64,
        expected_post_migration_state_sha256="7" * 64,
        project_name="fetchnow-staging",
        compose_files=["compose.yaml"],
        created_at_utc="2026-01-01T00:00:00Z",
    )


def test_migration_legal_transitions() -> None:
    for current, allowed in MIGRATION_LEGAL_TRANSITIONS.items():
        for nxt in MIGRATION_LEGAL_TRANSITIONS:
            if nxt == current:
                continue
            if nxt in allowed:
                assert_legal_migration_transition(current, nxt)
            else:
                with pytest.raises(MigrationJournalError, match="illegal"):
                    assert_legal_migration_transition(current, nxt)


def test_post_mutation_cannot_terminal_fail(tmp_path: Path) -> None:
    deploy = tmp_path / "deploy"
    ensure_migration_layout(deploy)
    mig_id = "00000000-0000-4000-8000-000000000001"
    mig = migration_dir(deploy, mig_id)
    mig.mkdir(mode=0o700)
    write_migration_plan(mig / "plan.json", _plan(mig_id))
    append_migration_event(mig, STATUS_MIGRATION_PLANNED, "plan")
    append_migration_event(mig, STATUS_MIGRATION_BACKUP_STARTED, "backup")
    append_migration_event(mig, STATUS_MIGRATION_BACKUP_VERIFIED, "verified")
    append_migration_event(mig, STATUS_MIGRATION_HOLD_ACQUIRED, "hold")
    append_migration_event(mig, STATUS_MIGRATION_PRE_MIGRATION_REVALIDATED, "revalidated")
    append_migration_event(mig, STATUS_MIGRATION_STARTED, "started")
    with pytest.raises(MigrationJournalError, match="illegal"):
        append_migration_event(mig, STATUS_MIGRATION_FAILED, "must not fail after migration_started")


def test_unresolved_requires_recovery_blocks(tmp_path: Path) -> None:
    deploy = tmp_path / "deploy"
    ensure_migration_layout(deploy)
    mig_id = "00000000-0000-4000-8000-000000000002"
    mig = migration_dir(deploy, mig_id)
    mig.mkdir(mode=0o700)
    write_migration_plan(mig / "plan.json", _plan(mig_id))
    append_migration_event(mig, STATUS_MIGRATION_PLANNED, "plan")
    write_migration_result(
        mig,
        MigrationResultDocument(
            schema_version=1,
            migration_id=mig_id,
            status=STATUS_MIGRATION_REQUIRES_RECOVERY,
            failure_phase=STATUS_MIGRATION_HOLD_ACQUIRED,
            result_sha256=None,
            recorded_at_utc="2026-01-01T00:00:00Z",
        ),
    )
    assert find_unresolved_migrations(deploy) == [mig_id]
    write_migration_resolution(
        mig,
        migration_id=mig_id,
        action="accept_source",
        outcome=STATUS_MIGRATION_FAILED,
        recovery_id="00000000-0000-4000-8000-000000000003",
    )
    assert find_unresolved_migrations(deploy) == []


def test_plan_write_once(tmp_path: Path) -> None:
    deploy = tmp_path / "deploy"
    ensure_migration_layout(deploy)
    mig_id = "00000000-0000-4000-8000-000000000004"
    mig = migration_dir(deploy, mig_id)
    mig.mkdir(mode=0o700)
    plan_path = mig / "plan.json"
    write_migration_plan(plan_path, _plan(mig_id))
    with pytest.raises(MigrationJournalError, match="overwrite"):
        write_migration_plan(plan_path, _plan(mig_id))


def test_committed_not_blocking(tmp_path: Path) -> None:
    deploy = tmp_path / "deploy"
    ensure_migration_layout(deploy)
    mig_id = "00000000-0000-4000-8000-000000000005"
    mig = migration_dir(deploy, mig_id)
    mig.mkdir(mode=0o700)
    write_migration_plan(mig / "plan.json", _plan(mig_id))
    write_migration_result(
        mig,
        MigrationResultDocument(
            schema_version=1,
            migration_id=mig_id,
            status=STATUS_MIGRATION_COMMITTED,
            failure_phase=None,
            result_sha256="7" * 64,
            recorded_at_utc="2026-01-01T00:00:00Z",
        ),
    )
    assert find_unresolved_migrations(deploy) == []
