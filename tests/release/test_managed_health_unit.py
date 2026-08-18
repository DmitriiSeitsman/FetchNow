"""Focused managed standalone health and Make target coverage."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fetchnow_release import cli as release_cli  # noqa: E402
from fetchnow_release.health import HealthInput, run_health  # noqa: E402
from fetchnow_release.health_project import (  # noqa: E402
    assert_isolated_tag_health_project,
)
from fetchnow_release.http_health import HttpCheckResult  # noqa: E402
from fetchnow_release.image_identity import ImageIdentityError  # noqa: E402
from fetchnow_release.override import image_ids_from_manifest  # noqa: E402

REVISION = "a" * 40
OTHER_REVISION = "b" * 40
PROJECT = "fetchnow-staging"
HEALTH_TEST_PROJECT = "fetchnow-health-test-abcdef12"


def _image_ids() -> dict[str, str]:
    api = "sha256:" + ("1" * 64)
    return {
        "api": api,
        "worker": api,
        "web": "sha256:" + ("2" * 64),
        "gateway": "sha256:" + ("3" * 64),
    }


def _manifest(revision: str = REVISION) -> dict[str, object]:
    ids = _image_ids()
    return {
        "schema_version": 1,
        "source_contract_version": 1,
        "revision": revision,
        "tree_oid": "c" * 40,
        "created_at_utc": "2026-01-01T00:00:00Z",
        "archive_commit_verified": True,
        "source_repository": "FetchNow",
        "compose_version": "2",
        "docker_version": "29",
        "build_platform": "linux/amd64",
        "images": [
            {
                "service": service,
                "reference": f"fetchnow-{service}:{revision}",
                "image_id": ids[service],
                "oci_revision": revision,
                "platform": "linux/amd64",
            }
            for service in ("api", "web", "gateway")
        ],
        "contract_hashes": {},
        "tool_version": "1.2.0",
        "status": "prepared",
    }


def _current(
    manifest_hash: str,
    *,
    revision: str = REVISION,
    image_ids: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "application": {
            "revision": revision,
            "release_manifest_sha256": manifest_hash,
            "image_ids": image_ids or _image_ids(),
            "deployment_id": "12345678-1234-1234-1234-123456789abc",
        },
        "database": {
            "heads": ["0001_baseline"],
            "schema_release_revision": revision,
            "last_migration_id": None,
            "compatibility_contract_sha256": None,
            "compatible_application_revisions": [revision],
        },
        "updated_at_utc": "2026-01-01T00:00:00Z",
    }


def _layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    deploy = tmp_path / "deploy"
    release = deploy / "releases" / REVISION
    state = deploy / "state"
    repo.mkdir()
    release.mkdir(parents=True)
    state.mkdir()
    manifest_bytes = (json.dumps(_manifest(), indent=2, sort_keys=True) + "\n").encode()
    (release / "release.json").write_bytes(manifest_bytes)
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    (state / "current.json").write_text(
        json.dumps(_current(manifest_hash), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    env = tmp_path / "external.env"
    env.write_text("X=1\n", encoding="utf-8")
    return repo, deploy, env


def _patch_healthy_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    running_ids: dict[str, str] | None = None,
    project_name: str = HEALTH_TEST_PROJECT,
) -> None:
    ids = running_ids or _image_ids()
    monkeypatch.setattr("fetchnow_release.health.require_compose_v2", lambda: None)
    monkeypatch.setattr("fetchnow_release.health.require_docker_daemon", lambda: None)
    monkeypatch.setattr(
        "fetchnow_release.health.compose_files_include_delivery", lambda _paths: True
    )
    monkeypatch.setattr(
        "fetchnow_release.health.assert_release_images_present",
        lambda manifest: image_ids_from_manifest(manifest),
    )
    monkeypatch.setattr(
        "fetchnow_release.health._ps_services",
        lambda _inp: [
            {
                "Service": service,
                "State": "running",
                "Health": "" if service == "worker" else "healthy",
                "ID": f"cid-{service}",
                # Managed health must not require this to contain a revision tag.
                "Image": (
                    ids["api"]
                    if service == "delivery"
                    else ids.get(service, "postgres:16.9-alpine")
                ),
            }
            for service in ("gateway", "api", "worker", "delivery", "postgres", "web")
        ],
    )

    def inspect(container_id: str) -> dict[str, object]:
        service = container_id.removeprefix("cid-")
        image_id = (
            ids["api"]
            if service == "delivery"
            else ids.get(service, "sha256:" + ("9" * 64))
        )
        state: dict[str, object] = {"Status": "running"}
        if service != "worker":
            state["Health"] = {"Status": "healthy"}
        return {
            "Config": {
                "Labels": {
                    "com.docker.compose.project": project_name,
                    "com.docker.compose.service": service,
                    "org.opencontainers.image.revision": REVISION,
                },
                "Image": image_id,
            },
            "Image": image_id,
            "State": state,
            "RestartCount": 0,
        }

    monkeypatch.setattr("fetchnow_release.health._inspect", inspect)
    monkeypatch.setattr(
        "fetchnow_release.health.check_endpoint",
        lambda _base, path: HttpCheckResult(path, 1, 200, True, "ok"),
    )


def _run_managed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo, deploy, env = _layout(tmp_path)
    _patch_healthy_runtime(monkeypatch)
    return run_health(
        HealthInput(
            project_name=HEALTH_TEST_PROJECT,
            env_file=env,
            compose_files=(repo / "compose.yaml",),
            expected_revision=REVISION,
            repo_root=repo,
            deploy_root=deploy,
        )
    )


def test_managed_id_pinned_container_passes_without_revision_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run_managed(tmp_path, monkeypatch)
    assert result.ok, result.messages


def test_managed_current_revision_mismatch_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, deploy, env = _layout(tmp_path)
    current_path = deploy / "state" / "current.json"
    current = json.loads(current_path.read_text(encoding="utf-8"))
    current["application"]["revision"] = OTHER_REVISION
    current["database"]["schema_release_revision"] = OTHER_REVISION
    current["database"]["compatible_application_revisions"] = [OTHER_REVISION]
    current_path.write_text(json.dumps(current), encoding="utf-8")
    _patch_healthy_runtime(monkeypatch)
    result = run_health(
        HealthInput(HEALTH_TEST_PROJECT, env, (repo / "compose.yaml",), REVISION, repo, deploy_root=deploy)
    )
    assert not result.ok
    assert "revision does not match" in result.messages[0]


def test_managed_current_manifest_image_id_mismatch_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, deploy, env = _layout(tmp_path)
    current_path = deploy / "state" / "current.json"
    current = json.loads(current_path.read_text(encoding="utf-8"))
    current["application"]["image_ids"]["web"] = "sha256:" + ("4" * 64)
    current_path.write_text(json.dumps(current), encoding="utf-8")
    _patch_healthy_runtime(monkeypatch)
    result = run_health(
        HealthInput(HEALTH_TEST_PROJECT, env, (repo / "compose.yaml",), REVISION, repo, deploy_root=deploy)
    )
    assert not result.ok
    assert "do not match release manifest" in result.messages[0]


def test_managed_manifest_hash_mismatch_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, deploy, env = _layout(tmp_path)
    current_path = deploy / "state" / "current.json"
    current = json.loads(current_path.read_text(encoding="utf-8"))
    current["application"]["release_manifest_sha256"] = "d" * 64
    current_path.write_text(json.dumps(current), encoding="utf-8")
    _patch_healthy_runtime(monkeypatch)
    result = run_health(
        HealthInput(HEALTH_TEST_PROJECT, env, (repo / "compose.yaml",), REVISION, repo, deploy_root=deploy)
    )
    assert not result.ok
    assert "release-manifest hash mismatch" in result.messages[0]


@pytest.mark.parametrize(
    "state_kind", ["missing", "invalid", "invalid_image_ids", "legacy"]
)
def test_managed_missing_invalid_or_legacy_current_fails(
    state_kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, deploy, env = _layout(tmp_path)
    current_path = deploy / "state" / "current.json"
    if state_kind == "missing":
        current_path.unlink()
    elif state_kind == "invalid":
        current_path.write_text('{"schema_version": 2, "application": {}}', encoding="utf-8")
    elif state_kind == "invalid_image_ids":
        current = json.loads(current_path.read_text(encoding="utf-8"))
        current["application"]["image_ids"] = "not-an-object"
        current_path.write_text(json.dumps(current), encoding="utf-8")
    else:
        current_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "revision": REVISION,
                    "release_manifest_sha256": "d" * 64,
                    "image_ids": _image_ids(),
                    "updated_at_utc": "2026-01-01T00:00:00Z",
                    "deployment_id": "12345678-1234-1234-1234-123456789abc",
                }
            ),
            encoding="utf-8",
        )
    _patch_healthy_runtime(monkeypatch)
    result = run_health(
        HealthInput(HEALTH_TEST_PROJECT, env, (repo / "compose.yaml",), REVISION, repo, deploy_root=deploy)
    )
    assert not result.ok
    assert result.messages[0].startswith("FAIL:")


def test_managed_local_image_oci_mismatch_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, deploy, env = _layout(tmp_path)
    _patch_healthy_runtime(monkeypatch)

    def fail_oci(_manifest):  # noqa: ANN001
        raise ImageIdentityError("gateway OCI revision mismatch")

    monkeypatch.setattr("fetchnow_release.health.assert_release_images_present", fail_oci)
    result = run_health(
        HealthInput(HEALTH_TEST_PROJECT, env, (repo / "compose.yaml",), REVISION, repo, deploy_root=deploy)
    )
    assert not result.ok
    assert "OCI revision mismatch" in result.messages[0]


def test_managed_running_image_id_mismatch_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, deploy, env = _layout(tmp_path)
    wrong = _image_ids()
    wrong["gateway"] = "sha256:" + ("5" * 64)
    _patch_healthy_runtime(monkeypatch, running_ids=wrong)
    result = run_health(
        HealthInput(HEALTH_TEST_PROJECT, env, (repo / "compose.yaml",), REVISION, repo, deploy_root=deploy)
    )
    assert not result.ok
    assert "gateway image ID" in result.messages[0]


def test_isolated_tag_mode_still_passes_for_health_test_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    env = tmp_path / "legacy.env"
    env.write_text("X=1\n", encoding="utf-8")
    _patch_healthy_runtime(monkeypatch, project_name=HEALTH_TEST_PROJECT)
    tagged = {
        "api": f"fetchnow-api:{REVISION}",
        "worker": f"fetchnow-api:{REVISION}",
        "delivery": f"fetchnow-api:{REVISION}",
        "web": f"fetchnow-web:{REVISION}",
        "gateway": f"fetchnow-gateway:{REVISION}",
    }
    original_ps = [
        {
            "Service": service,
            "State": "running",
            "Health": "" if service == "worker" else "healthy",
            "ID": f"cid-{service}",
            "Image": tagged.get(service, "postgres:16.9-alpine"),
        }
        for service in ("gateway", "api", "worker", "delivery", "postgres", "web")
    ]
    monkeypatch.setattr("fetchnow_release.health._ps_services", lambda _inp: original_ps)
    result = run_health(
        HealthInput(
            HEALTH_TEST_PROJECT, env, (repo / "compose.yaml",), REVISION, repo
        )
    )
    assert result.ok, result.messages


@pytest.mark.parametrize(
    "project",
    [
        "fetchnow-staging",
        "fetchnow-prod",
        "fetchnow-production",
        "fetchnow",
        "fetchnow-health-test",
        "fetchnow-health-test-SHORT",
        "fetchnow-rollout-test-abcdef12",
    ],
)
def test_protected_or_invalid_projects_reject_tag_mode_without_deploy_root(
    project: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    env = tmp_path / "env"
    env.write_text("X=1\n", encoding="utf-8")
    _patch_healthy_runtime(monkeypatch, project_name=project)
    result = run_health(
        HealthInput(project, env, (repo / "compose.yaml",), REVISION, repo)
    )
    assert not result.ok
    assert "tag-mode" in result.messages[0] or "deploy-root" in result.messages[0]


def test_cli_protected_project_without_deploy_root_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    env = tmp_path / "env"
    env.write_text("X=1\n", encoding="utf-8")
    compose = repo / "compose.yaml"
    compose.write_text("services: {}\n", encoding="utf-8")
    _patch_healthy_runtime(monkeypatch)
    rc = release_cli.main(
        [
            "health",
            "--project-name",
            PROJECT,
            "--env-file",
            str(env),
            "--compose-file",
            str(compose),
            "--expected-revision",
            REVISION,
            "--repo-root",
            str(repo),
            "--gateway-base-url",
            "http://127.0.0.1:8091",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert "deploy-root" in captured.err
    assert "POSTGRES_PASSWORD" not in captured.err
    assert "DATABASE_URL" not in captured.err


def test_cli_managed_mode_uses_current_state_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, deploy, env = _layout(tmp_path)
    compose = repo / "compose.yaml"
    compose.write_text("services: {}\n", encoding="utf-8")
    _patch_healthy_runtime(monkeypatch)
    rc = release_cli.main(
        [
            "health",
            "--project-name",
            HEALTH_TEST_PROJECT,
            "--env-file",
            str(env),
            "--compose-file",
            str(compose),
            "--expected-revision",
            REVISION,
            "--repo-root",
            str(repo),
            "--deploy-root",
            str(deploy),
            "--gateway-base-url",
            "http://127.0.0.1:8091",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert "OK: health gate passed" in captured.out


def test_managed_health_allows_tooling_revision_skew(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tooling checkout may be newer than deployed revision; health uses deploy root."""
    repo, deploy, env = _layout(tmp_path)
    # Simulate a newer tooling worktree that is not the deployed revision.
    (repo / "HEAD_SIM").write_text(OTHER_REVISION + "\n", encoding="utf-8")
    _patch_healthy_runtime(monkeypatch)
    result = run_health(
        HealthInput(
            HEALTH_TEST_PROJECT,
            env,
            (repo / "compose.yaml",),
            REVISION,
            repo,
            deploy_root=deploy,
        )
    )
    assert result.ok, result.messages
    assert (repo / "HEAD_SIM").read_text(encoding="utf-8").strip() == OTHER_REVISION


def test_internal_expected_image_ids_path_still_allows_staging_without_deploy_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rollout/recover/migrate may pass expected_image_ids without deploy_root."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = tmp_path / "env"
    env.write_text("X=1\n", encoding="utf-8")
    ids = _image_ids()
    _patch_healthy_runtime(monkeypatch, running_ids=ids, project_name=PROJECT)
    result = run_health(
        HealthInput(
            PROJECT,
            env,
            (repo / "compose.yaml",),
            REVISION,
            repo,
            expected_image_ids=ids,
        )
    )
    assert result.ok, result.messages


def test_tag_health_project_validation_rejects_prefix_confusion() -> None:
    with pytest.raises(Exception, match="tag-mode|deploy-root|unsafe"):
        assert_isolated_tag_health_project("fetchnow-staging")
    with pytest.raises(Exception, match="tag-mode|deploy-root|unsafe"):
        assert_isolated_tag_health_project("fetchnow-health-test")
    with pytest.raises(Exception, match="tag-mode|deploy-root|unsafe"):
        assert_isolated_tag_health_project("fetchnow-health-test-../evil")
    assert (
        assert_isolated_tag_health_project(HEALTH_TEST_PROJECT) == HEALTH_TEST_PROJECT
    )


def _make_render(target: str, **variables: str) -> str:
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


def test_make_health_honors_external_env_and_deploy_root(tmp_path: Path) -> None:
    env = tmp_path / "env with spaces" / "staging secrets"
    deploy = tmp_path / "deploy root"
    output = _make_render(
        "release-health",
        ENV_FILE=str(env),
        DEPLOY_ROOT=str(deploy),
        EXPECTED_REVISION=REVISION,
    )
    assert f'--env-file "{env}"' in output
    assert f'--deploy-root "{deploy}"' in output
    assert "--env-file .env.staging" not in output


def test_make_preflight_passes_canonical_roots(tmp_path: Path) -> None:
    env = tmp_path / "external staging env"
    deploy = Path("/srv/fetchnow-staging")
    output = _make_render(
        "release-preflight",
        ENV_FILE=str(env),
        EXPECTED_REVISION=REVISION,
    )
    assert f'--env-file "{env}"' in output
    assert f'--deploy-root "{deploy}"' in output
    assert f'--backup-root "{deploy / "backups"}"' in output


@pytest.mark.parametrize("target", ["release-health", "release-preflight", "release-deploy-plan"])
def test_affected_release_targets_have_no_hardcoded_env(
    target: str, tmp_path: Path
) -> None:
    env = tmp_path / "operator env file"
    output = _make_render(
        target,
        ENV_FILE=str(env),
        DEPLOY_ROOT=str(tmp_path / "deploy"),
        BACKUP_ROOT=str(tmp_path / "backup"),
        EXPECTED_REVISION=REVISION,
    )
    assert f'--env-file "{env}"' in output
    assert "--env-file .env.staging" not in output
