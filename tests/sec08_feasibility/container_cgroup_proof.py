"""One disposable native-container proof of the SEC-08 cgroup contract.

Host root may create the temporary slice, run Docker, and observe. Per-job
cgroups, cancellation, and reaping happen inside the executor container.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import secrets
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cgroup_contract import (  # noqa: E402
    SLICE_MEMORY_MAX,
    cgroup2_mount_points,
    cgroup_relative_path,
    control_groups_match,
    engine_version_tuple,
    memory_max_is_64m,
    narrow_caps_only,
    observe_slice_memory_max,
    preflight_blockers,
    proc_cgroup_under_control_group,
    resolve_control_group_dir,
    security_options_are_rootless,
)

ROOT = Path(__file__).resolve().parents[2]
SRC = Path(__file__).resolve().parent
OUT = ROOT / "sec08-feasibility-out"
PROBE_REQUIRED = (
    "tool_resuid_10003",
    "tool_resgid_10003",
    "tool_no_supplementary_groups",
    "tool_caps_clear",
    "tool_bounding_clear",
    "tool_nnp",
    "cannot_setuid_0",
    "cannot_setuid_10002",
    "cannot_capset",
    "sibling_read_denied",
    "published_read_denied",
    "symlink_escape_denied",
    "fork_exec_setsid_still_denied",
    "cannot_signal_sibling_same_uid",
    "cannot_open_parent_cgroup_procs",
    "cannot_write_delegated_cgroup_kill",
    "control_socket_denied",
    "abstract_uds_outside_denied",
    "no_inherited_socket_fd",
    "proc_self_denied",
)
CORE_CHECKS = (
    "preflight",
    "slice_memory_max",
    "image_build",
    "executor_runtime_constraints",
    "host_scope_matches_private_namespace",
    "visible_private_cgroup_root",
    "narrow_capabilities_without_chown",
    "supervisor_created_job_cgroups",
    "both_tasks_running",
    "joined_before_untrusted_exec",
    "tool_identity_landlock_and_cgroup_denial",
    "socket_and_workspace_without_chown",
    "cancel_writer_kept_kill_owner",
    "termination_a",
    "reaping_a",
    "job_b_continues",
    "supervisor_cannot_open_outside_subtree",
    "container_view_has_no_sentinel_scope",
    "slice_memory_max_unchanged",
    "usage_remains_under_slice",
    "job_b_visible_on_host_before_crash",
    "crash_clears_old_tool_processes",
    "fresh_instance_creates_only_its_cgroup",
    "sentinel_unchanged",
    "cleanup",
)


def run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, check=False)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.chmod(temp, 0o644)
    os.replace(temp, path)


class Proof:
    def __init__(self) -> None:
        self.suffix = secrets.token_hex(4)
        self.slice_name = f"fetchnow-sec08-cgproof-{self.suffix}.slice"
        self.unit_path = Path("/run/systemd/system") / self.slice_name
        self.image = f"fetchnow-sec08-cgproof:{self.suffix}"
        self.exec_name = f"fetchnow-sec08-cgproof-exec-{self.suffix}"
        self.fresh_name = f"fetchnow-sec08-cgproof-fresh-{self.suffix}"
        self.sentinel_name = f"fetchnow-sec08-cgproof-sentinel-{self.suffix}"
        self.containers: list[str] = []
        self.checks: list[dict] = []
        self.evidence: dict = {"suffix": self.suffix, "slice": self.slice_name}
        self.conclusion = "FAIL"
        self.cleanup_report: dict = {}
        self.slice_created = False
        self.image_id = ""
        self.control_group = ""
        self.host_slice_dir = ""
        self.host_memory_max_path = ""
        self.cgroup2_mount = ""

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.checks.append({"name": name, "status": status, "detail": detail})

    def flush(self, stage: str) -> None:
        payload = self.payload()
        atomic_write(OUT / "result.json", payload)
        stages = OUT / "stages"
        stages.mkdir(parents=True, exist_ok=True)
        atomic_write(stages / f"{stage}.json", payload)
        hashes = {}
        for path in sorted(stages.glob("*.json")):
            hashes[path.name] = sha256_file(path)
        hashes["result.json"] = sha256_file(OUT / "result.json")
        atomic_write(OUT / "stage-hashes.json", hashes)

    def payload(self) -> dict:
        counts = {"PASS": 0, "FAIL": 0, "NOT RUN": 0, "BLOCKED": 0}
        for item in self.checks:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        return {
            "kind": "sec08-container-cgroup-proof",
            "conclusion": self.conclusion,
            "counts": counts,
            "checks": self.checks,
            "evidence": self.evidence,
            "cleanup": self.cleanup_report,
            "historical_kernel_run": {
                "run": "36909616896",
                "commit": "898e65954e30d634ac7bb9715bd39a0dbccf3022",
                "result": "97 PASS / 0 FAIL",
                "role": "kernel mechanism only; this file does not replace it",
            },
        }

    def preflight(self) -> list[str]:
        version = run(["docker", "version", "--format", "{{.Server.Version}}"], 15)
        driver = run(["docker", "info", "--format", "{{.CgroupDriver}}"], 15)
        cgroup_field = run(["docker", "info", "--format", "{{.CgroupVersion}}"], 15)
        security = run(["docker", "info", "--format", "{{json .SecurityOptions}}"], 15)
        systemd = run(["systemctl", "--version"], 15)
        pid1 = run(["ps", "-p", "1", "-o", "comm="], 10)
        options: list[str] = []
        if security.returncode == 0 and security.stdout.strip():
            try:
                parsed = json.loads(security.stdout)
                if isinstance(parsed, list):
                    options = [str(item) for item in parsed]
            except json.JSONDecodeError:
                options = [security.stdout.strip()]
        controllers = Path("/sys/fs/cgroup/cgroup.controllers")
        docker_cgroup = cgroup_field.stdout.strip() if cgroup_field.returncode == 0 else ""
        cgroup_v2 = controllers.is_file() and docker_cgroup in {"", "2"}
        if docker_cgroup not in {"", "2"}:
            cgroup_v2 = False
        abi = None
        binary = OUT / "bin" / "sec08tool"
        compile_proc = run(
            ["gcc", "-O2", "-Wall", "-Wextra", "-Werror", "-o", str(binary), str(SRC / "sec08tool.c")],
            60,
        )
        self.evidence["compile"] = {
            "rc": compile_proc.returncode,
            "stderr": (compile_proc.stderr or "")[-500:],
        }
        if compile_proc.returncode == 0:
            identity = run([str(binary), "identity"], 10)
            self.evidence["identity"] = identity.stdout.strip()
            try:
                abi = int(json.loads(identity.stdout)["abi"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                abi = None
        facts = {
            "machine": platform.machine(),
            "system": platform.system(),
            "cgroup_v2": cgroup_v2,
            "docker_server_version": version.stdout.strip(),
            "cgroup_driver": driver.stdout.strip(),
            "systemd": systemd.returncode == 0 and pid1.stdout.strip() == "systemd",
            "rootful": os.geteuid() == 0 and not security_options_are_rootless(options),
            "security_options": options,
            "landlock_abi": abi,
        }
        self.evidence["preflight"] = {
            **facts,
            "docker_cgroup_version_field": docker_cgroup,
            "cgroup_controllers_present": controllers.is_file(),
            "systemd_version": systemd.stdout.splitlines()[:1],
            "pid1": pid1.stdout.strip(),
            "engine_tuple": engine_version_tuple(version.stdout),
        }
        return preflight_blockers(facts)

    def write_slice(self) -> None:
        self.unit_path.parent.mkdir(parents=True, exist_ok=True)
        self.unit_path.write_text(
            "\n".join(
                [
                    "[Unit]",
                    "Description=Disposable FetchNow SEC-08 cgroup proof",
                    "",
                    "[Slice]",
                    "MemoryAccounting=yes",
                    "MemoryMax=64M",
                    "CPUAccounting=yes",
                    "CPUQuota=50%",
                    "TasksAccounting=yes",
                    "TasksMax=64",
                    "",
                ]
            )
        )
        self.slice_created = True
        reload_proc = run(["systemctl", "daemon-reload"], 30)
        start_proc = run(["systemctl", "start", self.slice_name], 20)
        observed = self.capture_slice_limit()
        self.evidence["slice"] = {
            "unit": str(self.unit_path),
            "reload_rc": reload_proc.returncode,
            "start_rc": start_proc.returncode,
            "start_stderr": (start_proc.stderr or "")[-400:],
            **observed,
        }

    def show_value(self, prop: str) -> str:
        proc = run(["systemctl", "show", self.slice_name, "-p", prop, "--value"], 15)
        if proc.returncode != 0:
            return ""
        return proc.stdout.strip()

    def capture_slice_limit(self) -> dict:
        """Read ControlGroup from systemd and memory.max only at that path."""
        active = self.show_value("ActiveState")
        control = self.show_value("ControlGroup")
        cpu_quota_raw = self.show_value("CPUQuota")
        try:
            mounts = cgroup2_mount_points(Path("/proc/self/mountinfo").read_text())
        except OSError:
            mounts = []
        memory_text: str | None = None
        host_dir = ""
        if len(mounts) == 1:
            host_dir, _resolve_error = resolve_control_group_dir(control, mounts[0])
            if host_dir:
                memory_path = Path(host_dir) / "memory.max"
                if memory_path.is_file():
                    try:
                        memory_text = memory_path.read_text().strip()
                    except OSError:
                        memory_text = None
        observed, error = observe_slice_memory_max(
            active_state=active,
            control_group=control,
            cgroup2_mounts=mounts,
            memory_max_text=memory_text,
        )
        if not error:
            self.control_group = str(observed["control_group"])
            self.host_slice_dir = str(observed["host_dir"])
            self.host_memory_max_path = str(observed["memory_max_path"])
            self.cgroup2_mount = str(observed["cgroup2_mount"])
        return {
            "active_state": active,
            "control_group": control,
            "cgroup2_mounts": mounts,
            "host_dir": observed.get("host_dir", host_dir),
            "memory_max_path": observed.get("memory_max_path", ""),
            "memory_max_file": memory_text or "",
            "cpu_quota_raw": cpu_quota_raw,
            "error": error,
        }

    def build_image(self) -> subprocess.CompletedProcess[str]:
        proc = run(
            ["docker", "build", "--pull", "-f", str(SRC / "proof.Dockerfile"), "-t", self.image, str(SRC)],
            300,
        )
        if proc.returncode == 0:
            ident = run(["docker", "image", "inspect", "--format", "{{.Id}}", self.image], 20)
            self.image_id = ident.stdout.strip()
            self.evidence["image"] = {"id": self.image_id, "ref": self.image}
        else:
            self.evidence["image_build"] = (proc.stderr or proc.stdout)[-800:]
        return proc

    def docker_run(self, name: str, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.containers.append(name)
        return run(["docker", "run", "-d", "--name", name, *args], 60)

    def executor_args(self, out_dir: Path, mode: str) -> list[str]:
        out_dir.mkdir(parents=True, exist_ok=True)
        return [
            "--network",
            "none",
            "--cgroupns",
            "private",
            "--cgroup-parent",
            self.slice_name,
            "--security-opt",
            "writable-cgroups=true",
            "--security-opt",
            "no-new-privileges:true",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "SETUID",
            "--cap-add",
            "SETGID",
            "--cap-add",
            "SETPCAP",
            "--pids-limit",
            "64",
            "-e",
            "PYTHONUNBUFFERED=1",
            "-e",
            "PROOF_OUT=/out",
            "-e",
            "HOST_SENTINEL_CGROUP="
            + str(self.evidence.get("sentinel", {}).get("host_cgroup_dir", "")),
            "-e",
            f"HOST_SLICE_MEMORY_PATH={self.host_memory_max_path}",
            "-v",
            f"{SRC}:/opt/sec08-trusted/harness:ro",
            "-v",
            f"{out_dir}:/out",
            self.image,
            "python3",
            "/opt/sec08-trusted/harness/container_supervisor.py",
            mode,
        ]

    def inspect_security(self, name: str) -> dict:
        proc = run(["docker", "inspect", name], 20)
        if proc.returncode != 0:
            return {"error": (proc.stderr or "")[-400:]}
        raw = json.loads(proc.stdout)[0]
        host = raw.get("HostConfig") or {}
        state = raw.get("State") or {}
        kept = {
            "id": raw.get("Id"),
            "image": raw.get("Image"),
            "privileged": host.get("Privileged"),
            "cap_add": host.get("CapAdd"),
            "cap_drop": host.get("CapDrop"),
            "security_opt": host.get("SecurityOpt"),
            "cgroupns_mode": host.get("CgroupnsMode"),
            "cgroup_parent": host.get("CgroupParent"),
            "network_mode": host.get("NetworkMode"),
            "pid": state.get("Pid"),
            "running": state.get("Running"),
        }
        return kept

    def scope_facts(self, host_pid: int) -> dict:
        cgroup = ""
        try:
            cgroup = Path(f"/proc/{host_pid}/cgroup").read_text().strip()
        except OSError as exc:
            return {"error": exc.strerror, "cgroup": ""}
        scope = ""
        for part in cgroup.split("/"):
            if part.startswith("docker-") and part.endswith(".scope"):
                scope = part
        shown = ""
        if scope:
            proc = run(
                ["systemctl", "show", scope, "-p", "Delegate", "-p", "Slice", "-p", "ControlGroup", "-p", "MemoryMax"],
                15,
            )
            shown = proc.stdout.strip()
        return {"cgroup": cgroup, "scope": scope, "systemd": shown, "host_pid": host_pid}

    def processes_in(self, marker: str) -> list[dict]:
        found = []
        if not marker or marker in {"/", "."} or ".." in marker.split("/"):
            return found
        proc_root = Path("/proc")
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cgroup = (entry / "cgroup").read_text()
                if marker not in cgroup:
                    continue
                status = (entry / "status").read_text()
                stat = (entry / "stat").read_text()
            except OSError:
                continue
            nspid = ""
            for line in status.splitlines():
                if line.startswith("NSpid:"):
                    nspid = line.split(":", 1)[1].strip()
            found.append({"host_pid": int(entry.name), "nspid": nspid, "cgroup": cgroup.strip(), "stat": stat.strip()})
        return found

    def sentinel_view(self) -> dict:
        info = self.inspect_security(self.sentinel_name)
        pid = int(info.get("pid") or 0)
        view: dict = {"inspect_pid": pid}
        if pid:
            facts = self.scope_facts(pid)
            view.update(facts)
            memory = ""
            relative = cgroup_relative_path(facts.get("cgroup") or "")
            host_dir, path_error = ("", "cgroup2 mount is not confirmed")
            if self.cgroup2_mount:
                host_dir, path_error = resolve_control_group_dir(relative, self.cgroup2_mount)
            view["host_cgroup_dir"] = host_dir
            view["host_path_error"] = path_error
            if host_dir:
                memory_file = Path(host_dir) / "memory.max"
                if memory_file.is_file():
                    memory = memory_file.read_text().strip()
            view["memory_max"] = memory
        hb = OUT / "sentinel" / "hb"
        view["heartbeat_bytes"] = hb.stat().st_size if hb.exists() else 0
        return view

    def slice_memory(self) -> str:
        again = self.show_value("ControlGroup")
        self.evidence.setdefault("control_group_rereads", []).append(again)
        if not control_groups_match(self.control_group, again):
            return f"control-group-changed:{again}"
        if not self.host_memory_max_path:
            return "missing"
        path = Path(self.host_memory_max_path)
        if not path.is_file():
            return "missing"
        return path.read_text().strip()

    def wait_hold(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        target = OUT / "inside" / "hold"
        while time.monotonic() < deadline:
            if target.exists():
                return True
            time.sleep(0.25)
        return False

    def evaluate(self, supervisor: dict) -> None:
        self.add(
            "visible_private_cgroup_root",
            "PASS" if supervisor.get("cgroup") == "0::/" and "/ /sys/fs/cgroup rw" in supervisor.get("mountinfo", "") else "FAIL",
            supervisor.get("mountinfo", "")[:300],
        )
        self.add(
            "narrow_capabilities_without_chown",
            "PASS" if narrow_caps_only(str(supervisor.get("cap_eff") or "0")) else "FAIL",
            str(supervisor.get("cap_eff")),
        )
        created = supervisor.get("created_cgroups") or []
        self.add(
            "supervisor_created_job_cgroups",
            "PASS" if created == ["fn-a", "fn-b", "fn-probe"] and "existed before" not in str(supervisor.get("error")) else "FAIL",
            str(created),
        )
        before = supervisor.get("before") or {}
        both = True
        placed = True
        for name in ("a", "b"):
            parent = (before.get(name) or {}).get("parent") or {}
            child = (before.get(name) or {}).get("child") or {}
            if parent.get("class") not in {"running", "sleeping"} or child.get("class") not in {"running", "sleeping"}:
                both = False
            if not str(parent.get("cgroup", "")).endswith(f"/fn-{name}"):
                placed = False
            if not str(child.get("cgroup", "")).endswith(f"/fn-{name}"):
                placed = False
            if child.get("ppid") != 1 or child.get("session") == parent.get("session"):
                placed = False
        self.add("both_tasks_running", "PASS" if both else "FAIL", json.dumps(before)[:500])
        self.add("joined_before_untrusted_exec", "PASS" if placed else "FAIL", "cgroup membership and setsid child ppid")
        probe = supervisor.get("probe") or {}
        by_name = {item.get("name"): item for item in probe.get("checks") or []}
        missing = [name for name in PROBE_REQUIRED if not (by_name.get(name) or {}).get("pass")]
        self.add(
            "tool_identity_landlock_and_cgroup_denial",
            "PASS" if not missing and supervisor.get("probe_rc") == 0 else "FAIL",
            ",".join(missing) or "required probe checks passed",
        )
        sock = supervisor.get("socket") or {}
        work = supervisor.get("work") or {}
        ownership_ok = (
            sock.get("uid") == 10002
            and sock.get("gid") == 10001
            and sock.get("mode") == "0o660"
            and supervisor.get("connect_worker") == "ok"
            and str(supervisor.get("connect_tool", "")).startswith("errno 13")
            and "uid=10003" in str(work.get("tree"))
            and "gid=10001" in str(work.get("tree"))
        )
        self.add(
            "socket_and_workspace_without_chown",
            "PASS" if ownership_ok else "FAIL",
            json.dumps({"socket": sock, "connect_tool": supervisor.get("connect_tool"), "work": work.get("tree")}),
        )
        self.add(
            "cancel_writer_kept_kill_owner",
            "PASS" if supervisor.get("kill_owner_unchanged") and supervisor.get("kill_writer_euid") == 0 else "FAIL",
            json.dumps(supervisor.get("cgroup_kill_a_owner")),
        )
        after = supervisor.get("after_a") or {}
        waited = set(supervisor.get("waited") or [])
        a_pids = {
            (before.get("a") or {}).get("parent", {}).get("pid"),
            (before.get("a") or {}).get("child", {}).get("pid"),
        }
        terminated = all((after.get(role) or {}).get("class") in {"zombie", "reaped"} for role in ("parent", "child"))
        reaped = terminated and a_pids.issubset(waited) and all(
            (after.get(role) or {}).get("class") == "reaped" for role in ("parent", "child")
        )
        self.add("termination_a", "PASS" if terminated else "FAIL", json.dumps(after)[:400])
        self.add("reaping_a", "PASS" if reaped else "FAIL", json.dumps(supervisor.get("wait_status", {}))[:400])
        b_after = supervisor.get("b_after") or {}
        b_ok = (
            b_after.get("grew") is True
            and (b_after.get("parent") or {}).get("class") in {"running", "sleeping"}
            and (b_after.get("child") or {}).get("class") in {"running", "sleeping"}
        )
        self.add("job_b_continues", "PASS" if b_ok else "FAIL", json.dumps(b_after)[:400])
        outside = supervisor.get("outside") or {}
        hidden = (
            str(outside.get("sentinel", "")).startswith("errno")
            and str(outside.get("slice", "")).startswith("errno")
            and outside.get("dotdot_same_as_root") is True
        )
        self.add("supervisor_cannot_open_outside_subtree", "PASS" if hidden else "FAIL", json.dumps(outside))
        top = " ".join(supervisor.get("cgroup_top") or [])
        self.add(
            "container_view_has_no_sentinel_scope",
            "PASS" if "docker-" not in top and self.suffix not in top else "FAIL",
            top[:300],
        )

    def execute(self) -> int:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "bin").mkdir(exist_ok=True)
        blockers = self.preflight()
        self.flush("preflight")
        if blockers:
            for reason in blockers:
                self.add("preflight", "BLOCKED", reason)
            self.conclusion = "BLOCKED"
            self.flush("blocked")
            return 2
        self.add("preflight", "PASS", json.dumps(self.evidence["preflight"])[:400])
        self.write_slice()
        self.flush("slice")
        if not memory_max_is_64m(str(self.evidence["slice"].get("memory_max_file"))):
            self.add("slice_memory_max", "FAIL", str(self.evidence["slice"]))
            self.conclusion = "FAIL"
            self.flush("slice-fail")
            return 1
        self.add("slice_memory_max", "PASS", str(self.evidence["slice"].get("memory_max_file")))

        build = self.build_image()
        if build.returncode != 0:
            self.add("image_build", "FAIL", (build.stderr or "")[-300:])
            self.conclusion = "FAIL"
            self.flush("image-fail")
            return 1
        self.add("image_build", "PASS", self.image_id)

        sentinel_dir = OUT / "sentinel"
        sentinel_dir.mkdir(exist_ok=True)
        sentinel = self.docker_run(
            self.sentinel_name,
            [
                "--network",
                "none",
                "-v",
                f"{sentinel_dir}:/tmp",
                self.image,
                "sh",
                "-c",
                "while true; do echo x >> /tmp/hb; sleep 1; done",
            ],
        )
        if sentinel.returncode != 0:
            self.add("sentinel_start", "FAIL", (sentinel.stderr or "")[-300:])
            self.conclusion = "FAIL"
            self.flush("sentinel-fail")
            return 1
        time.sleep(1.2)
        before_sentinel = self.sentinel_view()
        self.evidence["sentinel_before"] = before_sentinel
        self.evidence["sentinel"] = before_sentinel
        self.flush("sentinel")

        memory_before = self.slice_memory()
        started = self.docker_run(self.exec_name, self.executor_args(OUT / "inside", "full"))
        if started.returncode != 0:
            self.add("executor_start", "FAIL", (started.stderr or "")[-400:])
            self.conclusion = "FAIL"
            self.flush("executor-fail")
            return 1
        security = self.inspect_security(self.exec_name)
        self.evidence["executor_security"] = security
        forbidden = " ".join(security.get("security_opt") or []).lower()
        security_ok = (
            security.get("privileged") is False
            and security.get("network_mode") == "none"
            and security.get("cgroupns_mode") == "private"
            and security.get("cgroup_parent") == self.slice_name
            and security.get("cap_drop") == ["ALL"]
            and set(security.get("cap_add") or []) == {"SETUID", "SETGID", "SETPCAP"}
            and "seccomp=unconfined" not in forbidden
            and "apparmor=unconfined" not in forbidden
            and "writable-cgroups=true" in (security.get("security_opt") or [])
            and "no-new-privileges:true" in (security.get("security_opt") or [])
        )
        self.add("executor_runtime_constraints", "PASS" if security_ok else "FAIL", json.dumps(security)[:500])
        scope = self.scope_facts(int(security.get("pid") or 0))
        self.evidence["executor_scope"] = scope
        linked = proc_cgroup_under_control_group(
            scope.get("cgroup", ""), self.control_group
        ) and scope.get("scope", "").endswith(".scope")
        delegate_ok = "Delegate=yes" in scope.get("systemd", "")
        self.add(
            "host_scope_matches_private_namespace",
            "PASS" if linked and delegate_ok else "FAIL",
            json.dumps(scope)[:500],
        )
        self.flush("executor-started")

        held = self.wait_hold(90)
        report_path = OUT / "inside" / "supervisor.json"
        supervisor = {}
        if report_path.exists():
            supervisor = json.loads(report_path.read_text())
        self.evidence["supervisor"] = supervisor
        if not held or supervisor.get("exit_code") not in (0, None) and supervisor.get("error"):
            logs = run(["docker", "logs", self.exec_name], 20)
            self.evidence["executor_logs"] = (logs.stdout + logs.stderr)[-2000:]
        if not held:
            self.add("supervisor_completed", "FAIL", supervisor.get("error", "hold file missing"))
            self.conclusion = "FAIL"
            self.flush("supervisor-fail")
            return 1
        self.evaluate(supervisor)
        memory_after = self.slice_memory()
        self.evidence["slice_memory_before"] = memory_before
        self.evidence["slice_memory_after"] = memory_after
        self.add(
            "slice_memory_max_unchanged",
            "PASS" if memory_before == memory_after and memory_max_is_64m(memory_after) else "FAIL",
            f"{memory_before} -> {memory_after}; local write {supervisor.get('memory_write')}",
        )
        current = Path(self.host_slice_dir) / "memory.current" if self.host_slice_dir else Path()
        current_value = current.read_text().strip() if current.is_file() else "missing"
        self.evidence["slice_memory_current"] = current_value
        try:
            charged = 0 < int(current_value) < SLICE_MEMORY_MAX
        except ValueError:
            charged = False
        self.add(
            "usage_remains_under_slice",
            "PASS" if charged else "FAIL",
            current_value,
        )
        host_procs = self.processes_in(self.control_group)
        self.evidence["executor_processes_before_crash"] = [
            {"host_pid": item["host_pid"], "nspid": item["nspid"], "cgroup": item["cgroup"]} for item in host_procs
        ]
        b_ns = str((supervisor.get("b_after") or {}).get("parent", {}).get("pid"))
        b_alive = any(item["nspid"].split()[-1:] == [b_ns] for item in host_procs if item["nspid"])
        self.add("job_b_visible_on_host_before_crash", "PASS" if b_alive else "FAIL", b_ns)
        self.flush("before-crash")

        killed = run(["docker", "kill", self.exec_name], 20)
        self.evidence["docker_kill_rc"] = killed.returncode
        deadline = time.monotonic() + 10
        gone = False
        later: list[dict] = []
        marker = scope.get("scope") or ""
        while marker and time.monotonic() < deadline:
            later = self.processes_in(marker)
            if not later:
                gone = True
                break
            time.sleep(0.25)
        self.evidence["executor_processes_after_crash"] = later
        self.add(
            "crash_clears_old_tool_processes",
            "PASS" if gone and killed.returncode == 0 and bool(marker) else "FAIL",
            marker or "scope missing",
        )

        fresh = self.docker_run(self.fresh_name, self.executor_args(OUT / "fresh", "fresh"))
        fresh_report = {}
        if fresh.returncode == 0:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                path = OUT / "fresh" / "supervisor.json"
                if path.exists():
                    fresh_report = json.loads(path.read_text())
                    if "fresh_only_own" in fresh_report:
                        break
                time.sleep(0.25)
        self.evidence["fresh"] = fresh_report
        self.add(
            "fresh_instance_creates_only_its_cgroup",
            "PASS" if fresh_report.get("fresh_only_own") is True else "FAIL",
            json.dumps(fresh_report)[:400],
        )
        after_sentinel = self.sentinel_view()
        self.evidence["sentinel_after"] = after_sentinel
        sentinel_ok = (
            after_sentinel.get("inspect_pid") == before_sentinel.get("inspect_pid")
            and after_sentinel.get("cgroup") == before_sentinel.get("cgroup")
            and after_sentinel.get("memory_max") == before_sentinel.get("memory_max")
            and after_sentinel.get("heartbeat_bytes", 0) > before_sentinel.get("heartbeat_bytes", 0)
        )
        self.add("sentinel_unchanged", "PASS" if sentinel_ok else "FAIL", json.dumps(after_sentinel)[:400])
        failed = any(item["status"] == "FAIL" for item in self.checks)
        self.conclusion = "FAIL" if failed else "PASS"
        self.flush("final")
        return 1 if failed else 0

    def remove_resources(self) -> None:
        errors = []
        for name in self.containers:
            proc = run(["docker", "rm", "-f", name], 30)
            if proc.returncode != 0 and "No such container" not in (proc.stderr or ""):
                errors.append(f"{name}: {(proc.stderr or '')[-200:]}")
        if self.slice_created:
            memory = Path(self.host_slice_dir) if self.host_slice_dir else None
            leftover = []
            if memory is not None and memory.is_dir():
                leftover = [path.name for path in memory.iterdir() if path.is_dir() or path.name == "cgroup.procs"]
                procs = memory / "cgroup.procs"
                if procs.is_file() and procs.read_text().strip():
                    errors.append(f"slice procs still populated: {procs.read_text().strip()}")
            stop = run(["systemctl", "stop", self.slice_name], 20)
            if stop.returncode != 0:
                errors.append(f"stop slice: {(stop.stderr or '')[-200:]}")
            if self.unit_path.exists():
                # Only the unit this process created.
                if self.unit_path.name == self.slice_name:
                    self.unit_path.unlink()
                else:
                    errors.append("refusing to delete an unexpected unit path")
            reload_proc = run(["systemctl", "daemon-reload"], 30)
            if reload_proc.returncode != 0:
                errors.append(f"daemon-reload: {(reload_proc.stderr or '')[-200:]}")
            self.cleanup_report["slice_children_seen"] = leftover
        if self.image_id:
            removed = run(["docker", "rmi", self.image], 60)
            if removed.returncode != 0:
                errors.append(f"rmi: {(removed.stderr or '')[-200:]}")
        self.cleanup_report["errors"] = errors
        self.cleanup_report["ok"] = not errors


def main() -> int:
    proof = Proof()
    code = 1
    try:
        code = proof.execute()
    except Exception as exc:  # noqa: BLE001 - partial evidence must be kept
        proof.evidence["exception"] = f"{type(exc).__name__}: {exc}"
        proof.add("proof_exception", "FAIL", str(exc))
        proof.conclusion = "FAIL"
        code = 1
    finally:
        try:
            proof.remove_resources()
        except Exception as exc:  # noqa: BLE001
            proof.cleanup_report.setdefault("errors", []).append(str(exc))
            proof.cleanup_report["ok"] = False
        if proof.cleanup_report.get("errors"):
            if proof.conclusion == "PASS":
                proof.conclusion = "CLEANUP_FAILURE"
                code = 1
            proof.add("cleanup", "FAIL", "; ".join(proof.cleanup_report.get("errors") or []))
        elif proof.slice_created or proof.containers:
            proof.add("cleanup", "PASS", proof.slice_name)
        present = {item["name"] for item in proof.checks}
        if proof.conclusion == "PASS":
            missing = [name for name in CORE_CHECKS if name not in present]
            if missing:
                proof.conclusion = "FAIL"
                code = 1
                for name in missing:
                    proof.add(name, "FAIL", "missing from the pass report")
        else:
            for name in CORE_CHECKS:
                if name not in present:
                    proof.add(name, "NOT RUN", "")
        proof.flush("cleanup")
    print(json.dumps({"conclusion": proof.conclusion, "counts": proof.payload()["counts"]}, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
