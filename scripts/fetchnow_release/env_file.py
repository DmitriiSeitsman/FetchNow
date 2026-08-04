"""Parse and validate operator env files without printing secrets."""

from __future__ import annotations

import re
import stat
from pathlib import Path

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class EnvFileError(ValueError):
    """Invalid env file."""


def load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        raise EnvFileError(f"env file not found: {path}")
    if path.is_symlink():
        raise EnvFileError(f"env file must not be a symlink: {path}")
    if not path.is_file():
        raise EnvFileError(f"env file must be a regular file: {path}")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise EnvFileError(
            f"env file mode {mode:04o} is broader than 0600 (group/other bits set)"
        )
    values: dict[str, str] = {}
    for lineno, raw in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise EnvFileError(f"invalid env line {lineno}: missing '='")
        key, value = line.split("=", 1)
        key = key.strip()
        if not _KEY_RE.fullmatch(key):
            raise EnvFileError(f"invalid env key on line {lineno}: {key!r}")
        if key in values:
            raise EnvFileError(f"duplicate env key: {key}")
        # Strip optional surrounding quotes without interpreting escapes.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def require_keys(values: dict[str, str], keys: tuple[str, ...]) -> None:
    missing = [k for k in keys if k not in values or values[k] == ""]
    if missing:
        raise EnvFileError("missing required env keys: " + ", ".join(missing))
