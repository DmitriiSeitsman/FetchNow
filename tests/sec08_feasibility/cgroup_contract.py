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


def _decode_mountinfo_token(token: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), token)


def cgroup2_mount_points(mountinfo: str) -> list[str]:
    """Mount points whose filesystem type is cgroup2. Order follows mountinfo."""
    found: list[str] = []
    for line in mountinfo.splitlines():
        if " - " not in line:
            continue
        left, right = line.split(" - ", 1)
        right_fields = right.split()
        if not right_fields or right_fields[0] != "cgroup2":
            continue
        left_fields = left.split()
        if len(left_fields) < 5:
            continue
        found.append(_decode_mountinfo_token(left_fields[4]))
    return found


def _contained_absolute(path: str) -> str:
    """Reject empty, relative, root, and any '.' or '..' component.

    The returned string still starts with '/'. An error returns ''.
    """
    if not path or path != path.strip() or "\x00" in path or "\n" in path or "\r" in path:
        return ""
    if not path.startswith("/"):
        return ""
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts[1:]):
        return ""
    if path == "/":
        return ""
    return path


def resolve_control_group_dir(control_group: str, cgroup2_mount: str) -> tuple[str, str]:
    """Host directory for a systemd ControlGroup value.

    The unit name is not an input. A missing or root ControlGroup is an error.
    """
    raw_group = control_group.strip()
    mount = cgroup2_mount.strip()
    if mount != "/":
        mount = mount.rstrip("/")
    if not raw_group:
        return "", "control group is empty"
    if raw_group in {"/", "/.", "."}:
        return "", "control group is the cgroup root"
    contained = _contained_absolute(raw_group.rstrip("/"))
    if not contained:
        return "", "control group is not a contained path"
    mount_contained = _contained_absolute(mount) if mount != "/" else "/"
    if mount != "/" and not mount_contained:
        return "", "cgroup2 mount is invalid"
    if mount == "/":
        host_dir = contained
        prefix = "/"
    else:
        host_dir = mount_contained + contained
        prefix = mount_contained + "/"
    if host_dir == mount_contained or not host_dir.startswith(prefix):
        return "", "control group is outside the cgroup2 mount"
    return host_dir, ""


def observe_slice_memory_max(
    *,
    active_state: str,
    control_group: str,
    cgroup2_mounts: list[str],
    memory_max_text: str | None,
) -> tuple[dict, str]:
    """Accept the slice only from ControlGroup plus the memory.max file.

    ``memory_max_text`` is None when that file cannot be read. A systemd
    MemoryMax property is not an argument and cannot satisfy this check.
    """
    if active_state.strip() != "active":
        return {}, "slice is not active"
    if len(cgroup2_mounts) != 1:
        return {}, "cgroup2 mount is not confirmed"
    host_dir, error = resolve_control_group_dir(control_group, cgroup2_mounts[0])
    if error:
        return {}, error
    if memory_max_text is None:
        return {}, "memory.max is missing"
    if not memory_max_is_64m(memory_max_text):
        return {}, "memory.max is not 64M"
    return {
        "control_group": control_group.strip(),
        "cgroup2_mount": cgroup2_mounts[0],
        "host_dir": host_dir,
        "memory_max_path": host_dir + "/memory.max",
        "memory_max": memory_max_text.strip(),
    }, ""


def control_groups_match(before: str, after: str) -> bool:
    first = before.strip()
    return bool(first) and first == after.strip()


def cgroup_relative_path(proc_cgroup: str) -> str:
    for line in proc_cgroup.splitlines():
        if line.startswith("0::"):
            return line.split("::", 1)[1].strip()
    return ""


def proc_cgroup_under_control_group(proc_cgroup: str, control_group: str) -> bool:
    prefix = control_group.strip().rstrip("/")
    if not prefix.startswith("/") or prefix == "":
        return False
    for line in proc_cgroup.splitlines():
        if "::" not in line:
            continue
        relative = line.split("::", 1)[1].strip()
        if relative == prefix or relative.startswith(prefix + "/"):
            return True
    return False


def flattened_unit_cgroup_dir(cgroup2_mount: str, unit_name: str) -> str:
    """The incorrect path this harness used to build from the unit name."""
    return cgroup2_mount.rstrip("/") + "/" + unit_name


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
