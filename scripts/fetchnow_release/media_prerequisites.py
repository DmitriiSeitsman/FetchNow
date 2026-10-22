"""Host prerequisites for SEC-08/09 media activation (check only).

Does not install slices, change neighbor units, or grant privileged
capabilities. Fail closed when Landlock ABI, slice ControlGroup, or Docker
cgroup support is unproven.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .bounded_subprocess import (
    BoundedCancelledError,
    BoundedResult,
    BoundedTimeoutError,
    OutputLimitExceededError,
    run_docker_probe,
)
from .media_storage import (
    MediaStorageContract,
    MediaStorageError,
    assert_media_storage_ready,
    parse_media_storage_contract,
)
from .redact import redact
from .resource_budget import ResourceBudget, ResourceBudgetError, parse_resource_budget

MIN_LANDLOCK_ABI = 6
DEFAULT_SLICE_UNIT = "fetchnow-media-executor.slice"
_CGROUP_RE = re.compile(r"^/[A-Za-z0-9_./-]+$")
CGROUP_ROOT = Path("/sys/fs/cgroup")


class MediaPrerequisiteError(RuntimeError):
    """Missing or unproven media activation prerequisite."""


@dataclass(frozen=True)
class MediaPrerequisiteReport:
    docker_rootless: bool
    cgroup_driver: str
    cgroup_version: str
    landlock_abi: int
    slice_unit: str
    control_group: str
    budget: ResourceBudget
    messages: tuple[str, ...]
    storage: MediaStorageContract | None = None


def _probe(argv: list[str]) -> BoundedResult:
    try:
        return run_docker_probe(argv, cwd=Path("/"))
    except (
        BoundedTimeoutError,
        BoundedCancelledError,
        OutputLimitExceededError,
    ) as exc:
        raise MediaPrerequisiteError(
            "docker prerequisite probe failed: " + redact(str(exc))
        ) from exc


def assert_rootful_docker_writable_cgroups() -> tuple[bool, str, str]:
    """Require rootful Docker with cgroup v2 + systemd driver."""
    result = _probe(["docker", "info", "--format", "{{json .}}"])
    if getattr(result, "returncode", 1) != 0:
        raise MediaPrerequisiteError(
            "docker info failed: " + redact(getattr(result, "stderr_text", "") or "")
        )
    try:
        info = json.loads(result.stdout_text)
    except json.JSONDecodeError as exc:
        raise MediaPrerequisiteError("docker info JSON invalid") from exc
    if not isinstance(info, dict):
        raise MediaPrerequisiteError("docker info must be an object")
    security = info.get("SecurityOptions") or []
    rootless = any(
        isinstance(item, str) and "rootless" in item.lower() for item in security
    )
    if rootless:
        raise MediaPrerequisiteError(
            "media activation requires rootful Docker with writable private cgroups"
        )
    cgroup_driver = str(info.get("CgroupDriver") or "")
    cgroup_version = str(info.get("CgroupVersion") or "")
    engine = str(info.get("ServerVersion") or "")
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", engine)
    if match is None or tuple(map(int, match.groups())) < (28, 0, 0):
        raise MediaPrerequisiteError(
            "Docker Engine >=28 required for writable private cgroups"
        )
    if cgroup_version not in {"2", "v2"}:
        raise MediaPrerequisiteError(
            f"media activation requires cgroup v2, got {cgroup_version!r}"
        )
    if cgroup_driver != "systemd":
        raise MediaPrerequisiteError(
            f"media activation requires cgroup driver systemd, got {cgroup_driver!r}"
        )
    return rootless, cgroup_driver, cgroup_version


def probe_landlock_abi(*, probe_command: list[str] | None = None) -> int:
    """Return Landlock ABI or raise if unproven.

    Default probe uses a tiny Python ctypes syscall wrapper when available.
    Callers may inject a disposable probe_command for native gates.
    """
    if probe_command is not None:
        proc = _probe(probe_command)
        if proc.returncode != 0:
            raise MediaPrerequisiteError(
                "Landlock ABI probe failed: "
                + redact(
                    proc.stderr_text.strip()
                    or proc.stdout_text.strip()
                    or "nonzero exit"
                )
            )
        text = proc.stdout_text.strip()
        try:
            abi = int(text)
        except ValueError as exc:
            raise MediaPrerequisiteError(
                f"Landlock ABI probe returned non-integer: {text!r}"
            ) from exc
        if abi < MIN_LANDLOCK_ABI:
            raise MediaPrerequisiteError(
                f"Landlock ABI {abi} < required {MIN_LANDLOCK_ABI}"
            )
        return abi

    # Inline probe: landlock_create_ruleset(NULL,0,VERSION) via ctypes.
    if platform.system() != "Linux":
        raise MediaPrerequisiteError(
            "Landlock ABI must be proven on the Linux deployment host"
        )
    script = (
        "import ctypes, ctypes.util, sys\n"
        "libc=ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)\n"
        "NR=getattr(ctypes, 'c_long')\n"
        "# __NR_landlock_create_ruleset: 444 on x86_64 and aarch64\n"
        "import platform\n"
        "mach=platform.machine()\n"
        "nr=444 if mach in ('x86_64','amd64','aarch64','arm64') else None\n"
        "if nr is None:\n"
        "  sys.stderr.write('unsupported machine for landlock probe\\n'); sys.exit(2)\n"
        "abi=libc.syscall(nr, None, 0, 1)\n"
        "if abi < 0:\n"
        "  sys.stderr.write('landlock syscall failed\\n'); sys.exit(3)\n"
        "print(abi)\n"
    )
    proc = _probe(["python3", "-c", script])
    if proc.returncode != 0:
        raise MediaPrerequisiteError(
            "Landlock ABI unproven: "
            + redact(
                proc.stderr_text.strip() or proc.stdout_text.strip() or "probe failed"
            )
        )
    try:
        abi = int(proc.stdout_text.strip())
    except ValueError as exc:
        raise MediaPrerequisiteError("Landlock ABI probe non-integer") from exc
    if abi < MIN_LANDLOCK_ABI:
        raise MediaPrerequisiteError(
            f"Landlock ABI {abi} < required {MIN_LANDLOCK_ABI}"
        )
    return abi


def assert_slice_control_group(
    slice_unit: str = DEFAULT_SLICE_UNIT,
    *,
    budget: ResourceBudget | None = None,
) -> str:
    """Resolve ControlGroup via systemctl; refuse path guessing."""
    if shutil.which("systemctl") is None:
        raise MediaPrerequisiteError("systemctl not available for slice verification")
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.slice", slice_unit):
        raise MediaPrerequisiteError("invalid media slice unit")
    show = _probe(
        [
            "systemctl",
            "show",
            slice_unit,
            "-p",
            "LoadState",
            "-p",
            "FragmentPath",
            "-p",
            "ControlGroup",
            "-p",
            "MemoryMax",
        ],
    )
    if show.returncode != 0:
        raise MediaPrerequisiteError(
            f"systemctl show {slice_unit} failed: "
            + redact(show.stderr_text.strip() or show.stdout_text.strip())
        )
    values = dict(
        line.split("=", 1) for line in str(show.stdout_text).splitlines() if "=" in line
    )
    load_state = values.get("LoadState", "")
    fragment = values.get("FragmentPath", "")
    control_group = values.get("ControlGroup", "")
    if load_state != "loaded" or not fragment:
        raise MediaPrerequisiteError(
            f"slice {slice_unit} is not installed (LoadState={load_state!r}, "
            f"FragmentPath empty/missing)"
        )
    if not fragment or not Path(fragment).is_file():
        raise MediaPrerequisiteError(
            f"slice {slice_unit} FragmentPath missing or not a file: {fragment!r}"
        )
    if not control_group or control_group == "/":
        raise MediaPrerequisiteError(
            f"slice {slice_unit} ControlGroup unproven ({control_group!r})"
        )
    if not _CGROUP_RE.fullmatch(control_group) or ".." in control_group.split("/"):
        raise MediaPrerequisiteError(
            f"slice {slice_unit} ControlGroup malformed: {control_group!r}"
        )
    if budget is not None:
        assert_slice_budget(control_group, budget)
    return control_group


def assert_slice_budget(control_group: str, budget: ResourceBudget) -> None:
    """Read the actual limiting ancestor, not just a declared unit setting."""
    path = CGROUP_ROOT / control_group.lstrip("/")
    try:
        memory = int((path / "memory.max").read_text().strip())
        tasks = int((path / "pids.max").read_text().strip())
        swap = int((path / "memory.swap.max").read_text().strip())
        quota, period = (path / "cpu.max").read_text().split()
        cpu = int(quota) * 1000 // int(period)
    except (OSError, ValueError, ZeroDivisionError) as exc:
        raise MediaPrerequisiteError(
            "finite memory/cpu/tasks ancestor limits unproven"
        ) from exc
    if (memory, cpu, tasks, swap) != (
        budget.aggregate_memory_max_bytes,
        budget.aggregate_cpu_quota_milli,
        budget.aggregate_tasks_max,
        budget.memory_swap_max_bytes,
    ):
        raise MediaPrerequisiteError(
            "actual slice limits do not match the approved budget"
        )


def assert_executor_scopes_under_ancestor(
    *,
    ancestor_control_group: str,
    scope_control_groups: tuple[str, ...],
) -> None:
    """Require each executor scope path to be under the slice ControlGroup."""
    if not ancestor_control_group.startswith("/"):
        raise MediaPrerequisiteError("ancestor ControlGroup must be absolute")
    prefix = ancestor_control_group.rstrip("/")
    if not scope_control_groups:
        raise MediaPrerequisiteError("no executor scope ControlGroups provided")
    for scope in scope_control_groups:
        if not scope.startswith(prefix + "/"):
            raise MediaPrerequisiteError(
                f"executor scope {scope!r} is not under limiting ancestor "
                f"{ancestor_control_group!r}"
            )


def check_media_prerequisites(
    *,
    budget_raw: object,
    slice_unit: str = DEFAULT_SLICE_UNIT,
    landlock_probe_command: list[str] | None = None,
    skip_landlock: bool = False,
    scope_control_groups: tuple[str, ...] | None = None,
    storage_contract_raw: object | None = None,
    # Activation/rollout must pass require_storage=True. Unit tests that only
    # exercise slice/Landlock may leave this false.
    require_storage: bool = False,
    storage_findmnt: Callable[..., Any] | None = None,
    storage_statfs: Callable[..., Any] | None = None,
) -> MediaPrerequisiteReport:
    """Fail-closed prerequisite gate for explicit media activation."""
    messages: list[str] = []
    try:
        budget = parse_resource_budget(budget_raw)
    except ResourceBudgetError as exc:
        raise MediaPrerequisiteError(str(exc)) from exc

    storage: MediaStorageContract | None = None
    if require_storage:
        if storage_contract_raw is None:
            raise MediaPrerequisiteError(
                "media_storage_contract required for media activation "
                "(dedicated filesystem boundary NOT_PROVEN)"
            )
        try:
            storage = parse_media_storage_contract(storage_contract_raw)
            kwargs: dict[str, Any] = {}
            if storage_findmnt is not None:
                kwargs["findmnt"] = storage_findmnt
            if storage_statfs is not None:
                kwargs["statfs"] = storage_statfs
            messages.extend(assert_media_storage_ready(storage, **kwargs))
        except MediaStorageError as exc:
            raise MediaPrerequisiteError(str(exc)) from exc

    rootless, driver, version = assert_rootful_docker_writable_cgroups()
    messages.append(
        f"OK: docker rootful cgroup_driver={driver} cgroup_version={version}"
    )
    if skip_landlock:
        raise MediaPrerequisiteError(
            "Landlock ABI check cannot be skipped for media activation"
        )
    abi = probe_landlock_abi(probe_command=landlock_probe_command)
    messages.append(f"OK: landlock_abi={abi}")
    control_group = assert_slice_control_group(slice_unit, budget=budget)
    messages.append(f"OK: slice {slice_unit} ControlGroup={control_group}")
    if scope_control_groups is not None:
        assert_executor_scopes_under_ancestor(
            ancestor_control_group=control_group,
            scope_control_groups=scope_control_groups,
        )
        messages.append("OK: executor scopes under slice ancestor")
    # Refuse privileged / dangerous env markers if present in process env.
    for banned in (
        "FETCHNOW_MEDIA_PRIVILEGED",
        "FETCHNOW_MEDIA_DOCKER_SOCK",
    ):
        if os.environ.get(banned):
            raise MediaPrerequisiteError(f"refusing media activation with {banned} set")
    return MediaPrerequisiteReport(
        docker_rootless=rootless,
        cgroup_driver=driver,
        cgroup_version=version,
        landlock_abi=abi,
        slice_unit=slice_unit,
        control_group=control_group,
        budget=budget,
        messages=tuple(messages),
        storage=storage,
    )


def assert_running_media_budget(
    *,
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
    cwd: Path,
    budget: ResourceBudget,
    slice_unit: str,
) -> None:
    """Verify every running sidecar's limits and actual host cgroup membership."""
    ancestor = assert_slice_control_group(slice_unit, budget=budget)
    for service, memory in budget.per_container_memory_bytes.items():
        argv = ["docker", "compose", "--env-file", str(env_file), "-p", project_name]
        for path in compose_files:
            argv.extend(["-f", str(path)])
        result = _probe([*argv, "ps", "-a", "-q", service])
        ids = result.stdout_text.split()
        if result.returncode != 0 or len(ids) != 1:
            raise MediaPrerequisiteError(f"exactly one running {service} required")
        result = _probe(["docker", "inspect", ids[0]])
        try:
            row = json.loads(result.stdout_text)[0]
            host = row["HostConfig"]
            state = row["State"]
            if (
                result.returncode != 0
                or not state["Running"]
                or host["Memory"] != memory
                or host.get("MemorySwap") != memory
                or host["CgroupParent"] != slice_unit
            ):
                raise ValueError("container limits/parent mismatch")
            pid = state["Pid"]
            if type(pid) is not int or pid <= 0:
                raise ValueError("invalid container pid")
            scopes = [
                line[3:]
                for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines()
                if line.startswith("0::")
            ]
            assert_executor_scopes_under_ancestor(
                ancestor_control_group=ancestor, scope_control_groups=tuple(scopes)
            )
            if (
                service != "egress-proxy"
                and f"MEDIA_EXECUTOR_MAX_JOBS={budget.concurrency}"
                not in row["Config"]["Env"]
            ):
                raise ValueError("concurrency mismatch")
            if service != "egress-proxy":
                capabilities = {
                    str(cap).removeprefix("CAP_") for cap in host.get("CapAdd") or []
                }
                if (
                    host.get("Privileged")
                    or host.get("CgroupnsMode") != "private"
                    or capabilities != {"SETUID", "SETGID", "SETPCAP"}
                    or set(host.get("CapDrop") or []) != {"ALL"}
                ):
                    raise ValueError(
                        "executor private-cgroup/capability contract mismatch"
                    )
        except (ValueError, KeyError, TypeError, IndexError, OSError) as exc:
            raise MediaPrerequisiteError(
                f"{service} actual budget/membership unproven"
            ) from exc
