"""Deployment / backup root path policy (reuses PRD1B safety rules)."""

from __future__ import annotations

import sys
from pathlib import Path

# Reuse proven path safety without importing backup create/verify.
_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from fetchnow_pg_backup.fs_safety import (  # noqa: E402
    PathSafetyError,
    validate_backup_root,
)


class RootPathError(PathSafetyError):
    """Unsafe deployment or backup root."""


def validate_operator_root(path: Path, *, repo_root: Path, label: str) -> Path:
    try:
        return validate_backup_root(path, repo_root=repo_root)
    except PathSafetyError as exc:
        raise RootPathError(f"{label}: {exc}") from exc
