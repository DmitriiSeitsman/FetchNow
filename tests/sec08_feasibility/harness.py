#!/usr/bin/env python3
"""Supervisor for one SEC-08 mechanism-C native feasibility run.

The process starts as uid 0 only because the container entrypoint does.
It is not the tool. sec08tool launch keeps CAP_SETUID, CAP_SETGID and
CAP_SETPCAP, applies Landlock, then setuids to 10003.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import pwd
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

MIN_ABI = 6
EXEC_UID = 10002
TOOL_UID = 10003
CANARY = "EXECUTOR_CANARY_SYNTHETIC"
ABSTRACT_NAME = "sec08-outside"
BUDGET_SEC = 10 * 60
ROOT = Path(__file__).resolve().parents[2]
SRC = Path(__file__).resolve().parent
OUT = ROOT / "sec08-feasibility-out"
WORK = Path("/var/tmp/sec08-feasibility-work")
TRUSTED = Path("/opt/sec08-trusted")
TRUSTED_BIN = TRUSTED / "bin"
CONTROL = Path("/opt/sec08-control")
LIBRARY_CANDIDATES = ("/usr", "/lib", "/lib64", "/bin", "/sbin")
TIMEOUT_ERRNOS = {110, 11, 115}  # ETIMEDOUT, EAGAIN, EINPROGRESS
PERM_ERRNOS = {13, 1}  # EACCES, EPERM
CHECKS: list[dict] = []
STAGE_HASHES: list[dict] = []
HOST_ACTIONS: list[str] = []
EXECUTOR_ACTIONS: list[str] = []
TOOL_ACTIONS: list[str] = []
STARTED = time.monotonic()
STOP = threading.Event()


def path_covers(root: str, child: str) -> bool:
    root_path = os.path.realpath(root) if os.path.exists(root) else os.path.abspath(root)
    child_path = os.path.realpath(child) if os.path.exists(child) else os.path.abspath(child)
    root_path = root_path.rstrip("/") or "/"
    child_path = child_path.rstrip("/") or "/"
    return child_path == root_path or child_path.startswith(root_path + "/")


def denial_class(ok: bool, err: int) -> str:
    if ok:
        return "success"
    if err in PERM_ERRNOS:
        return "permission"
    if err in TIMEOUT_ERRNOS:
        return "timeout"
    return "other"


def add(name: str, status: str, detail: str = "") -> None:
    CHECKS.append({"name": name, "status": status, "detail": detail})
    print(f"{status} {name} {detail}", flush=True)


def time_left() -> float:
    return BUDGET_SEC - (time.monotonic() - STARTED)


_BUDGET_MARKED = False


def budget_left(need: float = 20) -> bool:
    global _BUDGET_MARKED
    if time_left() >= need:
        return True
    if not _BUDGET_MARKED:
        _BUDGET_MARKED = True
        add("harness_budget", "FAIL", f"stopping with {time_left():.1f}s left; cleanup still runs")
    return False


def run(cmd: list[str], timeout: float = 20, **kwargs) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, text=True, capture_output=True, check=False, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = (exc.stderr or "") + "\nTIMEOUT"
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        return subprocess.CompletedProcess(cmd, 124, stdout, stderr)


def report_body(stage: str, *, partial: bool) -> dict:
    fails = [c for c in CHECKS if c["status"] == "FAIL"]
    not_run = [c for c in CHECKS if c["status"] == "NOT RUN"]
    passed = [c for c in CHECKS if c["status"] == "PASS"]
    return {
        "mechanism": "C",
        "stage": stage,
        "partial": partial,
        "min_landlock_abi": MIN_ABI,
        "scoped": ["LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET", "LANDLOCK_SCOPE_SIGNAL"],
        "handled_access_net": 0,
        "historical_run": {
            "id": 36892631777,
            "commit": "e84202186af0a719d99b5b8aafde01027b9f0245",
            "classification": "CANCELLED + confirmed isolation failures",
        },
        "deployment_prerequisites": [
            "cgroup v2 mkdir and cgroup.kill chown are performed by the root supervisor; this is not evidence a non-root Compose executor can delegate a cgroup",
            "ip netns and veth setup are performed by the root supervisor; this is not Compose network policy",
            "control-directory owner and mode are prepared by the root supervisor so the DAC contract can be observed",
        ],
        "actions": {
            "host_root_preparation": HOST_ACTIONS,
            "executor_uid_10002": EXECUTOR_ACTIONS,
            "tool_uid_10003": TOOL_ACTIONS,
        },
        "identity": IDENTITY,
        "checks": CHECKS,
        "summary": {
            "pass": len(passed),
            "fail": len(fails),
            "not_run": len(not_run),
            "fail_names": [c["name"] for c in fails],
            "not_run_names": [c["name"] for c in not_run],
        },
    }


IDENTITY: dict = {}


def flush(stage: str, *, partial: bool = True) -> str:
    OUT.mkdir(parents=True, exist_ok=True)
    body = report_body(stage, partial=partial)
    payload = json.dumps(body, indent=2) + "\n"
    tmp = OUT / "result.json.tmp"
    tmp.write_text(payload)
    os.chmod(tmp, 0o644)
    os.replace(tmp, OUT / "result.json")
    digest = hashlib.sha256((OUT / "result.json").read_bytes()).hexdigest()
    STAGE_HASHES.append({"stage": stage, "partial": partial, "sha256": digest})
    side = OUT / "stage-hashes.json.tmp"
    side.write_text(json.dumps({"stages": STAGE_HASHES}, indent=2) + "\n")
    os.replace(side, OUT / "stage-hashes.json")
    print(f"RESULT {stage} partial={str(partial).lower()} sha256={digest}", flush=True)
    return digest


def landlock_abi() -> int:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    rc = libc.syscall(ctypes.c_long(444), None, ctypes.c_size_t(0), ctypes.c_uint32(1))
    if rc < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return int(rc)


def read_status_bits() -> dict[str, str]:
    wanted = {}
    text = Path("/proc/self/status").read_text()
    for line in text.splitlines():
        key = line.split(":", 1)[0]
        if key in {"Uid", "Gid", "CapEff", "CapPrm", "CapBnd", "CapAmb", "NoNewPrivs", "Seccomp"}:
            wanted[key] = line.split(":", 1)[1].strip()
    return wanted


def ensure_user(uid: int, name: str, created: list[str]) -> None:
    try:
        existing = pwd.getpwuid(uid)
    except KeyError:
        existing = None
    if existing and existing.pw_name != name:
        add(f"uid_{uid}_available", "FAIL", f"uid {uid} is already {existing.pw_name}; refusing to reuse it")
        return
    if existing:
        add(f"uid_{uid}_available", "PASS", f"existing test user {name}")
        HOST_ACTIONS.append(f"reused existing user {name} uid {uid}")
        return
    group = run(["groupadd", "-g", str(uid), name])
    user = run(["useradd", "-u", str(uid), "-g", str(uid), "-M", "-N", "-s", "/usr/sbin/nologin", name])
    if group.returncode != 0 or user.returncode != 0:
        add(f"uid_{uid}_available", "FAIL", (group.stderr + user.stderr).strip()[:400])
        return
    created.append(name)
    HOST_ACTIONS.append(f"useradd {name} uid {uid} primary gid {uid} no supplementary group")
    add(f"uid_{uid}_available", "PASS", f"created {name}")


def compile_tool(binary: Path) -> bool:
    binary.parent.mkdir(parents=True, exist_ok=True)
    proc = run(["gcc", "-O2", "-Wall", "-Wextra", "-Werror", "-o", str(binary), str(SRC / "sec08tool.c")], timeout=60)
    if proc.returncode != 0:
        add("compile_sec08tool", "FAIL", (proc.stderr or proc.stdout)[-800:])
        return False
    os.chmod(binary, 0o755)
    HOST_ACTIONS.append(f"compiled trusted executable at {binary}, outside the work and control trees")
    add("compile_sec08tool", "PASS", str(binary))
    return True


def existing_ro_roots() -> list[str]:
    roots = [path for path in LIBRARY_CANDIDATES if os.path.isdir(path)]
    roots.append(str(TRUSTED_BIN))
    return roots


def launch(
    binary: Path,
    attempt: Path,
    argv: list[str],
    *,
    ro_roots: list[str],
    protected: list[str],
    cgroup_procs: str | None = None,
    status: Path | None = None,
    force_abi: bool = False,
    force_ruleset: bool = False,
    pass_fds: tuple[int, ...] = (),
    netns: str | None = None,
    timeout: float = 25,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        str(binary),
        "launch",
        "--attempt",
        str(attempt),
        "--binary",
        argv[0],
        "--uid",
        str(TOOL_UID),
        "--gid",
        str(TOOL_UID),
        "--control-dir",
        str(CONTROL),
    ]
    for root in ro_roots:
        cmd += ["--ro", root]
    for path in protected:
        cmd += ["--protected", path]
    if status:
        cmd += ["--status", str(status)]
    if cgroup_procs:
        cmd += ["--cgroup-procs", cgroup_procs]
    if force_abi:
        cmd.append("--force-abi")
    if force_ruleset:
        cmd.append("--force-ruleset")
    cmd.append("--")
    cmd.extend(argv)
    if netns:
        cmd = ["ip", "netns", "exec", netns, *cmd]
    env = os.environ.copy()
    env["EXECUTOR_CANARY"] = CANARY
    proc = subprocess.Popen(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        pass_fds=pass_fds,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(cmd, 124, "", "TIMEOUT_KILL")
        err = (err or "") + "\nTIMEOUT"
        return subprocess.CompletedProcess(cmd, 124, out, err)
    return subprocess.CompletedProcess(cmd, proc.returncode or 0, out, err)


def prepare_attempt(path: Path, sibling: Path, published: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o777)
    (path / "input.txt").write_text("attempt-input\n")
    os.chown(path / "input.txt", TOOL_UID, TOOL_UID)
    os.chmod(path / "input.txt", 0o666)
    sibling.parent.mkdir(parents=True, exist_ok=True)
    sibling.write_text("SIBLING_CANARY\n")
    os.chown(sibling, TOOL_UID, TOOL_UID)
    os.chmod(sibling.parent, 0o777)
    os.chmod(sibling, 0o666)
    published.parent.mkdir(parents=True, exist_ok=True)
    published.write_text("PUBLISHED_CANARY\n")
    os.chown(published, TOOL_UID, TOOL_UID)
    os.chmod(published.parent, 0o777)
    os.chmod(published, 0o666)


def load_probe_checks(path: Path, prefix: str) -> None:
    if not path.exists():
        add(f"{prefix}_result_present", "FAIL", f"missing {path}")
        return
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        add(f"{prefix}_result_present", "FAIL", f"bad json {exc}")
        return
    for item in data.get("checks", []):
        status = "PASS" if item.get("pass") is True else "FAIL"
        add(f"{prefix}_{item.get('name')}", status, str(item.get("detail", "")))


def wait_file(path: Path, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size > 0:
            return True
        time.sleep(0.05)
    return False


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def died(pid: int, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return not alive(pid)


def setup_cgroup(name: str) -> str | None:
    path = Path("/sys/fs/cgroup") / name
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        add(f"cgroup_{name}_create", "FAIL", f"{exc}")
        return None
    kill = path / "cgroup.kill"
    if not kill.exists():
        add(f"cgroup_{name}_kill_file", "FAIL", "cgroup.kill missing")
        return None
    os.chown(kill, EXEC_UID, EXEC_UID)
    os.chmod(kill, 0o200)
    os.chmod(path / "cgroup.procs", 0o644)
    HOST_ACTIONS.append(
        f"root created {path} and chowned cgroup.kill to uid {EXEC_UID}; DEPLOYMENT PREREQUISITE, not a non-root executor capability"
    )
    add(f"cgroup_{name}_delegated", "PASS", f"{kill} owner uid {EXEC_UID} prepared by root")
    return str(path)


def host_ip() -> str:
    proc = run(["ip", "-4", "route", "get", "1.1.1.1"], timeout=5)
    parts = proc.stdout.split()
    if "src" in parts:
        return parts[parts.index("src") + 1]
    return ""


def peer_cred(conn: socket.socket) -> dict[str, int]:
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    pid, uid, gid = struct.unpack("iii", raw)
    return {"pid": pid, "uid": uid, "gid": gid}


def serve_unix(sock: socket.socket, peers: list[dict], kind: str) -> None:
    sock.settimeout(0.2)
    while not STOP.is_set():
        try:
            conn, _ = sock.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            cred = peer_cred(conn)
            cred["kind"] = kind
            peers.append(cred)
            conn.sendall(b"ok")
        finally:
            conn.close()


def connect_pathname_as(uid: int, path: str) -> subprocess.CompletedProcess[str]:
    code = (
        "import socket,sys\n"
        "s=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        "s.settimeout(float(sys.argv[2]))\n"
        "s.connect(sys.argv[1])\n"
        "sys.stdout.write(s.recv(16).decode())\n"
    )
    return run(
        [
            "setpriv",
            "--reuid",
            str(uid),
            "--regid",
            str(uid),
            "--clear-groups",
            "--inh-caps=-all",
            "--ambient-caps=-all",
            "python3",
            "-c",
            code,
            path,
            "2",
        ],
        timeout=8,
    )


def connect_abstract_as(uid: int, name: str) -> subprocess.CompletedProcess[str]:
    code = (
        "import socket,sys\n"
        "s=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        "s.settimeout(2)\n"
        "s.connect('\\0'+sys.argv[1])\n"
        "sys.stdout.write(s.recv(8).decode())\n"
    )
    return run(
        [
            "setpriv",
            "--reuid",
            str(uid),
            "--regid",
            str(uid),
            "--clear-groups",
            "--inh-caps=-all",
            "--ambient-caps=-all",
            "python3",
            "-c",
            code,
            name,
        ],
        timeout=8,
    )


def start_listener(ns: str | None, host: str, port: int, ready: Path) -> subprocess.Popen[str]:
    code = (
        "import socket,sys,time\n"
        "s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "s.bind((sys.argv[1], int(sys.argv[2]))); s.listen(16)\n"
        "open(sys.argv[3],'w').write('up'); s.settimeout(0.5)\n"
        "end=time.time()+25\n"
        "while time.time()<end:\n"
        "  try:\n"
        "    c,a=s.accept(); c.close()\n"
        "  except socket.timeout:\n"
        "    pass\n"
        "s.close()\n"
    )
    cmd = ["python3", "-c", code, host, str(port), str(ready)]
    if ns:
        cmd = ["ip", "netns", "exec", ns, *cmd]
    return subprocess.Popen(cmd, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def expect_connect(table: dict, target: str, ok: bool, label: str) -> None:
    item = table.get(target)
    if not item:
        add(label, "FAIL", f"missing result for {target}")
        return
    got = bool(item.get("ok"))
    err = int(item.get("errno") or 0)
    klass = str(item.get("class") or denial_class(got, err))
    detail = f"{target} class={klass} ok={got} errno={err}"
    if ok:
        add(label, "PASS" if got else "FAIL", detail)
        return
    if got:
        add(label, "FAIL", "unexpected success " + detail)
        return
    if klass == "timeout" or err in TIMEOUT_ERRNOS:
        add(label, "FAIL", "timeout is not a security denial " + detail)
        return
    add(label, "PASS", detail)


def stop_proc(proc: subprocess.Popen[str]) -> bool:
    if proc.poll() is not None:
        return True
    proc.terminate()
    try:
        proc.wait(timeout=3)
        return True
    except subprocess.TimeoutExpired:
        proc.kill()
    try:
        proc.wait(timeout=2)
        return True
    except subprocess.TimeoutExpired:
        return False


def kill_pid_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        pid = int(path.read_text().strip())
    except ValueError:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return


def ensure_required(names: list[str]) -> None:
    have = {item["name"] for item in CHECKS}
    for name in names:
        if name not in have:
            add(name, "NOT RUN", "not reached before the harness stopped")


REQUIRED = [
    "ro_roots_do_not_cover_protected",
    "landlock_abi_meets_minimum",
    "force_abi_does_not_exec",
    "force_ruleset_does_not_exec",
    "worker_pathname_connect_allowed",
    "worker_pathname_peer_is_executor",
    "worker_abstract_connect_allowed",
    "jobA_sibling_read_denied",
    "jobA_sibling_write_denied",
    "jobA_sibling_truncate_denied",
    "jobA_rename_escape_denied",
    "jobA_link_escape_denied",
    "jobA_symlink_escape_denied",
    "jobA_published_read_denied",
    "jobA_control_socket_denied",
    "jobA_abstract_uds_outside_denied",
    "jobA_no_inherited_socket_fd",
    "jobB_sibling_read_denied",
    "jobB_sibling_write_denied",
    "jobB_sibling_truncate_denied",
    "jobB_rename_escape_denied",
    "jobB_link_escape_denied",
    "jobB_symlink_escape_denied",
    "jobB_published_read_denied",
    "signal_cannot_signal_sibling_same_uid",
    "sibling_sleeper_survived_signal_probe",
    "cancel_kills_tree_a_including_reparented",
    "cancel_leaves_tree_b",
    "cgroup_cannot_open_parent_cgroup_procs",
    "cgroup_cannot_write_delegated_cgroup_kill",
    "medianet_reaches_proxy_stub",
    "medianet_denied_internal_listener",
    "medianet_denied_metadata",
    "medianet_fails_when_proxy_down",
    "offline_denied_proxy",
    "ffmpeg_offline_fixture",
    "ffprobe_offline_fixture",
]


def main() -> int:
    if platform.system() != "Linux" or os.uname().machine != "x86_64":
        print("this harness runs only on native Linux x86_64", file=sys.stderr)
        return 2
    if os.geteuid() != 0:
        print("supervisor must start as uid 0 (container-entrypoint stand-in)", file=sys.stderr)
        return 2
    def on_term(_signum: int, _frame: object) -> None:
        raise SystemExit(1)

    signal.signal(signal.SIGTERM, on_term)
    OUT.mkdir(parents=True, exist_ok=True)
    for path in (WORK, TRUSTED, CONTROL):
        if path.exists():
            shutil.rmtree(path)
    WORK.mkdir(parents=True)
    TRUSTED_BIN.mkdir(parents=True)
    CONTROL.mkdir(parents=True)
    os.chmod(TRUSTED, 0o755)
    os.chmod(TRUSTED_BIN, 0o755)
    created_users: list[str] = []
    netns: list[str] = []
    cgroups: list[str] = []
    procs: list[subprocess.Popen[str]] = []
    host_links = ["v-host"]
    peers: list[dict] = []
    global IDENTITY
    IDENTITY = {
        "uname": " ".join(os.uname()),
        "machine": os.uname().machine,
        "os_release": Path("/etc/os-release").read_text().splitlines()[:6],
        "supervisor_status": read_status_bits(),
        "min_abi": MIN_ABI,
        "min_abi_reason": "LANDLOCK_SCOPE_SIGNAL requires ABI 6; no fallback if the scope is rejected",
        "scoped": ["LANDLOCK_SCOPE_SIGNAL", "LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET"],
        "handled_access_net": 0,
        "entrypoint_caps": ["CAP_SETGID", "CAP_SETUID", "CAP_SETPCAP"],
        "executor_uid": EXEC_UID,
        "tool_uid": TOOL_UID,
        "trusted_bin": str(TRUSTED_BIN),
        "work_root": str(WORK),
        "control_directory": str(CONTROL),
        "network_evidence": "synthetic ip-netns topology created by the root harness; not Compose network policy",
    }
    cleanup_failed = False
    try:
        try:
            abi = landlock_abi()
        except OSError as exc:
            add("landlock_abi_query", "FAIL", f"{exc}")
            abi = -1
        IDENTITY["landlock_abi"] = abi
        if abi < MIN_ABI:
            add("landlock_abi_meets_minimum", "FAIL", f"abi {abi} < {MIN_ABI}; refuse exec, no fallback")
        else:
            add("landlock_abi_meets_minimum", "PASS", f"abi {abi}")
        flush("abi")

        ensure_user(EXEC_UID, "sec08exec", created_users)
        ensure_user(TOOL_UID, "sec08tool", created_users)
        binary = TRUSTED_BIN / "sec08tool"
        if not compile_tool(binary):
            return 1
        flush("compile")

        job_a = WORK / "jobA"
        job_b = WORK / "jobB"
        published = WORK / "published" / "artifact.canary"
        sibling_a = job_b / "canary.txt"
        sibling_b = job_a / "canary-for-b.txt"
        prepare_attempt(job_a, sibling_a, published)
        job_b.mkdir(parents=True, exist_ok=True)
        os.chmod(job_b, 0o777)
        (job_b / "input.txt").write_text("attempt-b-input\n")
        os.chown(job_b / "input.txt", TOOL_UID, TOOL_UID)
        os.chmod(job_b / "input.txt", 0o666)
        sibling_b.write_text("SIBLING_B_CANARY\n")
        os.chown(sibling_b, TOOL_UID, TOOL_UID)
        os.chmod(sibling_b, 0o666)
        escape_a = job_a / "escape"
        escape_b = job_b / "escape"
        escape_a.symlink_to(sibling_a)
        escape_b.symlink_to(published)
        regain = job_a / "regain"
        shutil.copy(binary, regain)
        os.chown(regain, 0, 0)
        os.chmod(regain, 0o4755)
        HOST_ACTIONS.append(
            "POSIX canaries are mode 0666 and owned by uid 10003 so a later denial is not DAC"
        )
        os.chown(CONTROL, EXEC_UID, EXEC_UID)
        os.chmod(CONTROL, 0o750)
        sock_path = CONTROL / "worker.sock"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(sock_path))
        os.chown(sock_path, EXEC_UID, EXEC_UID)
        os.chmod(sock_path, 0o600)
        sock.listen(16)
        HOST_ACTIONS.append(
            f"control dir {CONTROL} mode 0750 owner {EXEC_UID}; socket mode 0600 owner {EXEC_UID}; tool gid {TOOL_UID} is not a member"
        )
        threading.Thread(target=serve_unix, args=(sock, peers, "pathname"), daemon=True).start()
        abstract = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        abstract.bind(b"\0" + ABSTRACT_NAME.encode())
        abstract.listen(16)
        threading.Thread(target=serve_unix, args=(abstract, peers, "abstract"), daemon=True).start()
        HOST_ACTIONS.append(f"abstract listener @{ABSTRACT_NAME} accepting outside any Landlock domain")

        worker = connect_pathname_as(EXEC_UID, str(sock_path))
        EXECUTOR_ACTIONS.append("uid 10002 pathname connect to the control socket while the listener accepts")
        worker_ok = worker.returncode == 0 and worker.stdout.strip() == "ok"
        add(
            "worker_pathname_connect_allowed",
            "PASS" if worker_ok else "FAIL",
            f"rc={worker.returncode} out={worker.stdout.strip()[:40]} err={worker.stderr[-180:]}",
        )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not any(p.get("kind") == "pathname" for p in peers):
            time.sleep(0.05)
        path_peers = [p for p in peers if p.get("kind") == "pathname" and p.get("uid") == EXEC_UID]
        add(
            "worker_pathname_peer_is_executor",
            "PASS" if path_peers else "FAIL",
            f"peers={peers}",
        )
        abstract_worker = connect_abstract_as(EXEC_UID, ABSTRACT_NAME)
        EXECUTOR_ACTIONS.append("uid 10002 abstract connect to the outside listener")
        add(
            "worker_abstract_connect_allowed",
            "PASS" if abstract_worker.returncode == 0 else "FAIL",
            f"rc={abstract_worker.returncode} err={abstract_worker.stderr[-180:]}",
        )
        flush("listeners")

        protected = [str(job_a), str(job_b), str(published.parent), str(CONTROL), str(WORK)]
        ro_roots = existing_ro_roots()
        IDENTITY["ro_roots"] = ro_roots
        IDENTITY["protected"] = protected
        covered = [
            f"{root} covers {path}"
            for root in ro_roots
            for path in protected
            if path_covers(root, path)
        ]
        if covered:
            add("ro_roots_do_not_cover_protected", "FAIL", "; ".join(covered))
            flush("ro-overlap")
            return 1
        add("ro_roots_do_not_cover_protected", "PASS", " ".join(ro_roots))
        flush("layout")

        if not budget_left(60):
            return 1
        for label, flag in (("force_abi", "abi"), ("force_ruleset", "ruleset")):
            started_mark = job_a / "tool_started"
            if started_mark.exists():
                started_mark.unlink()
            proc = launch(
                binary,
                job_a,
                [str(binary), "probe", "--mode", "full", "--result", str(job_a / f"{label}.json"),
                 "--attempt", str(job_a)],
                ro_roots=ro_roots,
                protected=protected,
                status=WORK / f"{label}-status.txt",
                force_abi=flag == "abi",
                force_ruleset=flag == "ruleset",
                timeout=15,
            )
            started = started_mark.exists()
            refused = proc.returncode != 0 and not started and proc.returncode != 124
            add(
                f"{label}_does_not_exec",
                "PASS" if refused else "FAIL",
                f"rc={proc.returncode} started={started} stderr={proc.stderr[-240:]}",
            )
        flush("refuse")

        if abi < MIN_ABI or not budget_left(90):
            return 1

        parent_procs = "/sys/fs/cgroup/cgroup.procs"

        def probe_argv(attempt: Path, escape: Path, sibling: Path, prefix: str) -> list[str]:
            return [
                str(binary), "probe", "--mode", "full", "--result", str(attempt / "result.json"),
                "--attempt", str(attempt), "--symlink", str(escape), "--sibling", str(sibling),
                "--published", str(published), "--socket", str(sock_path), "--abstract", ABSTRACT_NAME,
                "--parent-procs", parent_procs,
                "--rename-dst", str(WORK / "published" / f"renamed-from-{prefix}"),
                "--link-dst", str(WORK / "published" / f"linked-from-{prefix}"),
            ]

        def run_job_a() -> subprocess.CompletedProcess[str]:
            argv = probe_argv(job_a, escape_a, sibling_a, "a")
            argv += ["--regain-bin", str(regain)]
            return launch(
                binary, job_a, argv, ro_roots=ro_roots, protected=protected,
                status=WORK / "entrypoint-a.txt", pass_fds=(sock.fileno(),), timeout=25,
            )

        def run_job_b() -> subprocess.CompletedProcess[str]:
            return launch(
                binary, job_b, probe_argv(job_b, escape_b, sibling_b, "b"),
                ro_roots=ro_roots, protected=protected, status=WORK / "entrypoint-b.txt", timeout=25,
            )

        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            fut_a = pool.submit(run_job_a)
            fut_b = pool.submit(run_job_b)
            try:
                proc_a = fut_a.result(timeout=40)
            except concurrent.futures.TimeoutError:
                proc_a = subprocess.CompletedProcess(["jobA"], 124, "", "TIMEOUT")
            try:
                proc_b = fut_b.result(timeout=40)
            except concurrent.futures.TimeoutError:
                proc_b = subprocess.CompletedProcess(["jobB"], 124, "", "TIMEOUT")
        TOOL_ACTIONS.append("two Landlock domains, uid 10003, ran the filesystem probe concurrently")
        add(
            "concurrent_fs_pair_started",
            "PASS" if proc_a.returncode == 0 and proc_b.returncode == 0 else "FAIL",
            f"A rc={proc_a.returncode} B rc={proc_b.returncode} Aerr={proc_a.stderr[-240:]} Berr={proc_b.stderr[-240:]}",
        )
        load_probe_checks(job_a / "result.json", "jobA")
        load_probe_checks(job_b / "result.json", "jobB")
        tool_peers = [p for p in peers if p.get("uid") == TOOL_UID]
        add(
            "tool_uid_not_accepted_on_control_socket",
            "PASS" if worker_ok and not tool_peers else "FAIL",
            f"tool_peers={tool_peers}",
        )
        if (WORK / "entrypoint-a.txt").exists():
            IDENTITY["entrypoint_status"] = (WORK / "entrypoint-a.txt").read_text()[:1200]
        IDENTITY["jobA_ruleset_stderr"] = proc_a.stderr[-800:]
        flush("filesystem")

        if budget_left(40):
            exec_sleeper = subprocess.Popen(
                ["setpriv", "--reuid", str(EXEC_UID), "--regid", str(EXEC_UID), "--clear-groups",
                 "--inh-caps=-all", "--ambient-caps=-all", "sleep", "20"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            procs.append(exec_sleeper)
            sib_attempt = WORK / "signal-sibling"
            sib_attempt.mkdir()
            os.chmod(sib_attempt, 0o777)
            (sib_attempt / "input.txt").write_text("x\n")
            sib_err = open(sib_attempt / "launcher.err", "w")
            sib_proc = subprocess.Popen(
                [str(binary), "launch", "--attempt", str(sib_attempt), "--binary", str(binary),
                 "--uid", str(TOOL_UID), "--gid", str(TOOL_UID), "--control-dir", str(CONTROL),
                 *[arg for root in ro_roots for arg in ("--ro", root)],
                 *[arg for path in protected for arg in ("--protected", path)],
                 "--", str(binary), "probe", "--mode", "sleep", "--result", str(sib_attempt / "unused.json"),
                 "--attempt", str(sib_attempt)],
                stdout=subprocess.DEVNULL, stderr=sib_err, env={**os.environ, "EXECUTOR_CANARY": CANARY},
            )
            procs.append(sib_proc)
            sibling_ready = wait_file(sib_attempt / "parent.pid", 8)
            add("signal_sibling_ready", "PASS" if sibling_ready else "FAIL", "parent.pid")
            if sibling_ready and exec_sleeper.poll() is None:
                sib_pid = int((sib_attempt / "parent.pid").read_text().strip())
                sig_attempt = WORK / "signal-probe"
                sig_attempt.mkdir()
                os.chmod(sig_attempt, 0o777)
                (sig_attempt / "input.txt").write_text("sig\n")
                sig = launch(
                    binary, sig_attempt,
                    [str(binary), "probe", "--mode", "signal", "--result", str(sig_attempt / "result.json"),
                     "--attempt", str(sig_attempt), "--executor-pid", str(exec_sleeper.pid),
                     "--sibling-pid", str(sib_pid)],
                    ro_roots=ro_roots, protected=protected, timeout=15,
                )
                load_probe_checks(sig_attempt / "result.json", "signal")
                add(
                    "executor_sleeper_survived_signal_probe",
                    "PASS" if alive(exec_sleeper.pid) else "FAIL",
                    f"pid {exec_sleeper.pid} rc={sig.returncode}",
                )
                add(
                    "sibling_sleeper_survived_signal_probe",
                    "PASS" if alive(sib_pid) else "FAIL",
                    f"pid {sib_pid}",
                )
            flush("signals")

        if budget_left(40):
            cg_a = setup_cgroup("sec08-job-a")
            cg_b = setup_cgroup("sec08-job-b")
            if cg_a and cg_b:
                cgroups.extend([cg_a, cg_b])
                escape_attempt = WORK / "cgroup-escape"
                escape_attempt.mkdir()
                os.chmod(escape_attempt, 0o777)
                (escape_attempt / "input.txt").write_text("cg\n")
                escape_proc = launch(
                    binary, escape_attempt,
                    [str(binary), "probe", "--mode", "cgroup", "--result", str(escape_attempt / "result.json"),
                     "--attempt", str(escape_attempt), "--parent-procs", parent_procs,
                     "--cgroup-kill", f"{cg_a}/cgroup.kill"],
                    ro_roots=ro_roots, protected=protected, cgroup_procs=f"{cg_a}/cgroup.procs",
                    status=WORK / "cgroup-escape-status.txt", timeout=15,
                )
                if (WORK / "cgroup-escape-status.txt").exists():
                    IDENTITY["cgroup_membership_before_tool"] = (WORK / "cgroup-escape-status.txt").read_text()[-500:]
                IDENTITY["cgroup_layer_note"] = (
                    "cgroup files are outside the Landlock ruleset, so an EACCES from the tool "
                    "can be Landlock rather than the mode 0200 DAC on cgroup.kill; "
                    "root prepared that delegation, which is a deployment prerequisite"
                )
                TOOL_ACTIONS.append("uid 10003 inside sec08-job-a tried parent cgroup.procs and cgroup.kill")
                add("cgroup_escape_probe_ran", "PASS" if escape_proc.returncode == 0 else "FAIL",
                    f"rc={escape_proc.returncode} err={escape_proc.stderr[-200:]}")
                load_probe_checks(escape_attempt / "result.json", "cgroup")
                tree_a = WORK / "treeA"
                tree_b = WORK / "treeB"
                popens = []
                for tree, cg in ((tree_a, cg_a), (tree_b, cg_b)):
                    tree.mkdir()
                    os.chmod(tree, 0o777)
                    (tree / "input.txt").write_text("tree\n")
                    err = open(tree / "launcher.err", "w")
                    proc = subprocess.Popen(
                        [str(binary), "launch", "--attempt", str(tree), "--binary", str(binary),
                         "--cgroup-procs", f"{cg}/cgroup.procs", "--uid", str(TOOL_UID), "--gid", str(TOOL_UID),
                         "--control-dir", str(CONTROL),
                         *[arg for root in ro_roots for arg in ("--ro", root)],
                         *[arg for path in protected for arg in ("--protected", path)],
                         "--", str(binary), "probe", "--mode", "sleep", "--result", str(tree / "unused.json"),
                         "--attempt", str(tree)],
                        stdout=subprocess.DEVNULL, stderr=err,
                    )
                    procs.append(proc)
                    popens.append(proc)
                ready = (
                    wait_file(tree_a / "parent.pid", 8)
                    and wait_file(tree_a / "child.pid", 8)
                    and wait_file(tree_b / "parent.pid", 8)
                )
                add("cancel_trees_ready", "PASS" if ready else "FAIL",
                    f"A={(tree_a / 'launcher.err').read_text()[-180:] if (tree_a / 'launcher.err').exists() else ''}")
                if ready:
                    parent_pid = int((tree_a / "parent.pid").read_text().strip())
                    child_pid = int((tree_a / "child.pid").read_text().strip())
                    b_pid = int((tree_b / "parent.pid").read_text().strip())
                    before = (tree_b / "parent.hb").stat().st_size if (tree_b / "parent.hb").exists() else 0
                    status_dir = WORK / "executor-status"
                    status_dir.mkdir()
                    os.chmod(status_dir, 0o777)
                    status_path = status_dir / "cancel.txt"
                    cancel = run([
                        "setpriv", "--reuid", str(EXEC_UID), "--regid", str(EXEC_UID), "--clear-groups",
                        "--inh-caps=-all", "--ambient-caps=-all", "--bounding-set=-all",
                        str(binary), "cancel", f"{cg_a}/cgroup.kill", str(status_path),
                    ], timeout=10)
                    EXECUTOR_ACTIONS.append("uid 10002 with empty capabilities wrote sec08-job-a/cgroup.kill")
                    IDENTITY["cancel_status"] = status_path.read_text()[:800] if status_path.exists() else ""
                    IDENTITY["cancel_stderr"] = (cancel.stderr or "")[:400]
                    a_gone = died(parent_pid) and died(child_pid)
                    b_live = alive(b_pid)
                    grew = False
                    grow_deadline = time.monotonic() + 2
                    while time.monotonic() < grow_deadline:
                        after = (tree_b / "parent.hb").stat().st_size if (tree_b / "parent.hb").exists() else 0
                        if b_live and after > before:
                            grew = True
                            break
                        time.sleep(0.1)
                        b_live = alive(b_pid)
                    add("cancel_writer_uid_10002", "PASS" if cancel.returncode == 0 else "FAIL",
                        f"rc={cancel.returncode} status={IDENTITY['cancel_status'][:180]}")
                    add("cancel_kills_tree_a_including_reparented", "PASS" if a_gone else "FAIL",
                        f"parent {parent_pid} child {child_pid}")
                    add("cancel_leaves_tree_b", "PASS" if b_live and grew else "FAIL",
                        f"b_pid {b_pid} alive={b_live} hb_grew={grew}")
            flush("cgroup")

        if budget_left(50):
            def ns_add(name: str) -> bool:
                proc = run(["ip", "netns", "add", name], timeout=10)
                if proc.returncode != 0 and "File exists" not in proc.stderr:
                    add(f"netns_{name}", "FAIL", proc.stderr.strip()[:240])
                    return False
                netns.append(name)
                HOST_ACTIONS.append(f"root ip netns add {name}; DEPLOYMENT PREREQUISITE, not Compose policy")
                return True

            if all(ns_add(name) for name in ("sec08-offline", "sec08-proxy", "sec08-medianet", "sec08-internal")):
                run(["ip", "link", "add", "v-proxy", "type", "veth", "peer", "name", "v-media"], timeout=10)
                run(["ip", "link", "set", "v-proxy", "netns", "sec08-proxy"], timeout=10)
                run(["ip", "link", "set", "v-media", "netns", "sec08-medianet"], timeout=10)
                run(["ip", "netns", "exec", "sec08-proxy", "ip", "addr", "add", "10.88.0.2/30", "dev", "v-proxy"], timeout=10)
                run(["ip", "netns", "exec", "sec08-proxy", "ip", "link", "set", "v-proxy", "up"], timeout=10)
                run(["ip", "netns", "exec", "sec08-proxy", "ip", "link", "set", "lo", "up"], timeout=10)
                run(["ip", "netns", "exec", "sec08-medianet", "ip", "addr", "add", "10.88.0.1/30", "dev", "v-media"], timeout=10)
                run(["ip", "netns", "exec", "sec08-medianet", "ip", "link", "set", "v-media", "up"], timeout=10)
                run(["ip", "netns", "exec", "sec08-medianet", "ip", "link", "set", "lo", "up"], timeout=10)
                run(["ip", "link", "add", "v-host", "type", "veth", "peer", "name", "v-int"], timeout=10)
                run(["ip", "link", "set", "v-int", "netns", "sec08-internal"], timeout=10)
                run(["ip", "addr", "add", "10.77.0.1/30", "dev", "v-host"], timeout=10)
                run(["ip", "link", "set", "v-host", "up"], timeout=10)
                run(["ip", "netns", "exec", "sec08-internal", "ip", "addr", "add", "10.77.0.2/30", "dev", "v-int"], timeout=10)
                run(["ip", "netns", "exec", "sec08-internal", "ip", "link", "set", "v-int", "up"], timeout=10)
                run(["ip", "netns", "exec", "sec08-internal", "ip", "link", "set", "lo", "up"], timeout=10)
                run(["ip", "netns", "exec", "sec08-offline", "ip", "link", "set", "lo", "up"], timeout=10)
                proxy_ready = WORK / "proxy.ready"
                internal_ready = WORK / "internal.ready"
                host_ready = WORK / "host.ready"
                procs.append(start_listener("sec08-proxy", "10.88.0.2", 18080, proxy_ready))
                procs.append(start_listener("sec08-internal", "10.77.0.2", 18081, internal_ready))
                host_thread_ready = threading.Event()

                def host_listen() -> None:
                    server = socket.socket()
                    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    server.bind(("0.0.0.0", 18082))
                    server.listen(16)
                    host_ready.write_text("up")
                    host_thread_ready.set()
                    server.settimeout(0.5)
                    end = time.monotonic() + 25
                    while time.monotonic() < end and not STOP.is_set():
                        try:
                            conn, _ = server.accept()
                            conn.close()
                        except socket.timeout:
                            pass
                    server.close()

                threading.Thread(target=host_listen, daemon=True).start()
                wait_file(proxy_ready, 5)
                wait_file(internal_ready, 5)
                host_thread_ready.wait(5)
                hip = host_ip()
                IDENTITY["host_ip"] = hip
                targets_media = ["10.88.0.2:18080", "10.77.0.2:18081", "169.254.169.254:80"]
                if hip:
                    targets_media.append(f"{hip}:18082")
                targets_offline = ["10.88.0.2:18080", "10.77.0.2:18081", "127.0.0.1:18082", "169.254.169.254:80"]
                if hip:
                    targets_offline.append(f"{hip}:18082")

                def connect_check(ns: str, targets: list[str], name: str) -> dict:
                    attempt = WORK / name
                    attempt.mkdir(exist_ok=True)
                    os.chmod(attempt, 0o777)
                    (attempt / "input.txt").write_text("net\n")
                    argv = [str(binary), "probe", "--mode", "connect", "--result", str(attempt / "connect.json"),
                            "--attempt", str(attempt)]
                    for target in targets:
                        argv += ["--connect", target]
                    proc = launch(binary, attempt, argv, ro_roots=ro_roots, protected=protected, netns=ns, timeout=20)
                    path = attempt / "connect.json"
                    if proc.returncode != 0 or not path.exists():
                        add(f"{name}_connect_ran", "FAIL", f"rc={proc.returncode} err={proc.stderr[-240:]}")
                        return {}
                    data = json.loads(path.read_text())
                    return {item["target"]: item for item in data.get("connects", [])}

                media = connect_check("sec08-medianet", targets_media, "medianet")
                offline = connect_check("sec08-offline", targets_offline, "offline")
                TOOL_ACTIONS.append("uid 10003 connected from synthetic netns; not a Compose policy proof")
                host_to_internal = run(
                    ["python3", "-c", "import socket;s=socket.create_connection(('10.77.0.2',18081),2);s.close()"],
                    timeout=5,
                )
                add(
                    "internal_listener_reachable_from_supervisor",
                    "PASS" if host_to_internal.returncode == 0 else "FAIL",
                    (host_to_internal.stderr or host_to_internal.stdout)[-160:],
                )
                expect_connect(media, "10.88.0.2:18080", True, "medianet_reaches_proxy_stub")
                expect_connect(media, "10.77.0.2:18081", False, "medianet_denied_internal_listener")
                expect_connect(media, "169.254.169.254:80", False, "medianet_denied_metadata")
                if hip:
                    expect_connect(media, f"{hip}:18082", False, "medianet_denied_host_listener")
                expect_connect(offline, "10.88.0.2:18080", False, "offline_denied_proxy")
                expect_connect(offline, "10.77.0.2:18081", False, "offline_denied_internal")
                expect_connect(offline, "127.0.0.1:18082", False, "offline_denied_loopback_host_port")
                expect_connect(offline, "169.254.169.254:80", False, "offline_denied_metadata")
                if hip:
                    expect_connect(offline, f"{hip}:18082", False, "offline_denied_host_ip")
                for proc in list(procs):
                    args = proc.args if isinstance(proc.args, (list, tuple)) else []
                    if any("10.88.0.2" in str(arg) for arg in args):
                        stop_proc(proc)
                time.sleep(0.3)
                media_down = connect_check("sec08-medianet", ["10.88.0.2:18080"], "medianet-down")
                expect_connect(media_down, "10.88.0.2:18080", False, "medianet_fails_when_proxy_down")
                ff_attempt = WORK / "ffmpeg-offline"
                ff_attempt.mkdir()
                os.chmod(ff_attempt, 0o777)
                ff = launch(
                    binary, ff_attempt,
                    ["/usr/bin/ffmpeg", "-hide_banner", "-nostdin", "-y", "-f", "lavfi",
                     "-i", "sine=frequency=440:duration=0.2", "-ac", "1", "-ar", "16000",
                     str(ff_attempt / "tone.wav")],
                    ro_roots=ro_roots, protected=protected, netns="sec08-offline",
                    status=WORK / "ffmpeg-entrypoint.txt", timeout=30,
                )
                probe = launch(
                    binary, ff_attempt,
                    ["/usr/bin/ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=nw=1:nk=1", str(ff_attempt / "tone.wav")],
                    ro_roots=ro_roots, protected=protected, netns="sec08-offline", timeout=20,
                )
                wav_ok = (ff_attempt / "tone.wav").exists() and (ff_attempt / "tone.wav").stat().st_size > 0
                add("ffmpeg_offline_fixture", "PASS" if ff.returncode == 0 and wav_ok else "FAIL",
                    f"rc={ff.returncode} stderr={ff.stderr[-240:]}")
                add("ffprobe_offline_fixture", "PASS" if probe.returncode == 0 and probe.stdout.strip() else "FAIL",
                    f"rc={probe.returncode} out={probe.stdout.strip()[:80]} err={probe.stderr[-160:]}")
            flush("network")
    except Exception as exc:
        add("harness_exception", "FAIL", f"{type(exc).__name__}: {exc}"[:400])
    finally:
        STOP.set()
        for proc in procs:
            if not stop_proc(proc):
                cleanup_failed = True
                add("cleanup_process", "FAIL", "process did not die after SIGKILL")
        for tree in (WORK / "treeA", WORK / "treeB", WORK / "signal-sibling"):
            kill_pid_file(tree / "parent.pid")
            kill_pid_file(tree / "child.pid")
        sock_cleanup = locals().get("sock")
        if isinstance(sock_cleanup, socket.socket):
            sock_cleanup.close()
        abstract_cleanup = locals().get("abstract")
        if isinstance(abstract_cleanup, socket.socket):
            abstract_cleanup.close()
        for name in netns:
            deleted = run(["ip", "netns", "del", name], timeout=10)
            if deleted.returncode not in (0, 124) and "No such file" not in deleted.stderr:
                cleanup_failed = True
        for link in host_links:
            run(["ip", "link", "del", link], timeout=5)
        for path in cgroups:
            removed = run(["rmdir", path], timeout=5)
            if removed.returncode != 0:
                cleanup_failed = True
        for name in created_users:
            run(["userdel", name], timeout=10)
            run(["groupdel", name], timeout=10)
        for path in (WORK, TRUSTED, CONTROL):
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
        if cleanup_failed:
            add("cleanup_incomplete", "FAIL", "a cleanup step timed out or failed; not continuing")
        files = {}
        for rel in (
            ".github/workflows/sec08-native-feasibility.yml",
            "tests/sec08_feasibility/manifest.json",
            "tests/sec08_feasibility/harness.py",
            "tests/sec08_feasibility/sec08tool.c",
        ):
            blob = (ROOT / rel).read_bytes()
            files[rel] = hashlib.sha256(blob).hexdigest()
        IDENTITY["source_sha256"] = files
        ensure_required(REQUIRED)
        digest = flush("final", partial=False)
        print(json.dumps({"final_sha256": digest, "stages": STAGE_HASHES, **report_body("final", partial=False)["summary"]}, indent=2))
    return 1 if any(c["status"] != "PASS" for c in CHECKS) else 0


def self_check() -> int:
    assert path_covers("/usr", "/usr/bin/ffmpeg")
    assert not path_covers("/usr", "/var/tmp/sec08-feasibility-work")
    assert not path_covers("/opt/sec08-trusted/bin", "/opt/sec08-control")
    assert path_covers("/opt", "/opt/sec08-control")
    assert path_covers("/var/tmp/sec08-feasibility-work", "/var/tmp/sec08-feasibility-work/jobA")
    assert denial_class(True, 0) == "success"
    assert denial_class(False, 13) == "permission"
    assert denial_class(False, 1) == "permission"
    assert denial_class(False, 110) == "timeout"
    assert denial_class(False, 111) == "other"
    print("self-check ok")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-check":
        sys.exit(self_check())
    sys.exit(main())
