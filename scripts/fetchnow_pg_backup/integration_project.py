"""Safe Compose project naming for PRD1B backup integration / CI cleanup."""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path

PROJECT_PREFIX = "fetchnow-backup-test-"
PROJECT_RE = re.compile(r"^fetchnow-backup-test-[a-z0-9]{8,32}$")
FORBIDDEN_PROJECTS = frozenset(
    {
        "",
        "fetchnow",
        "fetchnow-staging",
        "fetchnow-production",
        "fetchnow-prod",
        "fetchnow-backup-test",
    }
)


class ProjectNameError(ValueError):
    """Invalid or forbidden Compose project name."""


def assert_safe_project(name: str) -> str:
    """Refuse any project name that is not a unique test-only identity."""
    if name in FORBIDDEN_PROJECTS:
        raise ProjectNameError(f"refusing forbidden project name: {name!r}")
    if not PROJECT_RE.fullmatch(name):
        raise ProjectNameError(
            f"refusing unsafe project name {name!r}; "
            f"expected {PROJECT_PREFIX}<unique-suffix>"
        )
    return name


def make_project_name() -> str:
    """Return a unique per-run test project name.

    Incorporates GitHub run identity when present so concurrent CI attempts
    cannot collide, while remaining valid under PROJECT_RE.
    """
    rand = secrets.token_hex(4)  # 8 hex chars
    run = re.sub(r"[^0-9]", "", os.environ.get("GITHUB_RUN_ID", ""))
    attempt = re.sub(r"[^0-9]", "", os.environ.get("GITHUB_RUN_ATTEMPT", "1")) or "1"
    if run:
        # Keep suffix length within [8, 32]: run tail + attempt + rand.
        suffix = f"{run[-10:]}{attempt}{rand}"
        if len(suffix) > 32:
            suffix = suffix[-32:]
        if len(suffix) < 8:
            suffix = (suffix + secrets.token_hex(4))[:16]
    else:
        suffix = secrets.token_hex(6)
    return assert_safe_project(f"{PROJECT_PREFIX}{suffix}")


def load_project_file(path: Path) -> str:
    """Read and validate a project-file written by the integration runner."""
    if not path.is_file():
        raise ProjectNameError(f"project file missing: {path}")
    raw = path.read_text(encoding="utf-8")
    name = "".join(raw.split())
    if not name:
        raise ProjectNameError("project file is empty")
    return assert_safe_project(name)
