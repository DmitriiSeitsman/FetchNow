"""Unit tests for canonical environment identity (PRD1D-B)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fetchnow_release.environment import (  # noqa: E402
    PRODUCTION,
    STAGING,
    EnvironmentError,
    overlay_from_compose_files,
    real_identity,
    recorded_compose_overlay,
    resolve_runtime_overlay,
    snapshot_compose_files,
    snapshot_compose_files_for_release,
    assert_loopback_gateway_port,
    assert_real_environment_bundle,
)
from fetchnow_release.rollout_project import (  # noqa: E402
    FORBIDDEN,
    assert_rollout_project,
    RolloutProjectError,
)
from fetchnow_release.health_project import (  # noqa: E402
    assert_isolated_tag_health_project,
    HealthProjectError,
)
from fetchnow_pg_backup.integration_project import (  # noqa: E402
    assert_safe_project,
    ProjectNameError,
)


def test_real_identities_are_exact_and_distinct() -> None:
    assert STAGING.project == "fetchnow-staging"
    assert PRODUCTION.project == "fetchnow-production"
    assert STAGING.overlay == "compose.staging.yaml"
    assert PRODUCTION.overlay == "compose.production.yaml"
    assert real_identity("fetchnow-staging") is STAGING
    assert real_identity("fetchnow-production") is PRODUCTION
    assert real_identity("fetchnow-prod") is None
    assert real_identity("fetchnow-rollout-test-abc12345") is None


def test_ephemeral_checkers_reject_real_names() -> None:
    for name in ("fetchnow-staging", "fetchnow-production", "fetchnow-prod"):
        assert name in FORBIDDEN
        with pytest.raises(RolloutProjectError):
            assert_rollout_project(name)
        with pytest.raises(HealthProjectError):
            assert_isolated_tag_health_project(name)
        with pytest.raises(ProjectNameError):
            assert_safe_project(name)


def test_operator_allow_real_accepts_only_canonical_pair() -> None:
    assert assert_rollout_project("fetchnow-staging", allow_real=True) == "fetchnow-staging"
    assert (
        assert_rollout_project("fetchnow-production", allow_real=True)
        == "fetchnow-production"
    )
    with pytest.raises(RolloutProjectError):
        assert_rollout_project("fetchnow-prod", allow_real=True)


@pytest.mark.parametrize(
    "value",
    ["80", "0.0.0.0:8091", "*:8091", "127.0.0.1:notaport", "127.0.0.1:0", "127.0.0.1:65536"],
)
def test_gateway_rejects_non_loopback_and_invalid_ports(value: str) -> None:
    with pytest.raises(EnvironmentError):
        assert_loopback_gateway_port(value)


def test_gateway_accepts_strict_loopback() -> None:
    assert assert_loopback_gateway_port("127.0.0.1:8091") == 8091


def test_overlay_from_compose_files_requires_exactly_one_canonical() -> None:
    files = (
        Path("/tmp/compose.yaml"),
        Path("/tmp/compose.production.yaml"),
    )
    assert overlay_from_compose_files(files) == "compose.production.yaml"
    with pytest.raises(EnvironmentError, match="exactly one"):
        overlay_from_compose_files(
            (
                Path("/tmp/compose.yaml"),
                Path("/tmp/compose.staging.yaml"),
                Path("/tmp/compose.production.yaml"),
            )
        )
    with pytest.raises(EnvironmentError, match="compose.yaml"):
        overlay_from_compose_files((Path("/tmp/compose.production.yaml"),))


def test_v1_v2_recorded_overlay_defaults_to_staging() -> None:
    assert recorded_compose_overlay(1, None) == "compose.staging.yaml"
    assert recorded_compose_overlay(2, None) == "compose.staging.yaml"
    with pytest.raises(EnvironmentError):
        recorded_compose_overlay(2, "compose.production.yaml")


def test_production_rejects_legacy_snapshot() -> None:
    with pytest.raises(EnvironmentError, match="v1/v2"):
        resolve_runtime_overlay(
            project_name="fetchnow-production",
            source_contract_version=2,
            compose_overlay=None,
        )


def test_real_v3_overlay_must_match_identity() -> None:
    with pytest.raises(EnvironmentError, match="does not match"):
        resolve_runtime_overlay(
            project_name="fetchnow-production",
            source_contract_version=3,
            compose_overlay="compose.staging.yaml",
        )
    with pytest.raises(EnvironmentError, match="does not match"):
        resolve_runtime_overlay(
            project_name="fetchnow-staging",
            source_contract_version=3,
            compose_overlay="compose.production.yaml",
        )
    assert (
        resolve_runtime_overlay(
            project_name="fetchnow-production",
            source_contract_version=3,
            compose_overlay="compose.production.yaml",
        )
        == "compose.production.yaml"
    )


def test_ephemeral_v3_uses_recorded_overlay(tmp_path: Path) -> None:
    release = tmp_path / "rel"
    source = release / "source"
    source.mkdir(parents=True)
    (source / "compose.yaml").write_text("services: {}\n")
    (source / "compose.production.yaml").write_text("services: {}\n")

    class _Man:
        source_contract_version = 3
        compose_overlay = "compose.production.yaml"

    files = snapshot_compose_files_for_release(
        release,
        project_name="fetchnow-rollout-test-abcd1234",
        manifest=_Man(),
    )
    assert files[1].name == "compose.production.yaml"


def test_snapshot_compose_files_refuse_unknown_overlay(tmp_path: Path) -> None:
    release = tmp_path / "rel"
    (release / "source").mkdir(parents=True)
    with pytest.raises(EnvironmentError, match="unknown snapshot overlay"):
        snapshot_compose_files(release, overlay="compose.override.yaml")


def test_real_bundle_rejects_cross_env(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.production"
    env_file.write_text("x=1\n")
    env = {
        "COMPOSE_PROJECT_NAME": "fetchnow-production",
        "APP_ENV": "staging",
        "PUBLIC_SITE_URL": "https://fetchnow.online",
        "FETCHNOW_DEPLOY_ROOT": "/srv/fetchnow-production",
        "FETCHNOW_BACKUP_ROOT": "/srv/fetchnow-production/backups",
    }
    with pytest.raises(EnvironmentError, match="APP_ENV"):
        assert_real_environment_bundle(
            project_name="fetchnow-production",
            env=env,
            env_file=env_file,
            compose_files=(
                tmp_path / "compose.yaml",
                tmp_path / "compose.production.yaml",
            ),
            deploy_root=Path("/srv/fetchnow-production"),
            cli_backup_root=Path("/srv/fetchnow-production/backups"),
            require_cli_backup_root=True,
        )


def _real_env(
    *,
    project: str,
    app_env: str,
    url: str,
    deploy: str,
    backup: str,
) -> dict[str, str]:
    return {
        "COMPOSE_PROJECT_NAME": project,
        "APP_ENV": app_env,
        "PUBLIC_SITE_URL": url,
        "FETCHNOW_DEPLOY_ROOT": deploy,
        "FETCHNOW_BACKUP_ROOT": backup,
    }


def test_deploy_plan_shaped_bundle_allows_missing_cli_backup_root(tmp_path: Path) -> None:
    staging_env = tmp_path / ".env.staging"
    staging_env.write_text("x=1\n")
    identity = assert_real_environment_bundle(
        project_name="fetchnow-staging",
        env=_real_env(
            project="fetchnow-staging",
            app_env="staging",
            url="https://staging.fetchnow.online",
            deploy="/srv/fetchnow-staging",
            backup="/srv/fetchnow-staging/backups",
        ),
        env_file=staging_env,
        compose_files=(tmp_path / "compose.yaml", tmp_path / "compose.staging.yaml"),
        deploy_root=Path("/srv/fetchnow-staging"),
        cli_backup_root=None,
        require_cli_backup_root=False,
    )
    assert identity is STAGING

    prod_env = tmp_path / ".env.production"
    prod_env.write_text("x=1\n")
    identity = assert_real_environment_bundle(
        project_name="fetchnow-production",
        env=_real_env(
            project="fetchnow-production",
            app_env="production",
            url="https://fetchnow.online",
            deploy="/srv/fetchnow-production",
            backup="/srv/fetchnow-production/backups",
        ),
        env_file=prod_env,
        compose_files=(
            tmp_path / "compose.yaml",
            tmp_path / "compose.production.yaml",
        ),
        deploy_root=Path("/srv/fetchnow-production"),
        cli_backup_root=None,
        require_cli_backup_root=False,
    )
    assert identity is PRODUCTION


def test_env_backup_root_mismatch_rejected_without_cli_backup(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.staging"
    env_file.write_text("x=1\n")
    with pytest.raises(EnvironmentError, match="FETCHNOW_BACKUP_ROOT"):
        assert_real_environment_bundle(
            project_name="fetchnow-staging",
            env=_real_env(
                project="fetchnow-staging",
                app_env="staging",
                url="https://staging.fetchnow.online",
                deploy="/srv/fetchnow-staging",
                backup="/srv/fetchnow-production/backups",
            ),
            env_file=env_file,
            compose_files=(tmp_path / "compose.yaml", tmp_path / "compose.staging.yaml"),
            deploy_root=Path("/srv/fetchnow-staging"),
            cli_backup_root=None,
            require_cli_backup_root=False,
        )


def test_cli_backup_root_mismatch_rejected_when_supplied(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.production"
    env_file.write_text("x=1\n")
    with pytest.raises(EnvironmentError, match="CLI backup root"):
        assert_real_environment_bundle(
            project_name="fetchnow-production",
            env=_real_env(
                project="fetchnow-production",
                app_env="production",
                url="https://fetchnow.online",
                deploy="/srv/fetchnow-production",
                backup="/srv/fetchnow-production/backups",
            ),
            env_file=env_file,
            compose_files=(
                tmp_path / "compose.yaml",
                tmp_path / "compose.production.yaml",
            ),
            deploy_root=Path("/srv/fetchnow-production"),
            cli_backup_root=Path("/srv/fetchnow-staging/backups"),
            require_cli_backup_root=True,
        )
