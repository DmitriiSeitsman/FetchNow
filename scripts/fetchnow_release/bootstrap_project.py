"""Validated Compose project names for initial DB bootstrap tests."""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path

from .c3b3_constants import BOOTSTRAP_TEST_PROJECT_PREFIX
from .environment import real_identity

PROJECT_RE = re.compile(r"^fetchnow-bootstrap-test-[a-z0-9]{8,32}$")
FORBIDDEN = frozenset(
    {
        "",
        "fetchnow",
        "fetchnow-staging",
        "fetchnow-production",
        "fetchnow-prod",
        "fetchnow-bootstrap-test",
    }
)


class BootstrapProjectError(ValueError):
    """Unsafe bootstrap project name."""


def assert_bootstrap_project(
    name: str, *, allow_real: bool = False
) -> str:
    if allow_real and real_identity(name) is not None:
        return name
    if name in FORBIDDEN:
        raise BootstrapProjectError(f"refusing forbidden project name: {name!r}")
    if not PROJECT_RE.fullmatch(name):
        raise BootstrapProjectError(
            f"refusing unsafe project name {name!r}; "
            f"expected {BOOTSTRAP_TEST_PROJECT_PREFIX}<unique-suffix>"
        )
    return name


def make_bootstrap_project_name() -> str:
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
    return assert_bootstrap_project(f"{BOOTSTRAP_TEST_PROJECT_PREFIX}{suffix}")


def load_bootstrap_project_file(path: Path) -> str:
    if not path.is_file():
        raise BootstrapProjectError(f"project file missing: {path}")
    name = "".join(path.read_text(encoding="utf-8").split())
    if not name:
        raise BootstrapProjectError("project file is empty")
    return assert_bootstrap_project(name)
