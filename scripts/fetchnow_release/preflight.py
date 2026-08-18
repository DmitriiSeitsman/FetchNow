"""Read-only staging deployment preflight."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from . import (
    MIN_FREE_BYTES,
)
from .compose_validate import ComposeContractError, validate_staging_rendered
from .bootstrap_project import BootstrapProjectError, assert_bootstrap_project
from .deploy_plan_project import DeployPlanProjectError, assert_deploy_plan_project
from .docker_checks import (
    DockerCheckError,
    compose_config_json,
    require_compose_v2,
    require_docker_daemon,
)
from .env_file import EnvFileError, load_env_file, require_keys
from .environment import (
    EnvironmentError,
    PRODUCTION,
    STAGING,
    assert_loopback_gateway_port,
    assert_real_environment_bundle,
    real_identity,
)
from .git_checks import (
    GitCheckError,
    assert_clean_worktree,
    assert_revision_in_origin_main,
)
from .password import PasswordError, validate_staging_password
from .redact import redact
from .revision import RevisionError, validate_full_sha
from .migration_project import MigrationProjectError, assert_migration_project
from .rollout_project import RolloutProjectError, assert_rollout_project
from .roots import RootPathError, validate_operator_root


def _assert_allowed_project(name: str) -> None:
    """Allow real staging/production or validated integration test projects."""
    if real_identity(name) is not None:
        return
    for checker in (
        assert_rollout_project,
        assert_deploy_plan_project,
        assert_migration_project,
        assert_bootstrap_project,
    ):
        try:
            checker(name)
            return
        except (
            RolloutProjectError,
            DeployPlanProjectError,
            MigrationProjectError,
            BootstrapProjectError,
        ):
            continue
    raise PreflightError(
        f"project name must be {STAGING.project!r}, {PRODUCTION.project!r}, "
        f"or a validated fetchnow-rollout-test-* / fetchnow-deploy-plan-test-* / "
        f"fetchnow-migration-test-* / fetchnow-bootstrap-test-* name, got {name!r}"
    )


class PreflightError(ValueError):
    """Aggregated preflight failure."""


REQUIRED_ENV = (
    "COMPOSE_PROJECT_NAME",
    "FETCHNOW_RELEASE_REVISION",
    "GATEWAY_PORT",
    "PUBLIC_SITE_URL",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "FETCHNOW_DEPLOY_ROOT",
    "FETCHNOW_BACKUP_ROOT",
)


@dataclass(frozen=True)
class PreflightInput:
    project_name: str
    env_file: Path
    compose_files: tuple[Path, ...]
    expected_revision: str
    repo_root: Path
    deploy_root: Path | None = None
    backup_root: Path | None = None


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    messages: tuple[str, ...]


def run_preflight(inp: PreflightInput) -> PreflightResult:
    messages: list[str] = []
    try:
        _assert_allowed_project(inp.project_name)
        if not inp.compose_files:
            raise PreflightError("at least one Compose file is required")

        env = load_env_file(inp.env_file)
        require_keys(env, REQUIRED_ENV)
        if env.get("COMPOSE_PROJECT_NAME") != inp.project_name:
            raise PreflightError(
                "COMPOSE_PROJECT_NAME must match --project-name "
                f"({inp.project_name!r})"
            )

        expected = validate_full_sha(inp.expected_revision)
        env_rev = validate_full_sha(env["FETCHNOW_RELEASE_REVISION"])
        if expected != env_rev:
            raise PreflightError(
                "expected revision does not match FETCHNOW_RELEASE_REVISION in env file"
            )

        validate_staging_password(env["POSTGRES_PASSWORD"])

        assert_loopback_gateway_port(env.get("GATEWAY_PORT", ""))
        identity = real_identity(inp.project_name)
        require_deploy = identity is not None
        assert_real_environment_bundle(
            project_name=inp.project_name,
            env=env,
            env_file=inp.env_file,
            compose_files=inp.compose_files,
            deploy_root=inp.deploy_root,
            cli_backup_root=inp.backup_root,
            require_cli_backup_root=False,
            require_deploy_root=require_deploy,
        )

        assert_clean_worktree(inp.repo_root)
        assert_revision_in_origin_main(inp.repo_root, expected)

        require_compose_v2()
        require_docker_daemon()

        deploy_root = Path(inp.deploy_root or env["FETCHNOW_DEPLOY_ROOT"])
        backup_root = Path(inp.backup_root or env["FETCHNOW_BACKUP_ROOT"])
        if identity is not None:
            deploy_root = identity.deploy_root
            backup_root = identity.backup_root
        validate_operator_root(
            deploy_root, repo_root=inp.repo_root, label="deploy root"
        )
        validate_operator_root(
            backup_root, repo_root=inp.repo_root, label="backup root"
        )

        # Free-space margin on deploy/backup filesystems (may not exist yet —
        # check nearest existing parent).
        for label, root in (("deploy root", deploy_root), ("backup root", backup_root)):
            probe = root
            while not probe.exists() and probe != probe.parent:
                probe = probe.parent
            usage = shutil.disk_usage(probe)
            if usage.free < MIN_FREE_BYTES:
                raise PreflightError(
                    f"insufficient free space under {label}: "
                    f"have {usage.free} bytes, need >= {MIN_FREE_BYTES}"
                )

        cfg = compose_config_json(
            project_name=inp.project_name,
            env_file=inp.env_file,
            compose_files=inp.compose_files,
            repo_root=inp.repo_root,
        )
        validate_staging_rendered(
            cfg, expected_revision=expected, expected_project=inp.project_name
        )
        messages.append("OK: deployment preflight passed")
        messages.append(f"revision={expected}")
        return PreflightResult(ok=True, messages=tuple(messages))
    except (
        PreflightError,
        EnvFileError,
        RevisionError,
        PasswordError,
        GitCheckError,
        DockerCheckError,
        RootPathError,
        EnvironmentError,
        ComposeContractError,
        OSError,
        ValueError,
    ) as exc:
        return PreflightResult(ok=False, messages=(f"FAIL: {redact(str(exc))}",))
