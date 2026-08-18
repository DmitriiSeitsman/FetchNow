"""Validated Compose project names for deploy-plan integration tests."""

from __future__ import annotations

import os
import re
import secrets

from .environment import real_identity
from .rollout_project import FORBIDDEN as _ROLLOUT_FORBIDDEN

DEPLOY_PLAN_TEST_PROJECT_RE = re.compile(
    r"^fetchnow-deploy-plan-test-[a-z0-9]{8,32}$"
)
FORBIDDEN = _ROLLOUT_FORBIDDEN | frozenset({"fetchnow-deploy-plan-test"})


class DeployPlanProjectError(ValueError):
    """Unsafe or invalid deploy-plan test project name."""


def assert_deploy_plan_project(
    name: str, *, allow_real: bool = False, allow_staging: bool = False
) -> None:
    if (allow_real or allow_staging) and real_identity(name) is not None:
        return
    if name in FORBIDDEN or not DEPLOY_PLAN_TEST_PROJECT_RE.fullmatch(name):
        raise DeployPlanProjectError(
            "project name must match "
            f"{DEPLOY_PLAN_TEST_PROJECT_RE.pattern!r}, got {name!r}"
        )


def make_deploy_plan_project_name() -> str:
    run = re.sub(r"[^0-9]", "", os.environ.get("GITHUB_RUN_ID", ""))
    rand = secrets.token_hex(4)
    suffix = f"{run[-8:]}{rand}" if run else secrets.token_hex(6)
    if len(suffix) > 32:
        suffix = suffix[-32:]
    name = f"fetchnow-deploy-plan-test-{suffix}"
    assert_deploy_plan_project(name)
    return name


def load_deploy_plan_project_file(path: os.PathLike[str] | str) -> str:
    text = open(path, encoding="utf-8").read().strip()
    assert_deploy_plan_project(text)
    return text
