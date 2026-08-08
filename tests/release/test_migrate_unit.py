"""Unit tests for PRD1C3B2B2 Alembic migration execution helpers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fetchnow_release.alembic_migrate import (  # noqa: E402
    AlembicMigrateError,
    alembic_destination_for_heads,
    build_alembic_upgrade_argv,
)
from fetchnow_release.cli import build_parser  # noqa: E402
from fetchnow_release.migration_project import (  # noqa: E402
    MigrationProjectError,
    assert_migration_project,
    make_migration_project_name,
)


def test_alembic_destination_single_head() -> None:
    assert alembic_destination_for_heads(("abc123",)) == "abc123"


def test_alembic_destination_multi_head() -> None:
    assert alembic_destination_for_heads(("a", "b")) == "heads"


def test_alembic_argv_uses_pull_never(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("x=1\n", encoding="utf-8")
    compose = tmp_path / "compose.yaml"
    compose.write_text("services: {}\n", encoding="utf-8")
    argv = build_alembic_upgrade_argv(
        project_name="fetchnow-staging",
        env_file=env,
        compose_files=(compose,),
        destination="abc123",
    )
    assert argv[:2] == ["docker", "compose"]
    assert "--pull" in argv and "never" in argv
    assert "--no-deps" in argv
    assert argv[-2:] == ["upgrade", "abc123"]
    joined = " ".join(argv).lower()
    assert "password" not in joined


def test_cli_has_migration_commands() -> None:
    parser = build_parser()
    help_text = parser.format_help()
    assert "migrate" in help_text
    assert "recover-migration" in help_text
    for banned in (
        "skip-backup",
        "skip-verify",
        "skip-health",
        "test-mode",
        "allow-downgrade",
    ):
        assert banned not in help_text


def test_migration_project_validation() -> None:
    assert_migration_project("fetchnow-staging", allow_staging=True)
    name = make_migration_project_name()
    assert name.startswith("fetchnow-migration-test-")
    with pytest.raises(MigrationProjectError):
        assert_migration_project("fetchnow-staging")
    with pytest.raises(MigrationProjectError):
        assert_migration_project("fetchnow-prod")


def test_empty_heads_rejected() -> None:
    with pytest.raises(AlembicMigrateError):
        alembic_destination_for_heads(())
