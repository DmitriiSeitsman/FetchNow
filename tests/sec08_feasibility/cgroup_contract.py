"""Pure checks for the SEC-08 container cgroup proof.

These functions do not talk to Docker or systemd. The runner script records
the observations and applies them here.
"""

from __future__ import annotations

import re

REQUIRED_ENGINE = (28, 0, 0)
CAP_CHOWN_BIT = 1 << 0
CAP_SYS_ADMIN_BIT = 1 << 21
NARROW_CAPS = (1 << 6) | (1 << 7) | (1 << 8)  # SETGID | SETUID | SETPCAP
SLICE_MEMORY_MAX = 64 * 1024 * 1024


def engine_version_tuple(text: str) -> tuple[int, int, int] | None:
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", text.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def engine_at_least(text: str, minimum: tuple[int, int, int] = REQUIRED_ENGINE) -> bool:
    found = engine_version_tuple(text)
    return found is not None and found >= minimum


def security_options_are_rootless(options: list[str]) -> bool:
    for item in options:
        lowered = item.lower()
        if "rootless" in lowered:
            return True
    return False


def narrow_caps_only(cap_eff_hex: str) -> bool:
    value = int(cap_eff_hex, 16)
    if value & CAP_CHOWN_BIT or value & CAP_SYS_ADMIN_BIT:
        return False
    return value == NARROW_CAPS


def memory_max_is_64m(text: str) -> bool:
    cleaned = text.strip()
    return cleaned in {"64M", "67108864"}


def preflight_blockers(facts: dict) -> list[str]:
    """Return reasons to stop before creating a slice. Empty means proceed."""
    blockers: list[str] = []
    if facts.get("machine") != "x86_64" or facts.get("system") != "Linux":
        blockers.append("not native linux x86_64")
    if facts.get("cgroup_v2") is not True:
        blockers.append("cgroup v2 not confirmed")
    if not engine_at_least(str(facts.get("docker_server_version") or "")):
        blockers.append("docker engine is not >= 28.0.0")
    if facts.get("cgroup_driver") != "systemd":
        blockers.append("cgroup driver is not systemd")
    if facts.get("systemd") is not True:
        blockers.append("systemd is not available")
    if facts.get("rootful") is not True or security_options_are_rootless(
        list(facts.get("security_options") or [])
    ):
        blockers.append("docker is not rootful")
    abi = facts.get("landlock_abi")
    if not isinstance(abi, int) or abi < 6:
        blockers.append("landlock abi is below 6")
    return blockers
