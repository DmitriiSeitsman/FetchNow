"""Read-only staging deployment preflight."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from . import (
    MIN_FREE_BYTES,
    STAGING_PROJECT,
)
from .compose_validate import ComposeContractError, validate_staging_rendered
from .docker_checks import (
    DockerCheckError,
    compose_config_json,
    require_compose_v2,
    require_docker_daemon,
)
from .env_file import EnvFileError, load_env_file, require_keys
from .git_checks import (
    GitCheckError,
    assert_clean_worktree,
    assert_revision_in_origin_main,
)
from .password import PasswordError, validate_staging_password
from .redact import redact
from .revision import RevisionError, validate_full_sha
from .roots import RootPathError, validate_operator_root
from .rollout_project import RolloutProjectError, assert_rollout_project


def _assert_allowed_project(name: str) -> None:
    """Allow production staging or validated rollout integration projects."""
    if name == STAGING_PROJECT:
        return
    try:
        assert_rollout_project(name)
    except RolloutProjectError as exc:
        raise PreflightError(
            f"project name must be {STAGING_PROJECT!r} or a validated "
            f"fetchnow-rollout-test-* name, got {name!r}"
        ) from exc


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

        gw = env.get("GATEWAY_PORT", "")
        if not gw.startswith("127.0.0.1:"):
            raise PreflightError("GATEWAY_PORT must be loopback (127.0.0.1:<port>)")

        assert_clean_worktree(inp.repo_root)
        assert_revision_in_origin_main(inp.repo_root, expected)

        require_compose_v2()
        require_docker_daemon()

        deploy_root = Path(inp.deploy_root or env["FETCHNOW_DEPLOY_ROOT"])
        backup_root = Path(inp.backup_root or env["FETCHNOW_BACKUP_ROOT"])
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
        messages.append("OK: staging deployment preflight passed")
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
        ComposeContractError,
        OSError,
        ValueError,
    ) as exc:
        return PreflightResult(ok=False, messages=(f"FAIL: {redact(str(exc))}",))
