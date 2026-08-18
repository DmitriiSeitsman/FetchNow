"""Make production wrappers must not be redirectable to staging."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REVISION = "a" * 40


def _make_n(target: str, **variables: str) -> str:
    command = ["make", "-n", target]
    command.extend(f"{key}={value}" for key, value in variables.items())
    proc = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_production_preflight_ignores_staging_cli_overrides() -> None:
    output = _make_n(
        "production-release-preflight",
        EXPECTED_REVISION=REVISION,
        PROJECT_NAME="fetchnow-staging",
        COMPOSE_OVERLAY="compose.staging.yaml",
        DEPLOY_ROOT="/srv/fetchnow-staging",
        ENV_FILE=".env.staging",
        BACKUP_ROOT="/srv/fetchnow-staging/backups",
    )
    assert "--project-name fetchnow-production" in output
    assert "--compose-file compose.production.yaml" in output
    assert "--env-file /srv/fetchnow-production/env/.env.production" in output
    assert "--deploy-root /srv/fetchnow-production" in output
    assert "--backup-root /srv/fetchnow-production/backups" in output
    assert "fetchnow-staging" not in output
    assert "compose.staging.yaml" not in output


def test_production_rollout_and_backup_wrappers_stay_production() -> None:
    rollout = _make_n(
        "production-release-rollout",
        EXPECTED_REVISION=REVISION,
        PROJECT_NAME="fetchnow-staging",
        DEPLOY_ROOT="/srv/fetchnow-staging",
        ENV_FILE=".env.staging",
    )
    assert "--project-name fetchnow-production" in rollout
    assert "/srv/fetchnow-production" in rollout
    assert "fetchnow-staging" not in rollout

    backup = _make_n(
        "production-pg-backup-create",
        PROJECT_NAME="fetchnow-staging",
        COMPOSE_OVERLAY="compose.staging.yaml",
        BACKUP_ROOT="/srv/fetchnow-staging/backups",
        ENV_FILE=".env.staging",
    )
    assert "--project-name fetchnow-production" in backup
    assert "compose.production.yaml" in backup
    assert "/srv/fetchnow-production/backups" in backup
    assert "compose.staging.yaml" not in backup


def test_staging_prepare_and_deploy_plan_make_do_not_require_backup_root() -> None:
    prepare = _make_n("release-prepare", EXPECTED_REVISION=REVISION)
    assert "--backup-root" not in prepare
    assert "--project-name \"fetchnow-staging\"" in prepare
    assert "--compose-file \"compose.staging.yaml\"" in prepare
    assert "--env-file \".env.staging\"" in prepare

    plan = _make_n("release-deploy-plan", EXPECTED_REVISION=REVISION)
    assert "--backup-root" not in plan
    assert "--project-name \"fetchnow-staging\"" in plan
    assert "--compose-file \"compose.staging.yaml\"" in plan


def test_production_prepare_make_omits_backup_root_deploy_plan_keeps_canonical() -> None:
    prepare = _make_n("production-release-prepare", EXPECTED_REVISION=REVISION)
    assert "--backup-root" not in prepare
    assert "--project-name fetchnow-production" in prepare
    assert "--compose-file compose.production.yaml" in prepare

    plan = _make_n("production-release-deploy-plan", EXPECTED_REVISION=REVISION)
    assert "--backup-root /srv/fetchnow-production/backups" in plan
    assert "--project-name fetchnow-production" in plan
    assert "fetchnow-staging" not in plan
