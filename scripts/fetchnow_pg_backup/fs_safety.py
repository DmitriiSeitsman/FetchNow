"""Filesystem safety, permissions, and atomic publication helpers."""

from __future__ import annotations

import os
import stat
from pathlib import Path


class PathSafetyError(ValueError):
    """Raised when a backup path is unsafe."""


def _abspath(path: Path) -> Path:
    return Path(os.path.abspath(str(path.expanduser())))


def resolve_backup_path(path: Path) -> Path:
    """Resolve a path for safety checks.

    Intermediate OS symlinks (e.g. macOS /var -> /private/var) are allowed.
    The leaf path itself must not be a symlink.
    """
    absolute = _abspath(path)
    if absolute.exists() and absolute.is_symlink():
        raise PathSafetyError(f"path must not be a symlink: {absolute}")
    if absolute.exists():
        return absolute.resolve()
    # Non-existent: resolve existing parent prefix, keep final name.
    parent = absolute.parent
    if parent.exists():
        return parent.resolve() / absolute.name
    return absolute


def validate_backup_root(
    backup_root: Path,
    *,
    repo_root: Path,
    home: Path | None = None,
) -> Path:
    """Validate and return a resolved backup root path."""
    home_path = resolve_backup_path(home or Path.home())
    repo = resolve_backup_path(repo_root)
    resolved = resolve_backup_path(backup_root)

    if resolved == Path("/"):
        raise PathSafetyError("backup root must not be filesystem root")
    if resolved == home_path:
        raise PathSafetyError("backup root must not be the user home directory")
    if resolved == repo:
        raise PathSafetyError("backup root must not be the repository root")
    try:
        resolved.relative_to(repo)
    except ValueError:
        pass
    else:
        raise PathSafetyError("backup root must not be inside the Git worktree")
    return resolved


def ensure_backup_root(backup_root: Path) -> Path:
    backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(backup_root, 0o700)
    if backup_root.is_symlink():
        raise PathSafetyError(f"backup root must not be a symlink: {backup_root}")
    return resolve_backup_path(backup_root)


def chmod_file_0600(path: Path) -> None:
    os.chmod(path, 0o600)


def chmod_dir_0700(path: Path) -> None:
    os.chmod(path, 0o700)


def fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_publish_directory(*, incomplete_dir: Path, final_dir: Path) -> None:
    if final_dir.exists():
        raise PathSafetyError(f"refusing to overwrite published backup: {final_dir}")
    os.rename(incomplete_dir, final_dir)
    fsync_dir(final_dir.parent)


def is_safe_relative_filename(name: str) -> bool:
    if not name or name in {".", ".."}:
        return False
    if "/" in name or "\\" in name or "\x00" in name:
        return False
    if name.startswith("."):
        return False
    return True


def mode_bits(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def remove_directory_tree(path: Path) -> None:
    """Remove a directory tree without following symlinks (no shell rm -rf)."""
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink():
        raise PathSafetyError(f"refusing to remove symlink path: {path}")
    if not path.is_dir():
        path.unlink()
        return
    for child in path.iterdir():
        if child.is_symlink() or child.is_file():
            child.unlink()
        elif child.is_dir():
            remove_directory_tree(child)
        else:
            child.unlink(missing_ok=True)
    path.rmdir()


def assert_direct_child(child: Path, parent: Path) -> Path:
    child_abs = resolve_backup_path(child) if child.exists() else _abspath(child)
    parent_abs = resolve_backup_path(parent)
    if _abspath(child).parent != _abspath(parent) and child_abs.parent != parent_abs:
        # Prefer lexical direct-child check on abspaths to avoid symlink escapes.
        if _abspath(child).parent != _abspath(parent):
            raise PathSafetyError(f"{child} is not a direct child of {parent}")
    if child.is_symlink():
        raise PathSafetyError(f"refusing symlink child: {child}")
    return child_abs if child.exists() else _abspath(child)


# Back-compat alias used by verify.py
def reject_symlink_components(path: Path) -> Path:
    return resolve_backup_path(path)
