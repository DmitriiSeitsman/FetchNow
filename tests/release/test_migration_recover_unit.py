"""Unit tests for PRD1C3B2B2 migration recovery and blocking gates."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fetchnow_release.c3b2b2_constants import (  # noqa: E402
    MIGRATION_RECOVERY_OUTCOME_ACCEPT_SOURCE,
    MIGRATION_RECOVERY_OUTCOME_RELEASE_HOLD,
    STATUS_MIGRATION_PLANNED,
    STATUS_MIGRATION_REQUIRES_RECOVERY,
)
from fetchnow_release.migration_journal import (  # noqa: E402
    MigrationPlanDocument,
    MigrationResultDocument,
    append_migration_event,
    ensure_migration_layout,
    find_unresolved_migrations,
    migration_dir,
    write_migration_plan,
    write_migration_result,
)
from fetchnow_release.migration_recover import (  # noqa: E402
    MigrationRecoverInput,
    recover_migration,
)


MIGRATION_TEST_PROJECT = "fetchnow-migration-test-abcdef12"


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
        project_name=MIGRATION_TEST_PROJECT,
        compose_files=["compose.yaml", "compose.staging.yaml"],
        created_at_utc="2026-01-01T00:00:00Z",
    )


def test_rollout_source_blocks_unresolved_migrations() -> None:
    rollout_src = (SCRIPTS / "fetchnow_release" / "rollout.py").read_text(encoding="utf-8")
    assert "find_unresolved_migrations" in rollout_src
    assert "unresolved migration journal(s) block application rollout" in rollout_src


def test_recover_rejects_partial_heads(tmp_path: Path) -> None:
    deploy = tmp_path / "deploy"
    ensure_migration_layout(deploy)
    mig_id = "00000000-0000-4000-8000-000000000011"
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
            failure_phase=STATUS_MIGRATION_PLANNED,
            result_sha256=None,
            recorded_at_utc="2026-01-01T00:00:00Z",
        ),
    )
    compose = tmp_path / "compose.yaml"
    overlay = tmp_path / "compose.staging.yaml"
    compose.write_text("services: {}\n")
    overlay.write_text("services: {}\n")
    env = tmp_path / ".env.release-test"
    env.write_text(
        "\n".join(
            [
                f"COMPOSE_PROJECT_NAME={MIGRATION_TEST_PROJECT}",
                f"FETCHNOW_RELEASE_REVISION={'b' * 40}",
                "GATEWAY_PORT=127.0.0.1:8091",
                "APP_ENV=test",
                "PUBLIC_SITE_URL=http://127.0.0.1",
                "POSTGRES_DB=fetchnow",
                "POSTGRES_USER=fetchnow",
                f"POSTGRES_PASSWORD={'x' * 32}",
                f"FETCHNOW_DEPLOY_ROOT={deploy}",
                f"FETCHNOW_BACKUP_ROOT={tmp_path / 'backups'}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(env, 0o600)
    with patch("fetchnow_release.migration_recover.RolloutLock") as lock_cls:
        lock_cls.return_value.__enter__ = MagicMock(return_value=lock_cls.return_value)
        lock_cls.return_value.__exit__ = MagicMock(return_value=False)
        with patch(
            "fetchnow_release.migration_recover.validate_deploy_root",
            return_value=deploy,
        ):
            with patch(
                "fetchnow_release.migration_recover.find_unresolved_deployments",
                return_value=[],
            ):
                with patch(
                    "fetchnow_release.migration_recover.load_and_resolve_current_state",
                    return_value=MagicMock(
                        application=MagicMock(
                            revision="d" * 40,
                            image_ids={
                                "api": "sha256:" + ("a" * 64),
                                "worker": "sha256:" + ("a" * 64),
                                "web": "sha256:" + ("e" * 64),
                                "gateway": "sha256:" + ("f" * 64),
                            },
                        ),
                        database=MagicMock(),
                    ),
                ):
                    with patch(
                        "fetchnow_release.migration_recover.database_heads_via_postgres",
                        return_value=frozenset({"partial"}),
                    ):
                        with patch(
                            "fetchnow_release.migration_recover._validate_active_hold",
                            return_value="00000000-0000-4000-8000-000000000099",
                        ):
                            with patch(
                                "fetchnow_release.migration_recover.run_health",
                                return_value=MagicMock(ok=True, messages=()),
                            ):
                                with patch(
                                    "fetchnow_release.migration_recover.current_path",
                                    return_value=MagicMock(
                                        read_bytes=MagicMock(return_value=b"x")
                                    ),
                                ):
                                    with patch(
                                        "fetchnow_release.migration_recover.sha256_bytes",
                                        return_value="c" * 64,
                                    ):
                                        with patch(
                                            "fetchnow_release.migration_recover.release_dir",
                                            return_value=tmp_path / "release",
                                        ):
                                            result = recover_migration(
                                                MigrationRecoverInput(
                                                    project_name=MIGRATION_TEST_PROJECT,
                                                    env_file=env,
                                                    compose_files=(compose, overlay),
                                                    repo_root=tmp_path,
                                                    deploy_root=deploy,
                                                    backup_root=tmp_path / "backups",
                                                    migration_id=mig_id,
                                                    action=MIGRATION_RECOVERY_OUTCOME_ACCEPT_SOURCE,
                                                )
                                            )
    assert not result.ok
    assert "neither planned source nor target" in result.messages[0]


def test_recover_release_hold_requires_committed_result(tmp_path: Path) -> None:
    deploy = tmp_path / "deploy"
    ensure_migration_layout(deploy)
    mig_id = "00000000-0000-4000-8000-000000000012"
    mig = migration_dir(deploy, mig_id)
    mig.mkdir(mode=0o700)
    write_migration_plan(mig / "plan.json", _plan(mig_id))
    env = tmp_path / ".env"
    env.write_text(
        f"FETCHNOW_BACKUP_ROOT={tmp_path / 'backups'}\n",
        encoding="utf-8",
    )
    os.chmod(env, 0o600)
    with patch(
        "fetchnow_release.migration_recover.validate_deploy_root",
        return_value=deploy,
    ):
        result = recover_migration(
            MigrationRecoverInput(
                project_name=MIGRATION_TEST_PROJECT,
                env_file=env,
                compose_files=(tmp_path / "compose.yaml",),
                repo_root=tmp_path,
                deploy_root=deploy,
                backup_root=tmp_path / "backups",
                migration_id=mig_id,
                action=MIGRATION_RECOVERY_OUTCOME_RELEASE_HOLD,
            )
        )
    assert not result.ok
    assert "committed migration result" in result.messages[0]


def test_requires_recovery_journal_blocks_mutation(tmp_path: Path) -> None:
    deploy = tmp_path / "deploy"
    ensure_migration_layout(deploy)
    mig_id = "00000000-0000-4000-8000-000000000010"
    mig = migration_dir(deploy, mig_id)
    mig.mkdir(mode=0o700)
    write_migration_plan(mig / "plan.json", _plan(mig_id))
    write_migration_result(
        mig,
        MigrationResultDocument(
            schema_version=1,
            migration_id=mig_id,
            status=STATUS_MIGRATION_REQUIRES_RECOVERY,
            failure_phase=STATUS_MIGRATION_PLANNED,
            result_sha256=None,
            recorded_at_utc="2026-01-01T00:00:00Z",
        ),
    )
    assert find_unresolved_migrations(deploy) == [mig_id]
