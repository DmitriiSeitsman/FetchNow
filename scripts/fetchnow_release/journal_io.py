"""Atomic JSON/YAML publication helpers for PRD1C3A journals."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class JournalIOError(RuntimeError):
    """Unsafe or failed journal I/O."""


def _reject_symlink(path: Path) -> None:
    if path.exists() and path.is_symlink():
        raise JournalIOError(f"refusing symlink path: {path}")


def atomic_write_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    """Write text via temp + fsync + replace; leaf must not be a symlink."""
    path = path if path.is_absolute() else path.resolve()
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    os.chmod(parent, 0o700)
    _reject_symlink(parent)
    if path.exists():
        _reject_symlink(path)
    tmp = path.with_name(path.name + ".tmp")
    if tmp.exists() or tmp.is_symlink():
        if tmp.is_symlink():
            raise JournalIOError(f"refusing symlink temp path: {tmp}")
        tmp.unlink()
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    fd = os.open(str(parent), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_json(path: Path, payload: dict[str, Any], *, mode: int = 0o600) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    atomic_write_text(path, text, mode=mode)


def read_json(path: Path) -> dict[str, Any]:
    _reject_symlink(path)
    if not path.is_file():
        raise JournalIOError(f"missing JSON file: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise JournalIOError(f"invalid JSON at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise JournalIOError(f"JSON root must be object: {path}")
    return raw
