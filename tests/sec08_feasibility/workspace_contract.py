"""DAC rules for the SEC-08 proof workspace.

uid 0 is not treated as a bypass. Without CAP_DAC_OVERRIDE the kernel uses
the caller uid, egid, and supplementary groups like any other subject.
"""

from __future__ import annotations

WORK_DIR_MODE = 0o2750
TOOL_FILE_MODE = 0o640
_ACCESS_BITS = {"r": 0o400, "w": 0o200, "x": 0o100}


def dac_allows(
    *,
    euid: int,
    egid: int,
    groups: list[int],
    owner: int,
    group: int,
    mode: int,
    access: str,
) -> bool:
    bit = _ACCESS_BITS[access]
    if euid == owner:
        return bool(mode & bit)
    if egid == group or group in groups:
        return bool(mode & (bit >> 3))
    return bool(mode & (bit >> 6))


def mkdir_visible_mode(requested: int, umask: int = 0) -> int:
    """Mode bits mkdir(2) actually stores. Setuid/setgid/sticky are masked off."""
    return requested & ~umask & 0o777
