#!/usr/bin/env python3
"""Native Linux AMD64 acceptance for media storage + resource budget (S1–S9).

This is the full Compose/cgroup/release orchestrator. It does NOT turn the
filesystem-only shell harness exit 78 into a green PASS.

Explicit execution only:
  FETCHNOW_NATIVE_MEDIA_STORAGE_RESOURCE_ACCEPTANCE_EXECUTE=1

Overall PASS requires every required case PASS (no NOT_RUN / BLOCKED / FAIL).
Disposable resources only; cleanup deletes recorded identities and refuses
recursive deletion after unmount/detach failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from fetchnow_release.media_storage import (
    CALCULATED_CAPACITY_BYTES,
    FS_RESERVE_BYTES,
    LAYOUT_SUBDIRS,
    MediaStorageError,
    assert_media_storage_ready,
    assert_path_on_contract_fs,
    parse_media_storage_contract,
)
from fetchnow_release.rollout_project import assert_rollout_project

EXECUTE_ENV = "FETCHNOW_NATIVE_MEDIA_STORAGE_RESOURCE_ACCEPTANCE_EXECUTE"
REQUIRED_CASES = (
    "S1_environment_identity",
    "S2_fault_injection_enospc",
    "S2_contract_geometry_fs",
    "S3_container_mounts",
    "S4_missing_or_wrong_fs",
    "S5_legacy_volume_refusal",
    "S6_sidecar_resources",
    "S7_media_operations",
    "S8_transaction_rollback",
    "S9_cleanup",
)

# Production proposal stays approved:false; disposable approval is evidence-only.
DISPOSABLE_BUDGET: dict[str, Any] = {
    "approved": True,
    "aggregate_memory_max_bytes": 2281701376,
    "aggregate_cpu_quota_milli": 1000,
    "aggregate_tasks_max": 512,
    "memory_swap_max_bytes": 0,
    "per_container_memory_bytes": {
        "media-executor": 805306368,
        "media-net-executor": 1207959552,
        "egress-proxy": 268435456,
    },
    "concurrency": 1,
    "shared_ceiling": False,
    "notes": (
        "disposable native acceptance budget aligned to 2176M/swap=0 proposal; "
        "NOT production owner approval"
    ),
}


@dataclass
class CaseResult:
    name: str
    status: str  # PASS | FAIL | NOT_RUN | BLOCKED
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
        }
        if self.evidence:
            out["evidence"] = self.evidence
        return out


@dataclass
class TrackedIdentity:
    kind: str
    value: str
    meta: dict[str, Any] = field(default_factory=dict)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = 120,
    check: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed rc={proc.returncode}: {' '.join(argv)}\n"
            f"stdout={proc.stdout[-2000:]}\nstderr={proc.stderr[-2000:]}"
        )
    return proc


def landlock_abi() -> int:
    import ctypes
    import ctypes.util

    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    return int(libc.syscall(444, None, 0, 1))


def free_bytes(path: Path) -> int:
    st = os.statvfs(path)
    return int(st.f_bavail * st.f_frsize)


def overall_status(cases: list[CaseResult]) -> str:
    by_name = {c.name: c for c in cases}
    if any(required not in by_name for required in REQUIRED_CASES):
        return "FAIL"
    statuses = {by_name[name].status for name in REQUIRED_CASES}
    # Execution order differs from REQUIRED_CASES (S5 runs before S3 completes).
    # A known failure must dominate unreached cases, never be reported NOT_RUN.
    if statuses - {"PASS", "FAIL", "NOT_RUN", "BLOCKED"} or "FAIL" in statuses:
        return "FAIL"
    if "BLOCKED" in statuses:
        return "BLOCKED"
    if "NOT_RUN" in statuses:
        return "NOT_RUN"
    return "PASS"


def mark_remaining_not_run(
    planned: tuple[str, ...], recorded: list[CaseResult], *, reason: str
) -> list[CaseResult]:
    seen = {c.name for c in recorded}
    extra: list[CaseResult] = []
    for name in planned:
        if name not in seen:
            extra.append(CaseResult(name=name, status="NOT_RUN", detail=reason))
    return extra


def mark_remaining_blocked(
    planned: tuple[str, ...], recorded: list[CaseResult], *, reason: str
) -> list[CaseResult]:
    seen = {c.name for c in recorded}
    extra: list[CaseResult] = []
    for name in planned:
        if name not in seen:
            extra.append(CaseResult(name=name, status="BLOCKED", detail=reason))
    return extra


REQUIRED_BEFORE_CLEANUP = tuple(n for n in REQUIRED_CASES if n != "S9_cleanup")


def upsert_case(cases: list[CaseResult], case: CaseResult) -> None:
    """Replace an existing case by name, or append. Used so S9 cannot stay NOT_RUN."""
    for i, existing in enumerate(cases):
        if existing.name == case.name:
            cases[i] = case
            return
    cases.append(case)


class AcceptanceOrchestrator:
    def __init__(
        self, evidence_dir: Path, *, evidence_owner: tuple[int, int] | None = None
    ) -> None:
        self.evidence = evidence_dir
        self.evidence_owner = evidence_owner
        self.cases: list[CaseResult] = []
        self.tracked: list[TrackedIdentity] = []
        self.work: Path | None = None
        self.loop_dev: str | None = None
        self.loop_file: Path | None = None
        self.mount_point: Path | None = None
        self.fault_loop_dev: str | None = None
        self.fault_loop_file: Path | None = None
        self.fault_mount: Path | None = None
        self.project: str | None = None
        self.slice_unit: str | None = None
        self.slice_path: Path | None = None
        self.contract: dict[str, Any] | None = None
        self.image_ids: dict[str, str] = {}
        self.source_sha: str = ""
        self.cleanup_ok = True
        self.cleanup_detail = ""
        self._blocked_reason: str | None = None
        self.current_release_case: str | None = None

    def record(self, name: str, status: str, detail: str = "", **evidence: Any) -> None:
        self.cases.append(
            CaseResult(name=name, status=status, detail=detail, evidence=dict(evidence))
        )
        self._write_partial()

    def track(self, kind: str, value: str, **meta: Any) -> None:
        self.tracked.append(TrackedIdentity(kind=kind, value=value, meta=dict(meta)))

    def begin_case(self, name: str) -> None:
        if name not in REQUIRED_CASES or name[:2] not in {
            "S3",
            "S4",
            "S5",
            "S6",
            "S7",
            "S8",
        }:
            raise ValueError(f"invalid release case: {name}")
        self.current_release_case = name
        self._write_partial()

    def _record_release_failure(self, exc: Exception) -> None:
        case = self.current_release_case or "S3_container_mounts"
        existing = next((c for c in self.cases if c.name == case), None)
        # Explicit FAIL evidence is retained. Any later exception must also
        # overturn a prematurely recorded PASS, never disappear in deduplication.
        if existing is None or existing.status != "FAIL":
            upsert_case(
                self.cases,
                CaseResult(
                    name=case,
                    status="FAIL",
                    detail=f"aborted: {type(exc).__name__}: {exc}",
                ),
            )
        self._write_partial()
        (self.evidence / "primary_failure.json").write_text(
            json.dumps(
                {
                    "stage": "release_path",
                    "case": case,
                    "error_type": type(exc).__name__,
                    "error_detail": str(exc)[:4000],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _write_partial(self) -> None:
        payload = {
            "status": "IN_PROGRESS",
            "current_release_case": self.current_release_case,
            "cases": [c.to_dict() for c in self.cases],
            "tracked": [
                {"kind": t.kind, "value": t.value, "meta": t.meta} for t in self.tracked
            ],
        }
        (self.evidence / "partial_result.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def write_final(self, status: str, extra: dict[str, Any] | None = None) -> Path:
        result: dict[str, Any] = {
            "schema": "fetchnow-native-media-storage-resource-acceptance/v1",
            "status": status,
            "source_sha": self.source_sha,
            "current_release_case": self.current_release_case,
            "required_cases": list(REQUIRED_CASES),
            "cases": [c.to_dict() for c in self.cases],
            "case_counts": {
                "PASS": sum(1 for c in self.cases if c.status == "PASS"),
                "FAIL": sum(1 for c in self.cases if c.status == "FAIL"),
                "NOT_RUN": sum(1 for c in self.cases if c.status == "NOT_RUN"),
                "BLOCKED": sum(1 for c in self.cases if c.status == "BLOCKED"),
            },
            "tracked_identities": [
                {"kind": t.kind, "value": t.value, "meta": t.meta} for t in self.tracked
            ],
            "image_ids": dict(self.image_ids),
            "contract": self.contract,
            "cleanup_ok": self.cleanup_ok,
            "cleanup_detail": self.cleanup_detail,
            "fresh_vs_mock": {
                "filesystem": "native_loop_ext4",
                "containers": "native_docker",
                "images": "fresh_build_or_prepare" if self.image_ids else "none",
                "mocked": False,
            },
            "security_residual_acceptance": False,
            "production_install": False,
            "production_readiness": False,
        }
        if extra:
            result.update(extra)
        path = self.evidence / "result.json"
        path.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return path

    # --- S1 -----------------------------------------------------------------
    def case_s1(self) -> None:
        if platform.system() != "Linux" or platform.machine() not in {
            "x86_64",
            "amd64",
        }:
            self.record(
                "S1_environment_identity",
                "NOT_RUN",
                "requires Linux x86_64",
            )
            raise RuntimeError("S1 NOT_RUN")
        if os.geteuid() != 0:
            self.record("S1_environment_identity", "NOT_RUN", "requires root")
            raise RuntimeError("S1 NOT_RUN")
        docker = run(
            ["docker", "version", "--format", "{{.Server.Version}}"], check=True
        )
        version = docker.stdout.strip()
        major = int(version.split(".", 1)[0])
        if major < 24:
            self.record(
                "S1_environment_identity",
                "FAIL",
                f"Docker {version} below supported floor",
            )
            raise RuntimeError("S1 FAIL")
        info = run(
            ["docker", "info", "--format", "{{.CgroupDriver}} {{.CgroupVersion}}"],
            check=True,
        )
        cgroup_line = info.stdout.strip()
        if not re.search(r"systemd (2|v2)", cgroup_line):
            self.record(
                "S1_environment_identity",
                "FAIL",
                f"cgroup v2/systemd required, got {cgroup_line!r}",
            )
            raise RuntimeError("S1 FAIL")
        abi = landlock_abi()
        if abi < 6:
            self.record(
                "S1_environment_identity",
                "FAIL",
                f"Landlock ABI {abi} < 6",
            )
            raise RuntimeError("S1 FAIL")
        self.source_sha = run(
            ["git", "rev-parse", "HEAD"], cwd=_ROOT, check=True
        ).stdout.strip()
        disk = free_bytes(Path("/"))
        mem = Path("/proc/meminfo").read_text(encoding="utf-8")
        mem_total = int(
            next(
                line for line in mem.splitlines() if line.startswith("MemTotal:")
            ).split()[1]
        )
        self.record(
            "S1_environment_identity",
            "PASS",
            "Linux AMD64 rootful Docker cgroup v2 Landlock OK",
            architecture=platform.machine(),
            docker_version=version,
            cgroup=cgroup_line,
            landlock_abi=abi,
            source_sha=self.source_sha,
            free_disk_bytes=disk,
            mem_total_kib=mem_total,
            calculated_capacity_bytes=CALCULATED_CAPACITY_BYTES,
        )

    # --- dedicated FS helpers -----------------------------------------------
    def _provision_loop_fs(
        self,
        *,
        size_bytes: int,
        prefix: str,
        parent: Path,
    ) -> tuple[Path, str, Path, str]:
        """Return (loop_file, loop_dev, mount_point, fs_uuid). Uses fallocate."""
        img = parent / f"{prefix}.img"
        mnt = parent / f"{prefix}-mnt"
        mnt.mkdir(parents=True, exist_ok=False)
        # fallocate reserves real blocks on ext4/xfs (not sparse pretend).
        run(["fallocate", "-l", str(size_bytes), str(img)], check=True)
        actual = img.stat().st_size
        if actual != size_bytes:
            raise RuntimeError(f"fallocate size mismatch {actual} != {size_bytes}")
        # Prove allocation is not sparse: blocks * 512 >= size (allow tiny slack).
        blocks = img.stat().st_blocks * 512
        if blocks + 1024 * 1024 < size_bytes:
            raise RuntimeError(
                f"backing appears sparse: allocated={blocks} requested={size_bytes}"
            )
        loop = run(["losetup", "--find", "--show", str(img)], check=True).stdout.strip()
        self.track("loop_device", loop, back_file=str(img))
        run(["mkfs.ext4", "-q", "-F", loop], check=True, timeout=300)
        run(["mount", loop, str(mnt)], check=True)
        self.track("mount", str(mnt), source=loop)
        uuid = run(
            ["findmnt", "-n", "-M", str(mnt), "-o", "UUID"], check=True
        ).stdout.strip()
        source = run(
            ["findmnt", "-n", "-M", str(mnt), "-o", "SOURCE"], check=True
        ).stdout.strip()
        if source != loop:
            raise RuntimeError(f"mount source {source!r} != {loop!r}")
        return img, loop, mnt, uuid

    def _safe_unmount(
        self, mnt: Path | None, loop: str | None, img: Path | None
    ) -> None:
        if (
            mnt is not None
            and mnt.exists()
            and run(["mountpoint", "-q", str(mnt)]).returncode == 0
        ):
            src = run(
                ["findmnt", "-n", "-M", str(mnt), "-o", "SOURCE"], check=False
            ).stdout.strip()
            if loop and src != loop:
                self.cleanup_ok = False
                self.cleanup_detail = f"mount source changed for {mnt}"
                return
            if run(["umount", str(mnt)]).returncode != 0:
                self.cleanup_ok = False
                self.cleanup_detail = f"umount failed for {mnt}; NO deletion"
                return
        if loop:
            back = run(
                ["losetup", "--noheadings", "--output", "BACK-FILE", loop], check=False
            ).stdout.strip()
            if img is not None and back and Path(back).resolve() != img.resolve():
                self.cleanup_ok = False
                self.cleanup_detail = f"loop ownership changed for {loop}"
                return
            if run(["losetup", "-d", loop]).returncode != 0:
                self.cleanup_ok = False
                self.cleanup_detail = f"detach failed for {loop}; files retained"
                return

    # --- S2 -----------------------------------------------------------------
    def case_s2_fault(self) -> None:
        assert self.work is not None
        fault_parent = self.work / "fault"
        fault_parent.mkdir()
        size = 64 * 1024 * 1024
        img, loop, mnt, uuid = self._provision_loop_fs(
            size_bytes=size, prefix="fault", parent=fault_parent
        )
        self.fault_loop_file, self.fault_loop_dev, self.fault_mount = img, loop, mnt
        for name in LAYOUT_SUBDIRS.values():
            (mnt / name).mkdir()
        sentinel = self.work / "sentinel"
        sentinel.mkdir()
        (sentinel / "before").write_text("ok\n", encoding="utf-8")
        fill = mnt / "tmp" / "fill.bin"
        proc = run(
            ["dd", "if=/dev/zero", f"of={fill}", "bs=1M"],
            timeout=60,
        )
        err = (proc.stderr or "") + (proc.stdout or "")
        if not re.search(r"No space left|Disk quota exceeded", err, re.IGNORECASE):
            # Some dd versions only put the message in stderr with non-zero rc.
            if proc.returncode == 0:
                self.record(
                    "S2_fault_injection_enospc",
                    "FAIL",
                    "writer did not hit ENOSPC/EDQUOT",
                    stderr=err[-500:],
                )
                raise RuntimeError("S2 fault FAIL")
            # Confirm FS is full via free space.
            st = os.statvfs(mnt)
            if int(st.f_bavail * st.f_frsize) > 2 * 1024 * 1024:
                self.record(
                    "S2_fault_injection_enospc",
                    "FAIL",
                    "ENOSPC text missing and free space remains",
                    stderr=err[-500:],
                    free=int(st.f_bavail * st.f_frsize),
                )
                raise RuntimeError("S2 fault FAIL")
        (sentinel / "after").write_text("still-ok\n", encoding="utf-8")
        if not (sentinel / "after").is_file():
            self.record("S2_fault_injection_enospc", "FAIL", "sentinel unwritable")
            raise RuntimeError("S2 fault FAIL")
        actual = img.stat().st_size
        if actual > size + 1024 * 1024:
            self.record(
                "S2_fault_injection_enospc",
                "FAIL",
                f"backing grew {actual} > {size}",
            )
            raise RuntimeError("S2 fault FAIL")
        # Missing mount after umount must be rejected by contract identity check.
        run(["umount", str(mnt)], check=True)
        self.fault_mount = None
        bogus = {
            "schema": "fetchnow-media-storage-contract/v1",
            "approved": True,
            "backend": "host_managed_bind_filesystem",
            "capacity_bytes": CALCULATED_CAPACITY_BYTES,
            "host_bind_root": str(mnt),
            "fs_uuid": uuid,
            "source_device": loop,
            "fs_type": "ext4",
            "require_dedicated_device": True,
            "reject_root_fs": True,
            "layout": dict(LAYOUT_SUBDIRS),
            "notes": "fault missing-mount probe",
        }
        contract = parse_media_storage_contract(bogus)
        rejected = False
        try:
            assert_path_on_contract_fs(mnt, contract, label="missing-mount")
        except MediaStorageError:
            rejected = True
        if not rejected:
            self.record(
                "S2_fault_injection_enospc",
                "FAIL",
                "missing mount was not rejected",
            )
            raise RuntimeError("S2 fault FAIL")
        run(["losetup", "-d", loop], check=True)
        self.fault_loop_dev = None
        self.record(
            "S2_fault_injection_enospc",
            "PASS",
            "small FS ENOSPC + sentinel + missing-mount reject",
            loop_bytes=actual,
            fs_uuid=uuid,
            note="fault-injection component only; not production geometry",
        )

    def case_s2_contract(self) -> bool:
        """Provision ~37GiB contract FS. Returns True if ready for release path."""
        assert self.work is not None
        need = CALCULATED_CAPACITY_BYTES + 2 * 1024**3  # loop + headroom for builds
        avail = free_bytes(Path("/"))
        if avail < need:
            self._blocked_reason = (
                f"runner free disk {avail} < required {need} for "
                f"CALCULATED_CAPACITY_BYTES={CALCULATED_CAPACITY_BYTES} "
                f"(+2GiB build headroom); not shrinking limits"
            )
            self.record(
                "S2_contract_geometry_fs",
                "BLOCKED",
                self._blocked_reason,
                free_disk_bytes=avail,
                required_bytes=need,
                calculated_capacity_bytes=CALCULATED_CAPACITY_BYTES,
            )
            return False
        parent = self.work / "contract"
        parent.mkdir()
        try:
            img, loop, mnt, uuid = self._provision_loop_fs(
                size_bytes=CALCULATED_CAPACITY_BYTES,
                prefix="contract",
                parent=parent,
            )
        except Exception as exc:  # noqa: BLE001
            self._blocked_reason = (
                f"contract FS provisioning failed: {type(exc).__name__}: {exc}"
            )
            self.record(
                "S2_contract_geometry_fs",
                "BLOCKED",
                self._blocked_reason,
            )
            return False
        self.loop_file, self.loop_dev, self.mount_point = img, loop, mnt
        for name in LAYOUT_SUBDIRS.values():
            (mnt / name).mkdir()
        source = run(
            ["findmnt", "-n", "-M", str(mnt), "-o", "SOURCE"], check=True
        ).stdout.strip()
        raw_contract = {
            "schema": "fetchnow-media-storage-contract/v1",
            "approved": True,
            "backend": "host_managed_bind_filesystem",
            "capacity_bytes": CALCULATED_CAPACITY_BYTES,
            "host_bind_root": str(mnt),
            "fs_uuid": uuid,
            "source_device": source,
            "fs_type": "ext4",
            "require_dedicated_device": True,
            "reject_root_fs": True,
            "layout": dict(LAYOUT_SUBDIRS),
            "notes": "disposable native acceptance storage; NOT production approval",
        }
        try:
            parsed = parse_media_storage_contract(raw_contract)
            msgs = assert_media_storage_ready(parsed)
        except (MediaStorageError, Exception) as exc:
            self.record(
                "S2_contract_geometry_fs",
                "FAIL",
                f"contract ready check failed: {exc}",
            )
            raise RuntimeError("S2 contract FAIL") from exc
        self.contract = parsed.to_dict()
        st = os.statvfs(mnt)
        capacity = int(st.f_blocks * st.f_frsize)
        self.record(
            "S2_contract_geometry_fs",
            "PASS",
            "dedicated contract FS mounted and validated",
            fs_uuid=uuid,
            source_device=source,
            mount_point=str(mnt),
            capacity_bytes=capacity,
            capacity_bounds=[
                CALCULATED_CAPACITY_BYTES - FS_RESERVE_BYTES,
                CALCULATED_CAPACITY_BYTES,
            ],
            ready_messages=list(msgs),
            disposable_approval=True,
            production_proposal_approved=False,
        )
        return True

    # --- cases that need contract FS + release engine -----------------------
    def _run_release_path(self) -> None:
        """S3–S8 via existing release prepare/rollout (single engine)."""
        assert self.work is not None and self.contract is not None
        from native_media_storage_resource_release_path import run_release_cases

        def set_project(name: str) -> None:
            self.project = name

        def set_slice(unit: str, path: Path) -> None:
            self.slice_unit = unit
            self.slice_path = path

        run_release_cases(
            root=_ROOT,
            work=self.work,
            contract=self.contract,
            budget_raw=DISPOSABLE_BUDGET,
            provision_loop_fs=lambda **kw: self._provision_loop_fs(**kw),
            safe_unmount=lambda mnt, loop, img: self._safe_unmount(mnt, loop, img),
            record=self.record,
            track=self.track,
            image_ids=self.image_ids,
            set_project=set_project,
            set_slice=set_slice,
            begin_case=self.begin_case,
        )

    def cleanup(self) -> None:
        # Order: containers/project → slice → mounts/loops → workdir.
        # Early abort after unmount/detach failure: no recursive deletion.
        try:
            projects = {t.value for t in self.tracked if t.kind == "compose_project"}
            if self.project:
                projects.add(self.project)
            # Validate the entire set before issuing any destructive command.
            for project in projects:
                assert_rollout_project(project)
            for project in sorted(projects):
                ids = run(
                    [
                        "docker",
                        "ps",
                        "-a",
                        "-q",
                        "--filter",
                        f"label=com.docker.compose.project={project}",
                    ],
                    check=True,
                ).stdout.split()
                if ids and run(["docker", "rm", "-f", *ids]).returncode != 0:
                    self.cleanup_ok = False
                    self.cleanup_detail = "container rm failed"
                    return
                vols = run(
                    [
                        "docker",
                        "volume",
                        "ls",
                        "-q",
                        "--filter",
                        f"name={project}_",
                    ],
                    check=True,
                ).stdout.split()
                for vol in vols:
                    if not vol.startswith(project + "_"):
                        continue
                    if run(["docker", "volume", "rm", vol]).returncode != 0:
                        self.cleanup_ok = False
                        self.cleanup_detail = f"volume rm failed: {vol}"
                        return
            for t in list(self.tracked):
                if t.kind == "image":
                    ref = t.meta.get("reference")
                    if isinstance(ref, str) and ref.startswith("fetchnow-"):
                        run(["docker", "image", "rm", ref], check=False)
            if self.slice_unit and self.slice_path:
                run(["systemctl", "stop", self.slice_unit], check=False)
                try:
                    if self.slice_path.is_file():
                        self.slice_path.unlink()
                except OSError as exc:
                    self.cleanup_ok = False
                    self.cleanup_detail = f"slice unlink failed: {exc}"
                    return
                run(["systemctl", "daemon-reload"], check=False)

            self._safe_unmount(self.mount_point, self.loop_dev, self.loop_file)
            if not self.cleanup_ok:
                return
            self._safe_unmount(
                self.fault_mount, self.fault_loop_dev, self.fault_loop_file
            )
            if not self.cleanup_ok:
                return

            if self.work is not None and self.work.exists():
                mounts = run(["findmnt", "-rn", "-o", "TARGET"], check=False).stdout
                if self.work.as_posix() + "/" in mounts or any(
                    line == self.work.as_posix() for line in mounts.splitlines()
                ):
                    self.cleanup_ok = False
                    self.cleanup_detail = "nested mount remains; NO recursive deletion"
                    return
                try:
                    shutil.rmtree(self.work)
                except OSError as exc:
                    self.cleanup_ok = False
                    self.cleanup_detail = f"workdir rmtree failed: {exc}"
                    return
        except Exception as exc:
            self.cleanup_ok = False
            self.cleanup_detail = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            upsert_case(
                self.cases,
                CaseResult(
                    name="S9_cleanup",
                    status="PASS" if self.cleanup_ok else "FAIL",
                    detail=self.cleanup_detail
                    or "scoped cleanup of recorded identities only",
                    evidence={"tracked_count": len(self.tracked)},
                ),
            )
            self._write_partial()

    def run(self) -> int:
        self.evidence.mkdir(parents=True, exist_ok=False)
        created_work = False
        try:
            self.case_s1()
            dirty = run(["git", "status", "--porcelain"], cwd=_ROOT, check=True).stdout
            if dirty.strip():
                self.record(
                    "S2_fault_injection_enospc",
                    "NOT_RUN",
                    "source must be committed and clean for native acceptance",
                )
                self.cases.extend(
                    mark_remaining_not_run(
                        REQUIRED_BEFORE_CLEANUP,
                        self.cases,
                        reason="dirty worktree",
                    )
                )
            else:
                self.work = Path(
                    tempfile.mkdtemp(prefix="fetchnow-media-storage-resource-")
                )
                created_work = True
                self.track("workdir", str(self.work))
                self.case_s2_fault()
                ready = self.case_s2_contract()
                if not ready:
                    self.cases.extend(
                        mark_remaining_blocked(
                            REQUIRED_BEFORE_CLEANUP,
                            self.cases,
                            reason=self._blocked_reason or "contract FS unavailable",
                        )
                    )
                else:
                    try:
                        self._run_release_path()
                    except Exception as exc:  # noqa: BLE001
                        self._record_release_failure(exc)
                        self.cases.extend(
                            mark_remaining_not_run(
                                REQUIRED_BEFORE_CLEANUP,
                                self.cases,
                                reason=f"aborted after {type(exc).__name__}",
                            )
                        )
        except Exception as exc:  # noqa: BLE001
            (self.evidence / "primary_failure.json").write_text(
                json.dumps(
                    {
                        "stage": "orchestrator",
                        "error_type": type(exc).__name__,
                        "error_detail": str(exc)[:4000],
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            self.cases.extend(
                mark_remaining_not_run(
                    REQUIRED_BEFORE_CLEANUP,
                    self.cases,
                    reason=f"aborted: {type(exc).__name__}: {exc}",
                )
            )
        finally:
            if created_work or self.work is not None:
                try:
                    self.cleanup()
                except Exception as exc:  # noqa: BLE001
                    self.cleanup_ok = False
                    self.cleanup_detail = f"{type(exc).__name__}: {exc}"
                    upsert_case(
                        self.cases,
                        CaseResult(
                            name="S9_cleanup",
                            status="FAIL",
                            detail=self.cleanup_detail,
                        ),
                    )
            elif not any(c.name == "S9_cleanup" for c in self.cases):
                # No disposable resources created; cleanup not applicable.
                upsert_case(
                    self.cases,
                    CaseResult(
                        name="S9_cleanup",
                        status="NOT_RUN",
                        detail="no disposable resources created",
                    ),
                )

        # Deduplicate keeping first status except S9 (always last upsert wins).
        by_name: dict[str, CaseResult] = {}
        order: list[str] = []
        for c in self.cases:
            if c.name not in by_name:
                order.append(c.name)
            if c.name == "S9_cleanup" or c.name not in by_name:
                by_name[c.name] = c
        self.cases = [by_name[n] for n in order]
        self.cases.extend(
            mark_remaining_not_run(
                REQUIRED_BEFORE_CLEANUP, self.cases, reason="never reached"
            )
        )
        if not any(c.name == "S9_cleanup" for c in self.cases):
            self.cases.append(
                CaseResult(
                    name="S9_cleanup",
                    status="NOT_RUN",
                    detail="cleanup never recorded",
                )
            )

        status = overall_status(self.cases)
        if not self.cleanup_ok and any(
            c.name == "S9_cleanup" and c.status == "FAIL" for c in self.cases
        ):
            status = "FAIL"
        result_path = self.write_final(status)
        self._write_evidence_manifest()
        self._transfer_evidence_ownership()
        print(f"result={status} path={result_path}")
        if status == "PASS":
            return 0
        if status in {"NOT_RUN", "BLOCKED"}:
            return 2
        return 1

    def _write_evidence_manifest(self) -> None:
        """Hash all published evidence files; do not nest a self-hash."""
        manifest: dict[str, str] = {}
        for path in sorted(self.evidence.rglob("*")):
            if not path.is_file():
                continue
            if path.name in {"evidence_sha256.json", "evidence_manifest.sha256"}:
                continue
            manifest[str(path.relative_to(self.evidence))] = sha256_file(path)
        man_path = self.evidence / "evidence_sha256.json"
        man_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        digest = sha256_file(man_path)
        (self.evidence / "evidence_manifest.sha256").write_text(
            digest + "\n", encoding="utf-8"
        )
        print(f"evidence_manifest_sha256={digest}")

    def _transfer_evidence_ownership(self) -> None:
        """Hand evidence to the non-root runner UID without world-writable modes."""
        if self.evidence_owner is None or os.geteuid() != 0:
            return
        uid, gid = self.evidence_owner
        if uid <= 0 or gid < 0:
            raise RuntimeError("refusing invalid evidence owner")
        for path in sorted(self.evidence.rglob("*"), reverse=True):
            os.chown(path, uid, gid)
        os.chown(self.evidence, uid, gid)


def default_guard_exit() -> int:
    print(
        "REFUSING TO RUN: native media storage/resource acceptance is prepared-only.",
        file=sys.stderr,
    )
    print(
        f"Re-run with {EXECUTE_ENV}=1 after explicit authorization.",
        file=sys.stderr,
    )
    return 78


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Native media storage/resource acceptance (S1–S9)"
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        required=True,
        help="Nonexistent directory for machine-readable evidence",
    )
    parser.add_argument(
        "--evidence-owner",
        default="",
        help="uid:gid that must own evidence after root execution (runner upload)",
    )
    args = parser.parse_args(argv)
    if os.environ.get(EXECUTE_ENV) != "1":
        return default_guard_exit()
    owner: tuple[int, int] | None = None
    if args.evidence_owner:
        try:
            uid_s, gid_s = args.evidence_owner.split(":", 1)
            owner = (int(uid_s), int(gid_s))
        except ValueError as exc:
            raise SystemExit(
                f"invalid --evidence-owner: {args.evidence_owner!r}"
            ) from exc
    orch = AcceptanceOrchestrator(args.evidence_dir.resolve(), evidence_owner=owner)
    return orch.run()


if __name__ == "__main__":
    raise SystemExit(main())
