"""Host-managed dedicated filesystem contract for media data paths.

Backend: a single host-mounted, fixed-capacity filesystem bind-mounted into
worker/executor media roots. A marker file, free-space number, named Docker
volume on root, ``max_bytes``, or post-reap reject is NOT proof of a hard
boundary.

This module validates an already-provisioned mount. It never runs mkfs,
fallocate, mount, umount, or quota tools.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Product ceiling reused for peak math (must stay aligned with executor constants).
MAX_ARTIFACT_BYTES = 3_221_225_472  # 3 GiB
FS_RESERVE_BYTES = 4 * 1024 * 1024 * 1024  # metadata / journal / inodes

# Peak model (see docs/operations/sec-08-09-media-storage-contract-20261016.md):
# concurrent offline+network executors (each MAX_JOBS=1) + worker tree +
# handoff amplification + two retained published artifacts + FS reserve.
PEAK_WORKING_MULTIPLIER = 9
RETAINED_ARTIFACT_HEADROOM = 2
CALCULATED_CAPACITY_BYTES = (
    PEAK_WORKING_MULTIPLIER + RETAINED_ARTIFACT_HEADROOM
) * MAX_ARTIFACT_BYTES + FS_RESERVE_BYTES

STORAGE_SCHEMA = "fetchnow-media-storage-contract/v1"
ALLOWED_FS_TYPES = frozenset({"ext4", "xfs"})
_ABS_PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]+$")

# Container targets that must resolve onto the dedicated filesystem.
MEDIA_ROOT_TARGETS: dict[str, str] = {
    "tmp": "/var/lib/fetchnow/tmp",
    "executor": "/var/lib/fetchnow/executor",
    "media_net": "/var/lib/fetchnow/media-net",
}

LAYOUT_SUBDIRS: dict[str, str] = {
    "tmp": "tmp",
    "executor": "executor",
    "media_net": "media-net",
}


class MediaStorageError(ValueError):
    """Invalid or unproven media storage contract / mount."""


@dataclass(frozen=True)
class MediaStorageContract:
    approved: bool
    backend: str
    capacity_bytes: int
    host_bind_root: str
    fs_uuid: str
    source_device: str
    fs_type: str
    require_dedicated_device: bool
    reject_root_fs: bool
    layout: dict[str, str]
    notes: str

    def host_path_for(self, key: str) -> Path:
        sub = self.layout.get(key) or LAYOUT_SUBDIRS[key]
        return Path(self.host_bind_root) / sub

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "backend": self.backend,
            "capacity_bytes": self.capacity_bytes,
            "host_bind_root": self.host_bind_root,
            "fs_uuid": self.fs_uuid,
            "source_device": self.source_device,
            "fs_type": self.fs_type,
            "require_dedicated_device": self.require_dedicated_device,
            "reject_root_fs": self.reject_root_fs,
            "layout": dict(self.layout),
            "notes": self.notes,
        }


@dataclass(frozen=True)
class MountIdentity:
    target: str
    source: str
    fstype: str
    fs_uuid: str
    options: str
    size_bytes: int | None


FindmntFn = Callable[[Path], MountIdentity | None]
StatfsFn = Callable[[Path], os.statvfs_result]


def calculated_capacity_bytes() -> int:
    return CALCULATED_CAPACITY_BYTES


def parse_media_storage_contract(raw: object) -> MediaStorageContract:
    if not isinstance(raw, dict):
        raise MediaStorageError("media_storage_contract must be an object")
    schema = raw.get("schema")
    if schema not in {STORAGE_SCHEMA, None}:
        # Allow nested proposal documents that wrap the activation contract.
        body = raw.get("activation_contract")
        if isinstance(body, dict):
            return parse_media_storage_contract(body)
        raise MediaStorageError(f"unsupported media storage schema: {schema!r}")
    try:
        approved = bool(raw["approved"])
        backend = str(raw["backend"])
        capacity = int(raw["capacity_bytes"])
        host_root = str(raw["host_bind_root"])
        fs_uuid = str(raw["fs_uuid"])
        source_device = str(raw["source_device"])
        fs_type = str(raw["fs_type"])
        require_dedicated = bool(raw.get("require_dedicated_device", True))
        reject_root = bool(raw.get("reject_root_fs", True))
        layout_raw = raw.get("layout") or dict(LAYOUT_SUBDIRS)
        notes = str(raw.get("notes") or "")
    except (KeyError, TypeError, ValueError) as exc:
        raise MediaStorageError(f"media_storage_contract invalid: {exc}") from exc
    if type(raw["approved"]) is not bool:
        raise MediaStorageError("approved must be a boolean")
    if not approved:
        raise MediaStorageError(
            "media_storage_contract.approved must be true for media activation "
            "(proposals remain approved=false until owner/ops authorization)"
        )
    if backend != "host_managed_bind_filesystem":
        raise MediaStorageError(
            "only backend=host_managed_bind_filesystem is supported in this package"
        )
    if capacity != CALCULATED_CAPACITY_BYTES:
        raise MediaStorageError(
            "capacity_bytes must equal the calculated peak+reserve contract "
            f"({CALCULATED_CAPACITY_BYTES})"
        )
    if (
        host_root == "/"
        or not _ABS_PATH_RE.fullmatch(host_root)
        or ".." in host_root.split("/")
    ):
        raise MediaStorageError("host_bind_root must be a safe absolute path")
    if not fs_uuid or not re.fullmatch(r"[0-9a-fA-F-]{8,64}", fs_uuid):
        raise MediaStorageError("fs_uuid missing or malformed")
    if not source_device.startswith("/dev/"):
        raise MediaStorageError(
            "source_device must be a /dev path (not invented at parse)"
        )
    if fs_type not in ALLOWED_FS_TYPES:
        raise MediaStorageError(f"fs_type must be one of {sorted(ALLOWED_FS_TYPES)}")
    if (
        raw.get("require_dedicated_device", True) is not True
        or raw.get("reject_root_fs", True) is not True
    ):
        raise MediaStorageError("dedicated device and root-FS rejection are mandatory")
    if not isinstance(layout_raw, dict):
        raise MediaStorageError("layout must be an object")
    layout: dict[str, str] = {}
    for key, default in LAYOUT_SUBDIRS.items():
        value = layout_raw.get(key, default)
        if (
            not isinstance(value, str)
            or not value
            or "/" in value
            or value in {".", ".."}
        ):
            raise MediaStorageError(f"layout.{key} must be a single safe path segment")
        layout[key] = value
    return MediaStorageContract(
        approved=approved,
        backend=backend,
        capacity_bytes=capacity,
        host_bind_root=host_root,
        fs_uuid=fs_uuid,
        source_device=source_device,
        fs_type=fs_type,
        require_dedicated_device=require_dedicated,
        reject_root_fs=reject_root,
        layout=layout,
        notes=notes,
    )


def default_findmnt(path: Path) -> MountIdentity | None:
    """Read mount identity via ``findmnt -J -T`` (Linux). Injectable in tests."""
    import shutil
    import subprocess

    if shutil.which("findmnt") is None:
        return None
    proc = subprocess.run(
        [
            "findmnt",
            "-b",
            "-J",
            "-T",
            str(path),
            "-o",
            "TARGET,SOURCE,FSTYPE,UUID,OPTIONS,SIZE",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    filesystems = payload.get("filesystems")
    if not isinstance(filesystems, list) or not filesystems:
        return None
    row = filesystems[0]
    if not isinstance(row, dict):
        return None
    size_raw = row.get("size")
    size_bytes: int | None = None
    if isinstance(size_raw, str) and size_raw.isdigit():
        size_bytes = int(size_raw)
    elif isinstance(size_raw, int):
        size_bytes = size_raw
    return MountIdentity(
        target=str(row.get("target") or ""),
        source=str(row.get("source") or ""),
        fstype=str(row.get("fstype") or ""),
        fs_uuid=str(row.get("uuid") or ""),
        options=str(row.get("options") or ""),
        size_bytes=size_bytes,
    )


def default_statfs(path: Path) -> os.statvfs_result:
    return os.statvfs(path)


def _is_root_filesystem(
    identity: MountIdentity, root_identity: MountIdentity | None
) -> bool:
    if identity.target == "/":
        return True
    if root_identity is None:
        raise MediaStorageError("root filesystem identity unproven")
    if not root_identity.source or not root_identity.fstype:
        raise MediaStorageError("root filesystem identity incomplete")
    if not root_identity.fs_uuid:
        raise MediaStorageError("root filesystem UUID unproven")
    if identity.fs_uuid and root_identity.fs_uuid:
        return identity.fs_uuid.lower() == root_identity.fs_uuid.lower()
    return (
        identity.source == root_identity.source
        and identity.fstype == root_identity.fstype
    )


def assert_path_on_contract_fs(
    path: Path,
    contract: MediaStorageContract,
    *,
    findmnt: FindmntFn = default_findmnt,
    label: str,
    require_containment: bool = True,
) -> MountIdentity:
    if path.is_symlink():
        raise MediaStorageError(f"{label} is a symlink (path escape)")
    try:
        resolved = path.resolve(strict=False)
    except OSError as exc:
        raise MediaStorageError(f"{label} resolve failed: {exc}") from exc
    root = Path(contract.host_bind_root).resolve(strict=False)
    if require_containment and not resolved.is_relative_to(root):
        raise MediaStorageError(f"{label} escapes host_bind_root")
    identity = findmnt(resolved)
    if identity is None:
        raise MediaStorageError(
            f"{label} mount identity unproven (findmnt failed); "
            "a plain directory is not a hard boundary"
        )
    if contract.reject_root_fs:
        root_id = findmnt(Path("/"))
        if _is_root_filesystem(identity, root_id):
            raise MediaStorageError(
                f"{label} resolves onto the root filesystem; "
                "dedicated media FS required"
            )
    if identity.fstype != contract.fs_type:
        raise MediaStorageError(
            f"{label} fstype {identity.fstype!r} != contract {contract.fs_type!r}"
        )
    if not identity.fs_uuid or identity.fs_uuid.lower() != contract.fs_uuid.lower():
        raise MediaStorageError(
            f"{label} fs UUID {identity.fs_uuid!r} != contract {contract.fs_uuid!r}"
        )
    # findmnt may render a bind source as /dev/vdb[/subdirectory]. Strip only
    # that documented suffix, never accept arbitrary device-name prefixes.
    device = identity.source.split("[", 1)[0]
    if device.upper().startswith("UUID="):
        matches = device[5:].lower() == contract.fs_uuid.lower()
    else:
        matches = (
            bool(device)
            and Path(device).resolve() == Path(contract.source_device).resolve()
        )
    if not matches:
        raise MediaStorageError(
            f"{label} source {identity.source!r} != contract device"
        )
    if "rw" not in identity.options.split(","):
        raise MediaStorageError(f"{label} filesystem is not writable")
    return identity


def assert_media_storage_ready(
    contract: MediaStorageContract,
    *,
    findmnt: FindmntFn = default_findmnt,
    statfs: StatfsFn = default_statfs,
) -> tuple[str, ...]:
    """Fail-closed preflight for an already-provisioned media filesystem."""
    messages: list[str] = []
    root = Path(contract.host_bind_root)
    if root.is_symlink():
        raise MediaStorageError("host_bind_root must not be a symlink")
    if not root.exists():
        raise MediaStorageError(
            "host_bind_root missing; refuse Docker auto-create of bind sources"
        )
    if not root.is_dir():
        raise MediaStorageError("host_bind_root must be a directory")

    root_identity = assert_path_on_contract_fs(
        root, contract, findmnt=findmnt, label="host_bind_root"
    )
    messages.append(
        f"OK: media FS mount target={root_identity.target} "
        f"fstype={root_identity.fstype} uuid={root_identity.fs_uuid or 'n/a'}"
    )

    try:
        st = statfs(root)
    except OSError as exc:
        raise MediaStorageError(f"statfs failed on host_bind_root: {exc}") from exc
    capacity = int(st.f_blocks * st.f_frsize)
    # The approved capacity is a hard upper bound, not a minimum. The 4GiB
    # reserve permits filesystem metadata overhead; usable blocks must still
    # fit the full working/retained peak (11 artifacts).
    if (
        not contract.capacity_bytes - FS_RESERVE_BYTES
        <= capacity
        <= contract.capacity_bytes
    ):
        raise MediaStorageError(
            f"filesystem capacity {capacity} outside approved bounds"
        )
    messages.append(f"OK: capacity_bytes<={contract.capacity_bytes}")

    for key in LAYOUT_SUBDIRS:
        sub = contract.host_path_for(key)
        if sub.is_symlink():
            raise MediaStorageError(f"layout.{key} is a symlink")
        if not sub.exists():
            raise MediaStorageError(
                f"layout.{key} path missing ({sub}); "
                "refuse auto-create bind sources on root FS"
            )
        if not sub.is_dir():
            raise MediaStorageError(f"layout.{key} must be a directory")
        assert_path_on_contract_fs(
            sub, contract, findmnt=findmnt, label=f"layout.{key}"
        )
        messages.append(f"OK: layout.{key} on dedicated FS")

    free = int(st.f_bavail * st.f_frsize)
    # Must fit one worst-case operation including copies (3×MAX handoff-ish).
    min_free = 3 * MAX_ARTIFACT_BYTES
    if free < min_free:
        raise MediaStorageError(
            f"insufficient free space on media FS: free={free} < required={min_free}"
        )
    messages.append(f"OK: free_bytes>={min_free}")
    return tuple(messages)


def compose_bind_volume_specs(contract: MediaStorageContract) -> dict[str, Any]:
    """Named-volume bind redefinition so overlay mount names stay stable.

    Docker fails closed when ``device`` is missing (no silent root-FS directory).
    """
    specs: dict[str, Any] = {}
    mapping = {
        "tmp": contract.host_path_for("tmp"),
        "executor-work": contract.host_path_for("executor"),
        "media-net-work": contract.host_path_for("media_net"),
    }
    for name, host_path in mapping.items():
        specs[name] = {
            "driver": "local",
            "driver_opts": {
                "type": "none",
                "o": "bind",
                "device": str(host_path),
            },
        }
    return specs


def _storage_probe(argv: list[str]) -> Any:
    from .bounded_subprocess import BoundedSubprocessError, run_docker_probe

    try:
        return run_docker_probe(argv, cwd=Path("/"))
    except BoundedSubprocessError as exc:
        raise MediaStorageError("storage Docker probe failed") from exc


def _inspect_volume(name: str, probe: Callable[..., Any]) -> dict[str, Any]:
    result = probe(["docker", "volume", "inspect", name])
    try:
        rows = json.loads(result.stdout_text)
        if result.returncode != 0 or not isinstance(rows, list) or len(rows) != 1:
            raise ValueError("volume unavailable")
        row = rows[0]
        if not isinstance(row, dict) or row.get("Name") != name:
            raise ValueError("wrong volume")
        return row
    except (ValueError, TypeError, KeyError) as exc:
        raise MediaStorageError("actual media volume identity unproven") from exc


def assert_rendered_storage_volumes(
    rendered: dict[str, Any],
    contract: MediaStorageContract,
    *,
    probe: Callable[..., Any] | None = None,
) -> None:
    """Refuse existing legacy volumes BEFORE storage-init or container changes.

    Never remove/migrate a volume automatically. Data migration and rollback
    retention are a separately authorized operator step when this gate refuses.
    """
    probe = probe or _storage_probe
    volumes = rendered.get("volumes") or {}
    expected = compose_bind_volume_specs(contract)
    roles = {
        "worker": {
            "tmp": "tmp",
            "executor": "executor-work",
            "media_net": "media-net-work",
        },
        "media-executor": {"executor": "executor-work"},
        "media-net-executor": {"media_net": "media-net-work"},
    }
    services = rendered.get("services") or {}
    for optional in ("storage-init", "delivery"):
        if optional in services:
            roles[optional] = {"tmp": "tmp"}
    for service, roots in roles.items():
        cfg = services.get(service)
        mounts = cfg.get("volumes") if isinstance(cfg, dict) else None
        if not isinstance(mounts, list):
            raise MediaStorageError(f"{service} rendered media mounts missing")
        for key, volume in roots.items():
            selected = [
                m
                for m in mounts
                if isinstance(m, dict) and m.get("target") == MEDIA_ROOT_TARGETS[key]
            ]
            if (
                len(selected) != 1
                or selected[0].get("type") != "volume"
                or selected[0].get("source") != volume
            ):
                raise MediaStorageError(
                    f"{service} rendered media mount differs from contract"
                )
    result = probe(["docker", "volume", "ls", "--format", "{{.Name}}"])
    if result.returncode != 0:
        raise MediaStorageError("Docker volume inventory unavailable")
    existing = set(result.stdout_text.splitlines())
    for key, spec in expected.items():
        definition = volumes.get(key)
        if (
            not isinstance(definition, dict)
            or definition.get("driver_opts") != spec["driver_opts"]
            or definition.get("driver", "local") != "local"
        ):
            raise MediaStorageError(
                f"rendered media volume {key} does not match contract"
            )
        name = definition.get("name")
        if not isinstance(name, str) or not name:
            raise MediaStorageError(f"rendered media volume {key} has no exact name")
        if name in existing:
            actual = _inspect_volume(name, probe)
            if (
                actual.get("Driver") != "local"
                or actual.get("Options") != spec["driver_opts"]
            ):
                raise MediaStorageError(
                    f"existing volume {key} requires explicit data migration; "
                    "refusing reuse/recreation (no data modified)"
                )


def assert_running_storage_mounts(
    *,
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
    contract: MediaStorageContract,
    sidecars_only: bool = False,
    probe: Callable[..., Any] | None = None,
    findmnt: FindmntFn = default_findmnt,
) -> None:
    """Check actual container mounts AND the bound volume's kernel filesystem."""
    probe = probe or _storage_probe
    services: dict[str, tuple[str, ...]] = {
        "media-executor": ("executor",),
        "media-net-executor": ("media_net",),
    }
    if not sidecars_only:
        services.update(
            {
                "worker": ("tmp", "executor", "media_net"),
                "delivery": ("tmp",),
            }
        )
    keys = {"tmp": "tmp", "executor": "executor-work", "media_net": "media-net-work"}
    specs = compose_bind_volume_specs(contract)
    base = ["docker", "compose", "--env-file", str(env_file), "-p", project_name]
    for path in compose_files:
        base.extend(["-f", str(path)])
    for service, roots in services.items():
        result = probe([*base, "ps", "-a", "-q", service])
        ids = result.stdout_text.split()
        if result.returncode != 0 or len(ids) != 1:
            raise MediaStorageError(
                f"exactly one {service} container required for storage proof"
            )
        inspected = probe(["docker", "inspect", ids[0]])
        try:
            row = json.loads(inspected.stdout_text)[0]
            if inspected.returncode != 0 or not row["State"]["Running"]:
                raise ValueError("container not running")
            mounts = row["Mounts"]
            for key in roots:
                selected = [
                    m for m in mounts if m.get("Destination") == MEDIA_ROOT_TARGETS[key]
                ]
                if len(selected) != 1 or selected[0].get("Type") != "volume":
                    raise ValueError("media mount missing or wrong type")
                mount = selected[0]
                name = mount.get("Name")
                if not isinstance(name, str) or not name:
                    raise ValueError("volume name missing")
                volume = _inspect_volume(name, probe)
                if (
                    volume.get("Driver") != "local"
                    or volume.get("Options") != specs[keys[key]]["driver_opts"]
                ):
                    raise ValueError("volume options differ from contract")
                source = Path(mount["Source"])
                if str(source) != volume.get("Mountpoint"):
                    raise ValueError("mounted source differs from inspected volume")
                if mount.get("RW") is not (service != "delivery"):
                    raise ValueError("media mount access mismatch")
                assert_path_on_contract_fs(
                    source,
                    contract,
                    findmnt=findmnt,
                    label=f"{service}:{key}",
                    require_containment=False,
                )
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            raise MediaStorageError(
                f"{service} actual media mounts unproven: {exc}"
            ) from exc


def disposable_test_storage_contract(
    *,
    host_bind_root: str,
    fs_uuid: str = "00000000-0000-4000-8000-000000000001",
    source_device: str = "/dev/testdisk",
    fs_type: str = "ext4",
) -> dict[str, Any]:
    """Approved disposable contract for offline unit tests only."""
    return {
        "schema": STORAGE_SCHEMA,
        "approved": True,
        "backend": "host_managed_bind_filesystem",
        "capacity_bytes": CALCULATED_CAPACITY_BYTES,
        "host_bind_root": host_bind_root,
        "fs_uuid": fs_uuid,
        "source_device": source_device,
        "fs_type": fs_type,
        "require_dedicated_device": True,
        "reject_root_fs": True,
        "layout": dict(LAYOUT_SUBDIRS),
        "notes": "disposable test storage contract; not production",
    }


def peak_disk_breakdown() -> dict[str, Any]:
    return {
        "max_artifact_bytes": MAX_ARTIFACT_BYTES,
        "peak_working_multiplier": PEAK_WORKING_MULTIPLIER,
        "retained_artifact_headroom": RETAINED_ARTIFACT_HEADROOM,
        "fs_reserve_bytes": FS_RESERVE_BYTES,
        "capacity_bytes": CALCULATED_CAPACITY_BYTES,
        "components": {
            "media_net_active": MAX_ARTIFACT_BYTES,
            "worker_tree_v_a_out": 3 * MAX_ARTIFACT_BYTES,
            "offline_executor_tree": 3 * MAX_ARTIFACT_BYTES,
            "handoff_amplification": 2 * MAX_ARTIFACT_BYTES,
            "retained_published_x2": RETAINED_ARTIFACT_HEADROOM * MAX_ARTIFACT_BYTES,
            "fs_reserve": FS_RESERVE_BYTES,
        },
        "concurrency_note": (
            "MEDIA_EXECUTOR_MAX_JOBS=concurrency applies per executor process. "
            "offline and network executors can run simultaneously; peak model "
            "includes both. MEDIA_DOWNLOAD_CONCURRENCY=1 limits worker jobs only."
        ),
    }
