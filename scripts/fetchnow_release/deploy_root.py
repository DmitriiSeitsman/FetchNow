"""Deployment-root layout validation for PRD1C2 (releases/ + locks/)."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

from . import MIN_FREE_BYTES
from .c2_constants import LOCKS_DIRNAME, RELEASES_DIRNAME
from .roots import RootPathError, validate_operator_root


class DeployRootError(RootPathError):
    """Unsafe or unusable deployment root."""


def validate_deploy_root(path: Path, *, repo_root: Path) -> Path:
    """Validate absolute deploy root and ensure releases/locks can be used."""
    try:
        resolved = validate_operator_root(
            path, repo_root=repo_root, label="deploy root"
        )
    except RootPathError as exc:
        raise DeployRootError(str(exc)) from exc
    if resolved.exists() and not resolved.is_dir():
        raise DeployRootError(f"deploy root must be a directory: {resolved}")
    if resolved.exists() and resolved.is_symlink():
        raise DeployRootError(f"deploy root must not be a symlink: {resolved}")
    if resolved.exists():
        mode = stat.S_IMODE(resolved.stat().st_mode)
        if mode & 0o002:
            raise DeployRootError(f"deploy root is world-writable: {resolved}")
        if not os.access(resolved, os.W_OK | os.X_OK):
            raise DeployRootError(
                f"deploy root is not writable by current user: {resolved}"
            )
    # Free-space margin on nearest existing parent.
    probe = resolved
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    if usage.free < MIN_FREE_BYTES:
        raise DeployRootError(
            f"insufficient free space under deploy root: "
            f"have {usage.free} bytes, need >= {MIN_FREE_BYTES}"
        )
    return resolved


def ensure_deploy_layout(deploy_root: Path) -> tuple[Path, Path]:
    """Create releases/ and locks/ with restrictive permissions."""
    deploy_root.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chmod(deploy_root, 0o750)
    if deploy_root.is_symlink():
        raise DeployRootError(f"deploy root must not be a symlink: {deploy_root}")
    releases = deploy_root / RELEASES_DIRNAME
    locks = deploy_root / LOCKS_DIRNAME
    releases.mkdir(mode=0o750, exist_ok=True)
    locks.mkdir(mode=0o700, exist_ok=True)
    os.chmod(releases, 0o750)
    os.chmod(locks, 0o700)
    if releases.is_symlink() or locks.is_symlink():
        raise DeployRootError("releases/ or locks/ must not be symlinks")
    return releases, locks


def release_dir(deploy_root: Path, revision: str) -> Path:
    releases = deploy_root / RELEASES_DIRNAME
    child = releases / revision
    # Direct child of releases/ only.
    if child.parent.resolve() != releases.resolve():
        raise DeployRootError("release path must be a direct child of releases/")
    if child.name != revision:
        raise DeployRootError("release directory name must equal full revision SHA")
    return child
