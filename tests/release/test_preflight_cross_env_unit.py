"""Cross-environment preflight / runtime fail-closed tests (PRD1D-B)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fetchnow_release.image_build import build_application_images  # noqa: E402
from fetchnow_release.preflight import PreflightInput, run_preflight  # noqa: E402
from fetchnow_release.rollout import RolloutInput, rollout_release  # noqa: E402
from password_fixture import valid_test_password  # noqa: E402


def _env_text(
    *,
    project: str,
    app_env: str,
    url: str,
    deploy: str,
    backup: str,
    revision: str,
    gateway: str = "127.0.0.1:8091",
) -> str:
    return "\n".join(
        [
            f"COMPOSE_PROJECT_NAME={project}",
            f"FETCHNOW_RELEASE_REVISION={revision}",
            f"GATEWAY_PORT={gateway}",
            f"APP_ENV={app_env}",
            f"PUBLIC_SITE_URL={url}",
            "POSTGRES_DB=fetchnow",
            "POSTGRES_USER=fetchnow",
            f"POSTGRES_PASSWORD={valid_test_password()}",
            f"FETCHNOW_DEPLOY_ROOT={deploy}",
            f"FETCHNOW_BACKUP_ROOT={backup}",
            "",
        ]
    )


def _write_env(path: Path, **kwargs: str) -> Path:
    kwargs.setdefault("revision", "a" * 40)
    path.write_text(_env_text(**kwargs), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def test_preflight_rejects_production_with_staging_overlay(tmp_path: Path) -> None:
    env = _write_env(
        tmp_path / ".env.production",
        project="fetchnow-production",
        app_env="production",
        url="https://fetchnow.online",
        deploy="/srv/fetchnow-production",
        backup="/srv/fetchnow-production/backups",
    )
    compose = tmp_path / "compose.yaml"
    overlay = tmp_path / "compose.staging.yaml"
    compose.write_text("services: {}\n")
    overlay.write_text("services: {}\n")
    result = run_preflight(
        PreflightInput(
            project_name="fetchnow-production",
            env_file=env,
            compose_files=(compose, overlay),
            expected_revision="a" * 40,
            repo_root=tmp_path,
            deploy_root=Path("/srv/fetchnow-production"),
            backup_root=Path("/srv/fetchnow-production/backups"),
        )
    )
    assert not result.ok
    assert "overlay" in result.messages[0]


def test_preflight_rejects_staging_with_production_overlay(tmp_path: Path) -> None:
    env = _write_env(
        tmp_path / ".env.staging",
        project="fetchnow-staging",
        app_env="staging",
        url="https://staging.fetchnow.online",
        deploy="/srv/fetchnow-staging",
        backup="/srv/fetchnow-staging/backups",
    )
    compose = tmp_path / "compose.yaml"
    overlay = tmp_path / "compose.production.yaml"
    compose.write_text("services: {}\n")
    overlay.write_text("services: {}\n")
    result = run_preflight(
        PreflightInput(
            project_name="fetchnow-staging",
            env_file=env,
            compose_files=(compose, overlay),
            expected_revision="a" * 40,
            repo_root=tmp_path,
            deploy_root=Path("/srv/fetchnow-staging"),
            backup_root=Path("/srv/fetchnow-staging/backups"),
        )
    )
    assert not result.ok
    assert "overlay" in result.messages[0]


@pytest.mark.parametrize(
    ("project", "env_name", "app_env", "url", "cli_root", "canonical", "needle"),
    [
        (
            "fetchnow-production",
            ".env.production",
            "production",
            "https://fetchnow.online",
            "/srv/fetchnow-staging",
            "/srv/fetchnow-production",
            "deploy root",
        ),
        (
            "fetchnow-staging",
            ".env.staging",
            "staging",
            "https://staging.fetchnow.online",
            "/srv/fetchnow-production",
            "/srv/fetchnow-staging",
            "deploy root",
        ),
    ],
)
def test_preflight_rejects_swapped_deploy_roots(
    tmp_path: Path,
    project: str,
    env_name: str,
    app_env: str,
    url: str,
    cli_root: str,
    canonical: str,
    needle: str,
) -> None:
    overlay_name = (
        "compose.production.yaml"
        if project == "fetchnow-production"
        else "compose.staging.yaml"
    )
    env = _write_env(
        tmp_path / env_name,
        project=project,
        app_env=app_env,
        url=url,
        deploy=canonical,
        backup=f"{canonical}/backups",
    )
    compose = tmp_path / "compose.yaml"
    overlay = tmp_path / overlay_name
    compose.write_text("services: {}\n")
    overlay.write_text("services: {}\n")
    result = run_preflight(
        PreflightInput(
            project_name=project,
            env_file=env,
            compose_files=(compose, overlay),
            expected_revision="a" * 40,
            repo_root=tmp_path,
            deploy_root=Path(cli_root),
            backup_root=Path(f"{canonical}/backups"),
        )
    )
    assert not result.ok
    assert needle in result.messages[0]


@pytest.mark.parametrize(
    ("project", "env_name", "app_env", "url", "overlay"),
    [
        (
            "fetchnow-production",
            ".env.production",
            "staging",
            "https://fetchnow.online",
            "compose.production.yaml",
        ),
        (
            "fetchnow-staging",
            ".env.staging",
            "production",
            "https://staging.fetchnow.online",
            "compose.staging.yaml",
        ),
        (
            "fetchnow-production",
            ".env.production",
            "production",
            "https://staging.fetchnow.online",
            "compose.production.yaml",
        ),
        (
            "fetchnow-staging",
            ".env.staging",
            "staging",
            "https://fetchnow.online",
            "compose.staging.yaml",
        ),
    ],
)
def test_preflight_rejects_app_env_and_url_mix(
    tmp_path: Path,
    project: str,
    env_name: str,
    app_env: str,
    url: str,
    overlay: str,
) -> None:
    canonical = (
        "/srv/fetchnow-production"
        if project == "fetchnow-production"
        else "/srv/fetchnow-staging"
    )
    env = _write_env(
        tmp_path / env_name,
        project=project,
        app_env=app_env,
        url=url,
        deploy=canonical,
        backup=f"{canonical}/backups",
    )
    compose = tmp_path / "compose.yaml"
    overlay_path = tmp_path / overlay
    compose.write_text("services: {}\n")
    overlay_path.write_text("services: {}\n")
    result = run_preflight(
        PreflightInput(
            project_name=project,
            env_file=env,
            compose_files=(compose, overlay_path),
            expected_revision="a" * 40,
            repo_root=tmp_path,
            deploy_root=Path(canonical),
            backup_root=Path(f"{canonical}/backups"),
        )
    )
    assert not result.ok


def test_preflight_rejects_production_env_file_on_staging(tmp_path: Path) -> None:
    env = _write_env(
        tmp_path / ".env.production",
        project="fetchnow-staging",
        app_env="staging",
        url="https://staging.fetchnow.online",
        deploy="/srv/fetchnow-staging",
        backup="/srv/fetchnow-staging/backups",
    )
    compose = tmp_path / "compose.yaml"
    overlay = tmp_path / "compose.staging.yaml"
    compose.write_text("services: {}\n")
    overlay.write_text("services: {}\n")
    result = run_preflight(
        PreflightInput(
            project_name="fetchnow-staging",
            env_file=env,
            compose_files=(compose, overlay),
            expected_revision="a" * 40,
            repo_root=tmp_path,
            deploy_root=Path("/srv/fetchnow-staging"),
            backup_root=Path("/srv/fetchnow-staging/backups"),
        )
    )
    assert not result.ok
    assert "env file" in result.messages[0]


def test_preflight_rejects_staging_env_file_on_production(tmp_path: Path) -> None:
    env = _write_env(
        tmp_path / ".env.staging",
        project="fetchnow-production",
        app_env="production",
        url="https://fetchnow.online",
        deploy="/srv/fetchnow-production",
        backup="/srv/fetchnow-production/backups",
    )
    compose = tmp_path / "compose.yaml"
    overlay = tmp_path / "compose.production.yaml"
    compose.write_text("services: {}\n")
    overlay.write_text("services: {}\n")
    result = run_preflight(
        PreflightInput(
            project_name="fetchnow-production",
            env_file=env,
            compose_files=(compose, overlay),
            expected_revision="a" * 40,
            repo_root=tmp_path,
            deploy_root=Path("/srv/fetchnow-production"),
            backup_root=Path("/srv/fetchnow-production/backups"),
        )
    )
    assert not result.ok
    assert "env file" in result.messages[0]


@pytest.mark.parametrize(
    "gateway",
    ["80", "0.0.0.0:8091", "*:8091", "127.0.0.1:notaport", "127.0.0.1:0", "127.0.0.1:65536"],
)
def test_preflight_rejects_unsafe_gateway(tmp_path: Path, gateway: str) -> None:
    env = _write_env(
        tmp_path / ".env.release-test",
        project="fetchnow-rollout-test-abcd1234",
        app_env="test",
        url="https://example.test",
        deploy=str(tmp_path / "deploy"),
        backup=str(tmp_path / "backup"),
        gateway=gateway,
    )
    (tmp_path / "deploy").mkdir()
    (tmp_path / "backup").mkdir()
    compose = tmp_path / "compose.yaml"
    overlay = tmp_path / "compose.staging.yaml"
    compose.write_text("services: {}\n")
    overlay.write_text("services: {}\n")
    result = run_preflight(
        PreflightInput(
            project_name="fetchnow-rollout-test-abcd1234",
            env_file=env,
            compose_files=(compose, overlay),
            expected_revision="a" * 40,
            repo_root=tmp_path,
        )
    )
    assert not result.ok
    assert "GATEWAY_PORT" in result.messages[0]


def _mock_preflight_downstream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fetchnow_release.preflight.assert_clean_worktree", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "fetchnow_release.preflight.assert_revision_in_origin_main", lambda *_a, **_k: None
    )
    monkeypatch.setattr("fetchnow_release.preflight.require_compose_v2", lambda: None)
    monkeypatch.setattr("fetchnow_release.preflight.require_docker_daemon", lambda: None)
    monkeypatch.setattr(
        "fetchnow_release.preflight.validate_operator_root", lambda path, **_k: path
    )
    monkeypatch.setattr(
        "fetchnow_release.preflight.shutil.disk_usage",
        lambda _path: MagicMock(free=2 * 1024 * 1024 * 1024),
    )
    monkeypatch.setattr(
        "fetchnow_release.preflight.compose_config_json", lambda **_k: {"services": {}}
    )
    monkeypatch.setattr(
        "fetchnow_release.preflight.validate_staging_rendered", lambda *_a, **_k: None
    )


def test_staging_prepare_shaped_preflight_without_cli_backup_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_preflight_downstream(monkeypatch)
    env = _write_env(
        tmp_path / ".env.staging",
        project="fetchnow-staging",
        app_env="staging",
        url="https://staging.fetchnow.online",
        deploy="/srv/fetchnow-staging",
        backup="/srv/fetchnow-staging/backups",
    )
    compose = tmp_path / "compose.yaml"
    overlay = tmp_path / "compose.staging.yaml"
    compose.write_text("services: {}\n")
    overlay.write_text("services: {}\n")
    result = run_preflight(
        PreflightInput(
            project_name="fetchnow-staging",
            env_file=env,
            compose_files=(compose, overlay),
            expected_revision="a" * 40,
            repo_root=tmp_path,
            deploy_root=Path("/srv/fetchnow-staging"),
            backup_root=None,
        )
    )
    assert result.ok, result.messages
    assert "OK: deployment preflight passed" in result.messages


def test_production_prepare_shaped_preflight_without_cli_backup_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_preflight_downstream(monkeypatch)
    env = _write_env(
        tmp_path / ".env.production",
        project="fetchnow-production",
        app_env="production",
        url="https://fetchnow.online",
        deploy="/srv/fetchnow-production",
        backup="/srv/fetchnow-production/backups",
    )
    compose = tmp_path / "compose.yaml"
    overlay = tmp_path / "compose.production.yaml"
    compose.write_text("services: {}\n")
    overlay.write_text("services: {}\n")
    result = run_preflight(
        PreflightInput(
            project_name="fetchnow-production",
            env_file=env,
            compose_files=(compose, overlay),
            expected_revision="a" * 40,
            repo_root=tmp_path,
            deploy_root=Path("/srv/fetchnow-production"),
            backup_root=None,
        )
    )
    assert result.ok, result.messages
    assert "OK: deployment preflight passed" in result.messages


def test_preflight_rejects_env_backup_root_mismatch_without_cli_backup(
    tmp_path: Path,
) -> None:
    env = _write_env(
        tmp_path / ".env.staging",
        project="fetchnow-staging",
        app_env="staging",
        url="https://staging.fetchnow.online",
        deploy="/srv/fetchnow-staging",
        backup="/srv/fetchnow-production/backups",
    )
    compose = tmp_path / "compose.yaml"
    overlay = tmp_path / "compose.staging.yaml"
    compose.write_text("services: {}\n")
    overlay.write_text("services: {}\n")
    result = run_preflight(
        PreflightInput(
            project_name="fetchnow-staging",
            env_file=env,
            compose_files=(compose, overlay),
            expected_revision="a" * 40,
            repo_root=tmp_path,
            deploy_root=Path("/srv/fetchnow-staging"),
            backup_root=None,
        )
    )
    assert not result.ok
    assert "FETCHNOW_BACKUP_ROOT" in result.messages[0]
    assert "CLI backup root is required" not in result.messages[0]


def test_preflight_rejects_cli_backup_root_mismatch(tmp_path: Path) -> None:
    env = _write_env(
        tmp_path / ".env.production",
        project="fetchnow-production",
        app_env="production",
        url="https://fetchnow.online",
        deploy="/srv/fetchnow-production",
        backup="/srv/fetchnow-production/backups",
    )
    compose = tmp_path / "compose.yaml"
    overlay = tmp_path / "compose.production.yaml"
    compose.write_text("services: {}\n")
    overlay.write_text("services: {}\n")
    result = run_preflight(
        PreflightInput(
            project_name="fetchnow-production",
            env_file=env,
            compose_files=(compose, overlay),
            expected_revision="a" * 40,
            repo_root=tmp_path,
            deploy_root=Path("/srv/fetchnow-production"),
            backup_root=Path("/srv/fetchnow-staging/backups"),
        )
    )
    assert not result.ok
    assert "CLI backup root" in result.messages[0]


def test_production_prepare_build_argv_excludes_staging_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "compose.yaml").write_text("services: {}\n")
    (source / "compose.production.yaml").write_text("services: {}\n")
    (source / "compose.staging.yaml").write_text("services: {}\n")
    env = tmp_path / ".env.production"
    env.write_text("X=1\n")
    captured: list[list[str]] = []

    def _run(argv, **_kwargs):  # noqa: ANN001
        captured.append(list(argv))
        return MagicMock(returncode=1, stderr="stop-after-argv", stdout="")

    monkeypatch.setattr("fetchnow_release.image_build.assert_tag_collision_safe", lambda *a, **k: ())
    monkeypatch.setattr("fetchnow_release.image_build.subprocess.run", _run)
    with pytest.raises(Exception, match="compose build failed"):
        build_application_images(
            source_dir=source,
            env_file=env,
            revision="a" * 40,
            compose_files=("compose.yaml", "compose.production.yaml"),
        )
    assert captured
    argv = captured[0]
    assert "compose.production.yaml" in " ".join(argv)
    assert "compose.staging.yaml" not in argv


def test_staging_prepare_build_argv_excludes_production_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "compose.yaml").write_text("services: {}\n")
    (source / "compose.production.yaml").write_text("services: {}\n")
    (source / "compose.staging.yaml").write_text("services: {}\n")
    env = tmp_path / ".env.staging"
    env.write_text("X=1\n")
    captured: list[list[str]] = []

    def _run(argv, **_kwargs):  # noqa: ANN001
        captured.append(list(argv))
        return MagicMock(returncode=1, stderr="stop-after-argv", stdout="")

    monkeypatch.setattr("fetchnow_release.image_build.assert_tag_collision_safe", lambda *a, **k: ())
    monkeypatch.setattr("fetchnow_release.image_build.subprocess.run", _run)
    with pytest.raises(Exception, match="compose build failed"):
        build_application_images(
            source_dir=source,
            env_file=env,
            revision="a" * 40,
            compose_files=("compose.yaml", "compose.staging.yaml"),
        )
    assert captured
    joined = " ".join(captured[0])
    assert "compose.staging.yaml" in joined
    assert "compose.production.yaml" not in captured[0]


def test_rollout_rejects_production_manifest_staging_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fetchnow_release.verify_release import VerifyResult

    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / "locks").mkdir()
    (deploy / "state").mkdir()
    (deploy / "deployments").mkdir()
    rev = "c" * 40
    release = deploy / "releases" / rev
    source = release / "source"
    source.mkdir(parents=True)
    (source / "compose.yaml").write_text("services: {}\n")
    (source / "compose.staging.yaml").write_text("services: {}\n")
    (source / "compose.production.yaml").write_text("services: {}\n")

    class _Man:
        source_contract_version = 3
        compose_overlay = "compose.staging.yaml"

    env = _write_env(
        tmp_path / ".env.production",
        project="fetchnow-production",
        app_env="production",
        url="https://fetchnow.online",
        deploy="/srv/fetchnow-production",
        backup="/srv/fetchnow-production/backups",
        revision=rev,
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.validate_deploy_root",
        lambda path, repo_root=None: path,
    )
    monkeypatch.setattr("fetchnow_release.rollout.ensure_rollout_layout", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "fetchnow_release.rollout.verify_prepared_release",
        lambda *_a, **_k: VerifyResult(ok=True, messages=("OK",)),
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.load_and_resolve_current_state",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.find_unresolved_deployments", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        "fetchnow_release.migration_journal.find_unresolved_migrations",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.release_dir",
        lambda _deploy, revision: release,
    )
    monkeypatch.setattr("fetchnow_release.rollout.load_manifest", lambda _p: _Man())
    monkeypatch.setattr(
        "fetchnow_release.rollout.assert_release_images_present",
        lambda _m: {
            "api": "sha256:" + ("1" * 64),
            "worker": "sha256:" + ("1" * 64),
            "web": "sha256:" + ("2" * 64),
            "gateway": "sha256:" + ("3" * 64),
        },
    )
    monkeypatch.setattr("fetchnow_release.rollout.sha256_file", lambda _p: "e" * 64)
    monkeypatch.setattr(
        "fetchnow_release.rollout.RolloutLock",
        lambda *a, **k: __import__("contextlib").nullcontext(),
    )
    result = rollout_release(
        RolloutInput(
            project_name="fetchnow-production",
            env_file=env,
            expected_revision=rev,
            repo_root=tmp_path,
            deploy_root=Path("/srv/fetchnow-production"),
            bootstrap=True,
        )
    )
    assert not result.ok
    assert "compose_overlay" in result.messages[0] or "does not match" in result.messages[0]
