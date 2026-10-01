#!/usr/bin/env python3
"""Supervisor for one SEC-08 mechanism-C native feasibility run.

The process starts as uid 0 only because the container entrypoint does.
It is not the tool. The tool transition is performed by sec08tool launch,
which keeps CAP_SETUID, CAP_SETGID and CAP_SETPCAP, then setuids to 10003.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import platform
import pwd
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

MIN_ABI = 3
EXEC_UID = 10002
TOOL_UID = 10003
CANARY = "EXECUTOR_CANARY_SYNTHETIC"
ROOT = Path(__file__).resolve().parents[2]
SRC = Path(__file__).resolve().parent
OUT = ROOT / "sec08-feasibility-out"
WORK = Path("/tmp/sec08-feasibility-work")
CHECKS: list[dict] = []


def add(name: str, status: str, detail: str = "") -> None:
    CHECKS.append({"name": name, "status": status, "detail": detail})
    print(f"{status} {name} {detail}", flush=True)


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, check=False, **kwargs)


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
        add(
            f"uid_{uid}_available",
            "FAIL",
            f"uid {uid} is already {existing.pw_name}; refusing to reuse it",
        )
        return
    if existing:
        add(f"uid_{uid}_available", "PASS", f"existing test user {name}")
        return
    group = run(["groupadd", "-g", str(uid), name])
    user = run(
        ["useradd", "-u", str(uid), "-g", str(uid), "-M", "-N", "-s", "/usr/sbin/nologin", name]
    )
    if group.returncode != 0 or user.returncode != 0:
        add(
            f"uid_{uid}_available",
            "FAIL",
            (group.stderr + user.stderr).strip()[:400],
        )
        return
    created.append(name)
    add(f"uid_{uid}_available", "PASS", f"created {name}")


def compile_tool(binary: Path) -> bool:
    proc = run(["gcc", "-O2", "-Wall", "-Wextra", "-o", str(binary), str(SRC / "sec08tool.c")])
    if proc.returncode != 0:
        add("compile_sec08tool", "FAIL", (proc.stderr or proc.stdout)[-800:])
        return False
    add("compile_sec08tool", "PASS", str(binary))
    return True


def launch(
    binary: Path,
    attempt: Path,
    argv: list[str],
    *,
    cgroup_procs: str | None = None,
    status: Path | None = None,
    force_abi: bool = False,
    force_ruleset: bool = False,
    pass_fds: tuple[int, ...] = (),
    netns: str | None = None,
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
    ]
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
    return subprocess.run(cmd, text=True, capture_output=True, env=env, pass_fds=pass_fds, check=False)


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
    data = json.loads(path.read_text())
    for item in data.get("checks", []):
        status = "PASS" if item.get("pass") is True else "FAIL"
        add(f"{prefix}_{item.get('name')}", status, str(item.get("detail", "")))


def wait_file(path: Path, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
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
    procs = path / "cgroup.procs"
    os.chmod(procs, 0o644)
    add(f"cgroup_{name}_delegated", "PASS", f"{kill} owner uid {EXEC_UID}")
    return str(path)


def host_ip() -> str:
    proc = run(["ip", "-4", "route", "get", "1.1.1.1"])
    for token in proc.stdout.split():
        if token == "src":
            continue
    parts = proc.stdout.split()
    if "src" in parts:
        return parts[parts.index("src") + 1]
    return ""


def start_listener(ns: str | None, host: str, port: int, ready: Path) -> subprocess.Popen[str]:
    code = (
        "import socket,sys\n"
        "s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "s.bind((sys.argv[1], int(sys.argv[2]))); s.listen(5)\n"
        "open(sys.argv[3],'w').write('up')\n"
        "s.settimeout(1)\n"
        "import time\n"
        "end=time.time()+40\n"
        "while time.time()<end:\n"
        "  try:\n"
        "    c,a=s.accept(); c.close()\n"
        "  except socket.timeout:\n"
        "    pass\n"
    )
    cmd = ["python3", "-c", code, host, str(port), str(ready)]
    if ns:
        cmd = ["ip", "netns", "exec", ns, *cmd]
    return subprocess.Popen(cmd, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def main() -> int:
    if platform.system() != "Linux" or os.uname().machine != "x86_64":
        print("this harness runs only on native Linux x86_64", file=sys.stderr)
        return 2
    if os.geteuid() != 0:
        print("supervisor must start as uid 0 (container-entrypoint stand-in)", file=sys.stderr)
        return 2
    OUT.mkdir(parents=True, exist_ok=True)
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    created_users: list[str] = []
    netns: list[str] = []
    cgroups: list[str] = []
    procs: list[subprocess.Popen[str]] = []
    identity: dict = {
        "uname": " ".join(os.uname()),
        "machine": os.uname().machine,
        "os_release": Path("/etc/os-release").read_text().splitlines()[:6],
        "supervisor_status": read_status_bits(),
        "mounts": Path("/proc/self/mounts").read_text().splitlines()[:40],
        "min_abi": MIN_ABI,
        "min_abi_reason": "REFER (ABI 2) and TRUNCATE (ABI 3) are both required",
        "entrypoint_caps": ["CAP_SETGID", "CAP_SETUID", "CAP_SETPCAP"],
        "executor_uid": EXEC_UID,
        "tool_uid": TOOL_UID,
    }
    try:
        try:
            abi = landlock_abi()
        except OSError as exc:
            add("landlock_abi_query", "FAIL", f"{exc}")
            abi = -1
        identity["landlock_abi"] = abi
        if abi < MIN_ABI:
            add("landlock_abi_meets_minimum", "FAIL", f"abi {abi} < {MIN_ABI}")
        else:
            add("landlock_abi_meets_minimum", "PASS", f"abi {abi}")

        ensure_user(EXEC_UID, "sec08exec", created_users)
        ensure_user(TOOL_UID, "sec08tool", created_users)
        binary = WORK / "sec08tool"
        if not compile_tool(binary):
            return 1

        job_a = WORK / "jobA"
        job_b = WORK / "jobB"
        published = WORK / "published" / "artifact.canary"
        sibling_a = job_b / "canary.txt"
        sibling_b = job_a / "canary-for-b.txt"
        prepare_attempt(job_a, sibling_a, published)
        (job_b).mkdir(parents=True, exist_ok=True)
        os.chmod(job_b, 0o777)
        (job_b / "input.txt").write_text("attempt-b-input\n")
        os.chown(job_b / "input.txt", TOOL_UID, TOOL_UID)
        os.chmod(job_b / "input.txt", 0o666)
        sibling_b.write_text("SIBLING_B_CANARY\n")
        os.chown(sibling_b, TOOL_UID, TOOL_UID)
        os.chmod(sibling_b, 0o666)
        escape_a = job_a / "escape"
        escape_b = job_b / "escape"
        if escape_a.exists() or escape_a.is_symlink():
            escape_a.unlink()
        if escape_b.exists() or escape_b.is_symlink():
            escape_b.unlink()
        escape_a.symlink_to(sibling_a)
        escape_b.symlink_to(published)
        regain = job_a / "regain"
        shutil.copy(binary, regain)
        os.chown(regain, 0, 0)
        os.chmod(regain, 0o4755)

        sock_dir = WORK / "control"
        sock_dir.mkdir()
        os.chmod(sock_dir, 0o755)
        sock_path = sock_dir / "worker.sock"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(sock_path))
        sock.listen(1)
        os.chmod(sock_path, 0o777)

        # Forced failures must not exec the tool.
        for label, flag in (("force_abi", "abi"), ("force_ruleset", "ruleset")):
            sentinel = job_a / f"{label}_started"
            if (job_a / "tool_started").exists():
                (job_a / "tool_started").unlink()
            proc = launch(
                binary,
                job_a,
                [str(binary), "probe", "--mode", "full", "--result", str(job_a / f"{label}.json"),
                 "--attempt", str(job_a)],
                status=WORK / f"{label}-status.txt",
                force_abi=flag == "abi",
                force_ruleset=flag == "ruleset",
            )
            started = (job_a / "tool_started").exists()
            add(
                f"{label}_does_not_exec",
                "PASS" if proc.returncode != 0 and not started else "FAIL",
                f"rc={proc.returncode} started={started} stderr={proc.stderr[-240:]}",
            )

        parent_procs = "/sys/fs/cgroup/cgroup.procs"
        def run_job_a() -> subprocess.CompletedProcess[str]:
            return launch(
                binary,
                job_a,
                [str(binary), "probe", "--mode", "full", "--result", str(job_a / "result.json"),
                 "--attempt", str(job_a), "--symlink", str(escape_a),
                 "--sibling", str(sibling_a), "--published", str(published),
                 "--socket", str(sock_path), "--parent-procs", parent_procs,
                 "--rename-dst", str(WORK / "published" / "renamed-from-a"),
                 "--link-dst", str(WORK / "published" / "linked-from-a"),
                 "--regain-bin", str(regain)],
                status=WORK / "entrypoint-a.txt",
                pass_fds=(sock.fileno(),),
            )

        def run_job_b() -> subprocess.CompletedProcess[str]:
            return launch(
                binary,
                job_b,
                [str(binary), "probe", "--mode", "full", "--result", str(job_b / "result.json"),
                 "--attempt", str(job_b), "--symlink", str(escape_b),
                 "--sibling", str(sibling_b), "--published", str(published),
                 "--socket", str(sock_path), "--parent-procs", parent_procs,
                 "--rename-dst", str(WORK / "published" / "renamed-from-b"),
                 "--link-dst", str(WORK / "published" / "linked-from-b")],
                status=WORK / "entrypoint-b.txt",
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            fut_a = pool.submit(run_job_a)
            fut_b = pool.submit(run_job_b)
            proc_a = fut_a.result()
            proc_b = fut_b.result()
        add(
            "concurrent_fs_pair_started",
            "PASS" if proc_a.returncode == 0 and proc_b.returncode == 0 else "FAIL",
            f"A rc={proc_a.returncode} B rc={proc_b.returncode} Aerr={proc_a.stderr[-180:]} Berr={proc_b.stderr[-180:]}",
        )
        load_probe_checks(job_a / "result.json", "jobA")
        load_probe_checks(job_b / "result.json", "jobB")
        if (WORK / "entrypoint-a.txt").exists():
            identity["entrypoint_status"] = (WORK / "entrypoint-a.txt").read_text()[:1200]

        # Same-uid signal and executor signal, while both targets are alive.
        exec_sleeper = subprocess.Popen(
            ["setpriv", "--reuid", str(EXEC_UID), "--regid", str(EXEC_UID), "--clear-groups",
             "--inh-caps=-all", "--ambient-caps=-all", "sleep", "60"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        procs.append(exec_sleeper)
        sib_attempt = WORK / "signal-sibling"
        sib_attempt.mkdir()
        os.chmod(sib_attempt, 0o777)
        (sib_attempt / "input.txt").write_text("x\n")
        sib_proc = subprocess.Popen(
            [str(binary), "launch", "--attempt", str(sib_attempt), "--binary", str(binary),
             "--uid", str(TOOL_UID), "--gid", str(TOOL_UID), "--",
             str(binary), "probe", "--mode", "sleep", "--result", str(sib_attempt / "unused.json"),
             "--attempt", str(sib_attempt)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            env={**os.environ, "EXECUTOR_CANARY": CANARY},
        )
        procs.append(sib_proc)
        sibling_ready = wait_file(sib_attempt / "parent.pid")
        add("signal_sibling_ready", "PASS" if sibling_ready else "FAIL", "parent.pid")
        if sibling_ready and exec_sleeper.poll() is None:
            sib_pid = int((sib_attempt / "parent.pid").read_text().strip())
            sig_attempt = WORK / "signal-probe"
            prepare_attempt(sig_attempt, WORK / "signal-canary.txt", WORK / "published" / "signal.canary")
            escape_sig = sig_attempt / "escape"
            if escape_sig.exists() or escape_sig.is_symlink():
                escape_sig.unlink()
            escape_sig.symlink_to(WORK / "signal-canary.txt")
            sig = launch(
                binary,
                sig_attempt,
                [str(binary), "probe", "--mode", "full", "--result", str(sig_attempt / "result.json"),
                 "--attempt", str(sig_attempt), "--symlink", str(sig_attempt / "escape"),
                 "--sibling", str(WORK / "signal-canary.txt"), "--published", str(published),
                 "--socket", str(sock_path), "--executor-pid", str(exec_sleeper.pid),
                 "--sibling-pid", str(sib_pid),
                 "--rename-dst", str(WORK / "published" / "sig-rename"),
                 "--link-dst", str(WORK / "published" / "sig-link")],
            )
            load_probe_checks(sig_attempt / "result.json", "signal")
            time.sleep(0.2)
            add(
                "executor_sleeper_survived_signal_probe",
                "PASS" if alive(exec_sleeper.pid) else "FAIL",
                f"pid {exec_sleeper.pid}",
            )
            add(
                "sibling_sleeper_survived_signal_probe",
                "PASS" if alive(sib_pid) else "FAIL",
                f"pid {sib_pid} launcher_rc={sig.returncode}",
            )
        else:
            add("signal_probe", "NOT RUN", "targets were not alive")

        # Cancellation: per-job cgroup, writer is uid 10002 with empty caps.
        cg_a = setup_cgroup("sec08-job-a")
        cg_b = setup_cgroup("sec08-job-b")
        if cg_a and cg_b:
            cgroups.extend([cg_a, cg_b])
            tree_a = WORK / "treeA"
            tree_b = WORK / "treeB"
            for tree in (tree_a, tree_b):
                tree.mkdir()
                os.chmod(tree, 0o777)
                (tree / "input.txt").write_text("tree\n")
            pa = subprocess.Popen(
                [str(binary), "launch", "--attempt", str(tree_a), "--binary", str(binary),
                 "--cgroup-procs", f"{cg_a}/cgroup.procs", "--uid", str(TOOL_UID), "--gid", str(TOOL_UID),
                 "--", str(binary), "probe", "--mode", "sleep", "--result", str(tree_a / "unused.json"),
                 "--attempt", str(tree_a)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            )
            pb = subprocess.Popen(
                [str(binary), "launch", "--attempt", str(tree_b), "--binary", str(binary),
                 "--cgroup-procs", f"{cg_b}/cgroup.procs", "--uid", str(TOOL_UID), "--gid", str(TOOL_UID),
                 "--", str(binary), "probe", "--mode", "sleep", "--result", str(tree_b / "unused.json"),
                 "--attempt", str(tree_b)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            )
            procs.extend([pa, pb])
            ready = wait_file(tree_a / "parent.pid") and wait_file(tree_a / "child.pid") and wait_file(tree_b / "parent.pid")
            add("cancel_trees_ready", "PASS" if ready else "FAIL",
                f"Aerr={(pa.stderr.read() if pa.poll() is not None else '')[-200:]}")
            if ready:
                parent_pid = int((tree_a / "parent.pid").read_text().strip())
                child_pid = int((tree_a / "child.pid").read_text().strip())
                b_pid = int((tree_b / "parent.pid").read_text().strip())
                before = (tree_b / "parent.hb").stat().st_size if (tree_b / "parent.hb").exists() else 0
                status_path = WORK / "cancel-as-10002.txt"
                cancel = run([
                    "setpriv", "--reuid", str(EXEC_UID), "--regid", str(EXEC_UID), "--clear-groups",
                    "--inh-caps=-all", "--ambient-caps=-all", "--bounding-set=-all",
                    str(binary), "cancel", f"{cg_a}/cgroup.kill", str(status_path),
                ])
                identity["cancel_status"] = status_path.read_text()[:800] if status_path.exists() else ""
                identity["cancel_stderr"] = (cancel.stderr or "")[:400]
                time.sleep(0.6)
                a_gone = (not alive(parent_pid)) and (not alive(child_pid))
                b_live = alive(b_pid)
                after = (tree_b / "parent.hb").stat().st_size if (tree_b / "parent.hb").exists() else 0
                add("cancel_writer_uid_10002", "PASS" if cancel.returncode == 0 else "FAIL",
                    f"rc={cancel.returncode} status={identity['cancel_status'][:180]}")
                add("cancel_kills_tree_a_including_reparented", "PASS" if a_gone else "FAIL",
                    f"parent {parent_pid} child {child_pid}")
                add("cancel_leaves_tree_b", "PASS" if b_live and after >= before else "FAIL",
                    f"b_pid {b_pid} hb {before}->{after}")
            else:
                add("cancel_kills_tree_a_including_reparented", "NOT RUN", "trees did not become ready")
                add("cancel_leaves_tree_b", "NOT RUN", "trees did not become ready")
        else:
            add("cancel_kills_tree_a_including_reparented", "NOT RUN", "cgroup was not created")
            add("cancel_leaves_tree_b", "NOT RUN", "cgroup was not created")

        # Network fixture. Supervisor root builds the namespaces; the tool does not.
        def ns_add(name: str) -> bool:
            proc = run(["ip", "netns", "add", name])
            if proc.returncode != 0 and "File exists" not in proc.stderr:
                add(f"netns_{name}", "FAIL", proc.stderr.strip()[:240])
                return False
            netns.append(name)
            return True

        offline_ok = ns_add("sec08-offline")
        proxy_ok = ns_add("sec08-proxy")
        media_ok = ns_add("sec08-medianet")
        internal_ok = ns_add("sec08-internal")
        if all((offline_ok, proxy_ok, media_ok, internal_ok)):
            run(["ip", "link", "add", "v-proxy", "type", "veth", "peer", "name", "v-media"])
            run(["ip", "link", "set", "v-proxy", "netns", "sec08-proxy"])
            run(["ip", "link", "set", "v-media", "netns", "sec08-medianet"])
            run(["ip", "netns", "exec", "sec08-proxy", "ip", "addr", "add", "10.88.0.2/30", "dev", "v-proxy"])
            run(["ip", "netns", "exec", "sec08-proxy", "ip", "link", "set", "v-proxy", "up"])
            run(["ip", "netns", "exec", "sec08-proxy", "ip", "link", "set", "lo", "up"])
            run(["ip", "netns", "exec", "sec08-medianet", "ip", "addr", "add", "10.88.0.1/30", "dev", "v-media"])
            run(["ip", "netns", "exec", "sec08-medianet", "ip", "link", "set", "v-media", "up"])
            run(["ip", "netns", "exec", "sec08-medianet", "ip", "link", "set", "lo", "up"])
            run(["ip", "link", "add", "v-host", "type", "veth", "peer", "name", "v-int"])
            run(["ip", "link", "set", "v-int", "netns", "sec08-internal"])
            run(["ip", "addr", "add", "10.77.0.1/30", "dev", "v-host"])
            run(["ip", "link", "set", "v-host", "up"])
            run(["ip", "netns", "exec", "sec08-internal", "ip", "addr", "add", "10.77.0.2/30", "dev", "v-int"])
            run(["ip", "netns", "exec", "sec08-internal", "ip", "link", "set", "v-int", "up"])
            run(["ip", "netns", "exec", "sec08-internal", "ip", "link", "set", "lo", "up"])
            run(["ip", "netns", "exec", "sec08-offline", "ip", "link", "set", "lo", "up"])
            proxy_ready = WORK / "proxy.ready"
            internal_ready = WORK / "internal.ready"
            host_ready = WORK / "host.ready"
            procs.append(start_listener("sec08-proxy", "10.88.0.2", 18080, proxy_ready))
            procs.append(start_listener("sec08-internal", "10.77.0.2", 18081, internal_ready))
            host_thread_ready = threading.Event()

            def host_listen() -> None:
                s = socket.socket()
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("0.0.0.0", 18082))
                s.listen(5)
                host_ready.write_text("up")
                host_thread_ready.set()
                s.settimeout(1)
                end = time.time() + 40
                while time.time() < end:
                    try:
                        c, _ = s.accept()
                        c.close()
                    except socket.timeout:
                        pass
                s.close()

            threading.Thread(target=host_listen, daemon=True).start()
            wait_file(proxy_ready)
            wait_file(internal_ready)
            host_thread_ready.wait(5)
            hip = host_ip()
            identity["host_ip"] = hip
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
                argv = [str(binary), "probe", "--mode", "connect", "--result", str(attempt / "connect.json"),
                        "--attempt", str(attempt)]
                for target in targets:
                    argv += ["--connect", target]
                proc = launch(binary, attempt, argv, netns=ns)
                path = attempt / "connect.json"
                if proc.returncode != 0 or not path.exists():
                    add(f"{name}_connect_ran", "FAIL", f"rc={proc.returncode} err={proc.stderr[-240:]}")
                    return {}
                data = json.loads(path.read_text())
                return {item["target"]: item for item in data.get("connects", [])}

            media = connect_check("sec08-medianet", targets_media, "medianet")
            offline = connect_check("sec08-offline", targets_offline, "offline")
            host_to_internal = run(["python3", "-c",
                                    "import socket;s=socket.create_connection(('10.77.0.2',18081),1);s.close()"])
            add("internal_listener_reachable_from_supervisor",
                "PASS" if host_to_internal.returncode == 0 else "FAIL",
                host_to_internal.stderr[-160:])

            def expect(table: dict, target: str, ok: bool, label: str) -> None:
                item = table.get(target)
                if not item:
                    add(label, "FAIL", f"missing result for {target}")
                    return
                add(label, "PASS" if bool(item.get("ok")) is ok else "FAIL",
                    f"{target} ok={item.get('ok')} errno={item.get('errno')}")

            expect(media, "10.88.0.2:18080", True, "medianet_reaches_proxy_stub")
            expect(media, "10.77.0.2:18081", False, "medianet_denied_internal_listener")
            expect(media, "169.254.169.254:80", False, "medianet_denied_metadata")
            if hip:
                expect(media, f"{hip}:18082", False, "medianet_denied_host_listener")
            expect(offline, "10.88.0.2:18080", False, "offline_denied_proxy")
            expect(offline, "10.77.0.2:18081", False, "offline_denied_internal")
            expect(offline, "127.0.0.1:18082", False, "offline_denied_loopback_host_port")
            expect(offline, "169.254.169.254:80", False, "offline_denied_metadata")
            if hip:
                expect(offline, f"{hip}:18082", False, "offline_denied_host_ip")

            # Proxy down: the stub process is killed and the same target must fail.
            for proc in list(procs):
                if proc.args and "10.88.0.2" in proc.args:
                    proc.terminate()
            time.sleep(0.3)
            media_down = connect_check("sec08-medianet", ["10.88.0.2:18080"], "medianet-down")
            expect(media_down, "10.88.0.2:18080", False, "medianet_fails_when_proxy_down")

            ff_attempt = WORK / "ffmpeg-offline"
            ff_attempt.mkdir()
            os.chmod(ff_attempt, 0o777)
            ff = launch(
                binary,
                ff_attempt,
                ["/usr/bin/ffmpeg", "-hide_banner", "-nostdin", "-y", "-f", "lavfi",
                 "-i", "sine=frequency=440:duration=0.2", "-ac", "1", "-ar", "16000",
                 str(ff_attempt / "tone.wav")],
                netns="sec08-offline",
                status=WORK / "ffmpeg-entrypoint.txt",
            )
            probe = launch(
                binary,
                ff_attempt,
                ["/usr/bin/ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", str(ff_attempt / "tone.wav")],
                netns="sec08-offline",
            )
            wav_ok = (ff_attempt / "tone.wav").exists() and (ff_attempt / "tone.wav").stat().st_size > 0
            add("ffmpeg_offline_fixture", "PASS" if ff.returncode == 0 and wav_ok else "FAIL",
                f"rc={ff.returncode} stderr={ff.stderr[-240:]}")
            add("ffprobe_offline_fixture", "PASS" if probe.returncode == 0 and probe.stdout.strip() else "FAIL",
                f"rc={probe.returncode} out={probe.stdout.strip()[:80]} err={probe.stderr[-160:]}")
        else:
            for name in (
                "medianet_reaches_proxy_stub",
                "medianet_denied_internal_listener",
                "offline_denied_proxy",
                "ffmpeg_offline_fixture",
                "ffprobe_offline_fixture",
            ):
                add(name, "NOT RUN", "netns setup failed")
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
        sock_cleanup = locals().get("sock")
        if sock_cleanup:
            sock_cleanup.close()
        for name in netns:
            run(["ip", "netns", "del", name])
        for path in cgroups:
            run(["rmdir", path])
        for name in created_users:
            run(["userdel", name])
            run(["groupdel", name])
        files = {}
        for rel in (
            ".github/workflows/sec08-native-feasibility.yml",
            "tests/sec08_feasibility/manifest.json",
            "tests/sec08_feasibility/harness.py",
            "tests/sec08_feasibility/sec08tool.c",
        ):
            blob = (ROOT / rel).read_bytes()
            files[rel] = hashlib.sha256(blob).hexdigest()
        fails = [c for c in CHECKS if c["status"] == "FAIL"]
        not_run = [c for c in CHECKS if c["status"] == "NOT RUN"]
        passed = [c for c in CHECKS if c["status"] == "PASS"]
        report = {
            "mechanism": "C",
            "min_landlock_abi": MIN_ABI,
            "identity": identity,
            "source_sha256": files,
            "checks": CHECKS,
            "summary": {
                "pass": len(passed),
                "fail": len(fails),
                "not_run": len(not_run),
                "fail_names": [c["name"] for c in fails],
                "not_run_names": [c["name"] for c in not_run],
            },
        }
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "result.json").write_text(json.dumps(report, indent=2) + "\n")
        os.chmod(OUT / "result.json", 0o644)
        print(json.dumps(report["summary"], indent=2))
    return 1 if any(c["status"] != "PASS" for c in CHECKS) else 0


if __name__ == "__main__":
    sys.exit(main())
