"""Validated Compose project names for PRD1C3B2B2 migration integration."""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path

from .environment import real_identity
from .c3b2b2_constants import MIGRATION_TEST_PROJECT_PREFIX

PROJECT_RE = re.compile(r"^fetchnow-migration-test-[a-z0-9]{8,32}$")
FORBIDDEN = frozenset(
    {
        "",
        "fetchnow",
        "fetchnow-staging",
        "fetchnow-production",
        "fetchnow-prod",
        "fetchnow-migration-test",
    }
)


class MigrationProjectError(ValueError):
    """Unsafe migration project name."""


def assert_migration_project(
    name: str, *, allow_real: bool = False, allow_staging: bool = False
) -> str:
    if (allow_real or allow_staging) and real_identity(name) is not None:
        return name
    if name in FORBIDDEN:
        raise MigrationProjectError(f"refusing forbidden project name: {name!r}")
    if not PROJECT_RE.fullmatch(name):
        raise MigrationProjectError(
            f"refusing unsafe project name {name!r}; "
            f"expected {MIGRATION_TEST_PROJECT_PREFIX}<unique-suffix>"
        )
    return name


def make_migration_project_name() -> str:
    rand = secrets.token_hex(4)
    run = re.sub(r"[^0-9]", "", os.environ.get("GITHUB_RUN_ID", ""))
    attempt = re.sub(r"[^0-9]", "", os.environ.get("GITHUB_RUN_ATTEMPT", "1")) or "1"
    if run:
        suffix = f"{run[-10:]}{attempt}{rand}"
        if len(suffix) > 32:
            suffix = suffix[-32:]
        if len(suffix) < 8:
            suffix = (suffix + secrets.token_hex(4))[:16]
    else:
        suffix = secrets.token_hex(6)
    return assert_migration_project(f"{MIGRATION_TEST_PROJECT_PREFIX}{suffix}")


def load_migration_project_file(path: Path) -> str:
    if not path.is_file():
        raise MigrationProjectError(f"project file missing: {path}")
    name = "".join(path.read_text(encoding="utf-8").split())
    if not name:
        raise MigrationProjectError("project file is empty")
    return assert_migration_project(name)
