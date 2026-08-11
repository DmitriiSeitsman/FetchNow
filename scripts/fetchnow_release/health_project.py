"""Validated Compose project names for isolated tag-based health checks."""

from __future__ import annotations

import re

from . import STAGING_PROJECT

HEALTH_TEST_PROJECT_PREFIX = "fetchnow-health-test-"
PROJECT_RE = re.compile(rf"^{re.escape(HEALTH_TEST_PROJECT_PREFIX)}[a-z0-9]{{8,32}}$")
FORBIDDEN = frozenset(
    {
        "",
        "fetchnow",
        STAGING_PROJECT,
        "fetchnow-prod",
        "fetchnow-health-test",
        HEALTH_TEST_PROJECT_PREFIX.rstrip("-"),
    }
)


class HealthProjectError(ValueError):
    """Unsafe health project name for tag-based mode."""


def assert_isolated_tag_health_project(name: str) -> str:
    """Allow tag-mode health only for validated fetchnow-health-test-* projects."""
    if name in FORBIDDEN:
        raise HealthProjectError(
            f"refusing tag-mode health for protected project {name!r}; "
            "pass --deploy-root for managed current-state/image-ID verification"
        )
    if not PROJECT_RE.fullmatch(name):
        raise HealthProjectError(
            f"refusing tag-mode health for project {name!r}; "
            f"expected {HEALTH_TEST_PROJECT_PREFIX}<unique-suffix> "
            "or managed mode with --deploy-root"
        )
    if ".." in name or "/" in name or "\\" in name:
        raise HealthProjectError(f"refusing unsafe project name: {name!r}")
    return name
