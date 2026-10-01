"""In-container supervisor for the SEC-08 cgroup proof.

Pid 1 inside the executor container. It creates per-job cgroups, joins the
launcher before the tool exec, writes cgroup.kill itself, and reaps. It does
not call chown.
"""

from __future__ import annotations

import ctypes
import json
import os
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cancel_oracle import classify_pid  # noqa: E402
from cgroup_contract import private_cgroup_mount  # noqa: E402
from workspace_contract import WORK_DIR_MODE, mkdir_visible_mode  # noqa: E402

BIN = "/opt/sec08-trusted/bin/sec08tool"
WORK = Path("/var/tmp/sec08-proof-work")
CONTROL = Path("/run/fetchnow-executor")
SOCKET_PATH = CONTROL / "worker.sock"
CG = Path("/sys/fs/cgroup")
OUT = Path(os.environ.get("PROOF_OUT", "/out"))
ABSTRACT = "sec08-outside"
PR_SET_CHILD_SUBREAPER = 36
UID_WORKER = 10001
UID_EXEC = 10002
UID_TOOL = 10003


def atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(temp, path)


def cap_eff() -> str:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("CapEff:"):
            return line.split(":", 1)[1].strip()
    return ""


def read_stat(pid: int) -> tuple[str | None, str | None]:
    path = Path(f"/proc/{pid}/stat")
    if not path.exists():
        return None, "absent"
    try:
        return path.read_text(), None
    except OSError as exc:
        return None, exc.strerror or "read error"


def observe(pid: int, starttime: int | None = None) -> dict:
    text, error = read_stat(pid)
    item = classify_pid(text, expected_starttime=starttime, read_error=error)
    item["pid"] = pid
    cgroup_path = Path(f"/proc/{pid}/cgroup")
    if cgroup_path.exists():
        try:
            item["cgroup"] = cgroup_path.read_text().strip()
        except OSError as exc:
            item["cgroup_error"] = exc.strerror
    return item


def creds() -> dict:
    return {"euid": os.geteuid(), "egid": os.getegid(), "groups": sorted(os.getgroups())}


def chain_stat(path: Path) -> list[dict]:
    rows = []
    current = path
    for _ in range(8):
        try:
            st = current.stat()
        except OSError as exc:
            rows.append({"path": str(current), "errno": exc.errno})
            break
        rows.append(
            {
                "path": str(current),
                "uid": st.st_uid,
                "gid": st.st_gid,
                "mode": oct(stat.S_IMODE(st.st_mode)),
            }
        )
        if current.parent == current:
            break
        current = current.parent
    return rows


@contextmanager
def as_worker_group() -> Iterator[None]:
    """Read the worker-group workspace. uid 0 does not override DAC."""
    os.setresgid(UID_WORKER, UID_WORKER, 0)
    try:
        yield
    finally:
        os.setresgid(0, 0, 0)


def launcher_credentials() -> None:
    """Group 10001 lets the uid-0 launcher traverse the attempt directory."""
    os.umask(0o027)
    os.setgroups([])
    os.setresgid(UID_WORKER, UID_WORKER, UID_WORKER)


def mountinfo_cgroup() -> str:
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        if "cgroup" in line:
            return line
    return ""


def cgroup_fds(mountinfo: str) -> list[int]:
    """Record inherited cgroup descriptors before opening any proof cgroups."""
    mount_ids = {
        line.split()[0]
        for line in mountinfo.splitlines()
        if " - cgroup2 " in line or " - cgroup " in line
    }
    found = []
    for path in Path("/proc/self/fdinfo").iterdir():
        try:
            text = path.read_text()
        except FileNotFoundError:
            # The descriptor used to enumerate this directory has closed.
            continue
        for line in text.splitlines():
            if line.startswith("mnt_id:") and line.split(":", 1)[1].strip() in mount_ids:
                found.append(int(path.name))
    return sorted(found)


def run_as(uid: int, gid: int, fn, timeout: float = 5) -> tuple[int, str]:
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            os.setgroups([])
            os.setresgid(gid, gid, gid)
            os.setresuid(uid, uid, uid)
            text = fn() or ""
            os.write(write_fd, text.encode())
            os._exit(0)
        except Exception as exc:  # noqa: BLE001 - report the child failure
            os.write(write_fd, f"ERR {exc}\n".encode())
            os._exit(1)
    os.close(write_fd)
    os.set_blocking(read_fd, False)
    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    status = None
    while time.monotonic() < deadline:
        try:
            piece = os.read(read_fd, 4096)
        except BlockingIOError:
            piece = b""
        if piece:
            chunks.append(piece)
        waited, status = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            break
        time.sleep(0.05)
    else:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
        os.close(read_fd)
        return 124, b"".join(chunks).decode(errors="replace") or "timeout"
    while True:
        try:
            piece = os.read(read_fd, 4096)
        except BlockingIOError:
            break
        if not piece:
            break
        chunks.append(piece)
    os.close(read_fd)
    if status is None:
        _, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    return code, b"".join(chunks).decode(errors="replace")


def prepare_socket_dir() -> dict:
    old = os.umask(0)
    try:
        os.setresgid(UID_WORKER, UID_WORKER, 0)
        CONTROL.mkdir(mode=0o770)
    finally:
        os.setresgid(0, 0, 0)
        os.umask(old)
    st = CONTROL.stat()
    return {"uid": st.st_uid, "gid": st.st_gid, "mode": oct(stat.S_IMODE(st.st_mode))}


def start_listener() -> int:
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            os.setgroups([])
            os.setresgid(UID_WORKER, UID_WORKER, UID_WORKER)
            os.setresuid(UID_EXEC, UID_EXEC, UID_EXEC)
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(str(SOCKET_PATH))
            os.chmod(SOCKET_PATH, 0o660)
            sock.listen(8)
            os.write(write_fd, b"ready")
            os.close(write_fd)
            while True:
                conn, _ = sock.accept()
                conn.close()
        except Exception as exc:  # noqa: BLE001
            try:
                os.write(write_fd, f"ERR {exc}\n".encode())
            except OSError:
                pass
            os._exit(1)
    os.close(write_fd)
    os.set_blocking(read_fd, False)
    deadline = time.monotonic() + 3
    ready = b""
    while time.monotonic() < deadline and ready == b"":
        try:
            piece = os.read(read_fd, 64)
        except BlockingIOError:
            piece = b""
        if piece:
            ready += piece
            break
        time.sleep(0.05)
    os.close(read_fd)
    if ready != b"ready":
        os.kill(pid, 9)
        os.waitpid(pid, 0)
        raise RuntimeError(f"listener failed: {ready!r}")
    return pid


def connect_as(uid: int, gid: int) -> str:
    def attempt() -> str:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(1)
        try:
            sock.connect(str(SOCKET_PATH))
        except OSError as exc:
            return f"errno {exc.errno}"
        finally:
            sock.close()
        return "ok"

    code, text = run_as(uid, gid, attempt)
    return text.strip() if code == 0 else f"child {code} {text.strip()}"


def make_setgid_dir(path: Path) -> dict:
    """mkdir(2) stores only mode & 0777. The owner chmod restores setgid."""
    requested = WORK_DIR_MODE
    path.mkdir(mode=requested)
    after_mkdir = stat.S_IMODE(path.stat().st_mode)
    if after_mkdir != requested:
        os.chmod(path, requested)
    proven = stat.S_IMODE(path.stat().st_mode)
    if proven != requested:
        raise RuntimeError(
            f"{path} requested {oct(requested)} mkdir {oct(after_mkdir)} proven {oct(proven)}"
        )
    return {
        "path": str(path),
        "requested": oct(requested),
        "after_mkdir": oct(after_mkdir),
        "proven": oct(proven),
        "mkdir_masked": oct(mkdir_visible_mode(requested)),
    }


def prepare_work() -> dict:
    def create() -> str:
        os.umask(0)
        modes = [make_setgid_dir(WORK)]
        for name in ("a", "b", "probe", "published"):
            modes.append(make_setgid_dir(WORK / name))
        (WORK / "a" / "input.txt").write_text("a\n")
        (WORK / "b" / "input.txt").write_text("b\n")
        (WORK / "probe" / "input.txt").write_text("probe\n")
        canary = WORK / "b" / "canary"
        canary.write_text("canary\n")
        os.chmod(canary, 0o666)
        published = WORK / "published" / "canary"
        published.write_text("published\n")
        os.chmod(published, 0o666)
        link = WORK / "probe" / "escape"
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(canary)
        rows = []
        for name in ("a", "b", "probe"):
            st = (WORK / name).stat()
            rows.append(f"{name} uid={st.st_uid} gid={st.st_gid} mode={oct(stat.S_IMODE(st.st_mode))}")
        payload = {
            "tree": "\n".join(rows),
            "modes": modes,
            "creator": creds(),
            "chain": chain_stat(WORK / "a"),
        }
        return json.dumps(payload)

    # /var/tmp is sticky. The tool uid owns the tree, so no chown is required.
    # egid 10001 is the worker group. mkdir drops the setgid bit; chmod by the
    # owner puts it back. uid 0 is not a member of that group.
    code, text = run_as(UID_TOOL, UID_WORKER, create)
    if code != 0:
        raise RuntimeError(f"work create failed: {code} {text}")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"work create returned {text!r}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"work create returned {text!r}")
    return payload


def start_abstract() -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind("\0" + ABSTRACT)
    sock.listen(4)
    sock.settimeout(0.2)
    return sock


def ro_args() -> list[str]:
    args: list[str] = []
    for path in ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/opt/sec08-trusted/bin"):
        if Path(path).exists():
            args.extend(["--ro", path])
    return args


def protected_args() -> list[str]:
    return [
        "--protected",
        str(WORK / "a"),
        "--protected",
        str(WORK / "b"),
        "--protected",
        str(WORK / "probe"),
        "--protected",
        str(WORK / "published"),
        "--protected",
        str(CONTROL),
        "--protected",
        str(WORK),
    ]


def launch(name: str, attempt: Path, procs: Path | None, extra: list[str]) -> subprocess.Popen:
    cmd = [
        BIN,
        "launch",
        "--attempt",
        str(attempt),
        "--binary",
        BIN,
    ]
    if procs is not None:
        cmd.extend(["--cgroup-procs", str(procs)])
    cmd.extend(
        [
            "--control-dir",
            str(CONTROL),
            "--status",
            str(OUT / f"{name}-launcher-status.txt"),
            *ro_args(),
            *protected_args(),
            "--",
            BIN,
            *extra,
        ]
    )
    err = open(OUT / f"{name}-launch.err", "w")
    return subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=err,
        close_fds=True,
        preexec_fn=launcher_credentials,
    )


def status_caps(path: Path) -> dict:
    found: dict[str, str] = {}
    if not path.is_file():
        return found
    for line in path.read_text().splitlines():
        name, sep, value = line.partition(":")
        if sep and name in {"CapEff", "CapPrm", "CapBnd", "CapInh", "CapAmb"}:
            found[name] = value.strip()
    return found


def wait_file(path: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with as_worker_group():
            try:
                ready = path.is_file() and path.stat().st_size > 0
            except OSError as exc:
                detail = {
                    "path": str(path),
                    "errno": exc.errno,
                    "creds": creds(),
                    "chain": chain_stat(path.parent),
                }
                raise RuntimeError(f"workspace access failed {detail}") from exc
        if ready:
            return True
        time.sleep(0.05)
    return False


def pid_from(path: Path) -> int:
    with as_worker_group():
        return int(path.read_text().strip())


def ready_launcher_caps() -> dict:
    """Bounded tool readiness is the barrier for closed launcher status files."""
    deadline = time.monotonic() + 5
    for name in ("a", "b"):
        for kind in ("parent", "child"):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not wait_file(WORK / name / f"{kind}.pid", remaining):
                raise TimeoutError("sleep jobs did not publish pids")
    return {name: status_caps(OUT / f"{name}-launcher-status.txt") for name in ("a", "b")}


def file_size(path: Path) -> int:
    with as_worker_group():
        return path.stat().st_size


def fresh_mode(report: dict) -> int:
    report["cgroup"] = Path("/proc/self/cgroup").read_text().strip()
    report["mountinfo"] = mountinfo_cgroup()
    names = sorted(path.name for path in CG.iterdir() if path.is_dir())
    report["fresh_existing_dirs"] = names
    if any(name.startswith("fn-") for name in names):
        report["error"] = "fresh scope already had fn- cgroups"
        return 1
    job = CG / "fn-fresh"
    job.mkdir()
    (job / "cgroup.procs").read_text()
    report["fresh_created"] = job.name
    report["fresh_only_own"] = sorted(path.name for path in CG.iterdir() if path.is_dir()) == ["fn-fresh"]
    return 0 if report["fresh_only_own"] else 1


def full_mode(report: dict) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    sub = libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
    report["subreaper_rc"] = int(sub)
    report["euid"] = os.geteuid()
    report["cap_eff"] = cap_eff()
    report["supervisor_caps"] = status_caps(Path("/proc/self/status"))
    report["cgroup"] = Path("/proc/self/cgroup").read_text().strip()
    report["mountinfo"] = mountinfo_cgroup()
    report["mountinfo_all"] = Path("/proc/self/mountinfo").read_text()
    report["mount_writable"] = private_cgroup_mount(report["mountinfo_all"])
    root_stat = CG.stat()
    report["cgroup_root_identity"] = {"dev": root_stat.st_dev, "ino": root_stat.st_ino}
    report["cgroup_namespace"] = os.readlink("/proc/self/ns/cgroup")
    report["inherited_cgroup_fds"] = cgroup_fds(report["mountinfo_all"])
    if not report["mount_writable"] or report["cgroup"] != "0::/":
        report["error"] = "cgroup view is not a writable private root"
        return 1
    for name in ("fn-a", "fn-b", "fn-probe"):
        if (CG / name).exists():
            report["error"] = f"{name} existed before the supervisor created it"
            return 1

    # The proof uses numeric identities. Do not mutate passwd/shadow at runtime
    # with a capability set intentionally unable to maintain those databases.
    report["socket_dir"] = prepare_socket_dir()
    listener = start_listener()
    report["listener_pid"] = listener
    sock_stat = SOCKET_PATH.stat()
    report["socket"] = {
        "uid": sock_stat.st_uid,
        "gid": sock_stat.st_gid,
        "mode": oct(stat.S_IMODE(sock_stat.st_mode)),
    }
    report["connect_worker"] = connect_as(UID_WORKER, UID_WORKER)
    report["connect_tool"] = connect_as(UID_TOOL, UID_TOOL)
    report["work"] = prepare_work()

    report["namespace_cgroup_root"] = str(CG)
    report["namespace_memory_max_path"] = str(CG / "memory.max")
    report["host_slice_memory_path"] = os.environ.get("HOST_SLICE_MEMORY_PATH", "")
    report["host_sentinel_cgroup"] = os.environ.get("HOST_SENTINEL_CGROUP", "")
    outside: dict[str, str] = {}
    for label, env_name in (("sentinel", "HOST_SENTINEL_CGROUP"), ("slice", "HOST_SLICE_MEMORY_PATH")):
        target = os.environ.get(env_name, "")
        if not target:
            outside[label] = "unset"
            continue
        try:
            fd = os.open(target, os.O_RDONLY | os.O_CLOEXEC)
            os.close(fd)
            outside[label] = "opened"
        except OSError as exc:
            outside[label] = f"errno {exc.errno}"
    # Diagnostic only: .. walks out of the mount to /sys/fs. It is NOT a
    # handle to the parent host cgroup and must not be a same-inode gate.
    outside["mount_parent_resolved"] = str((CG / "..").resolve(strict=True))
    report["outside"] = outside

    (CG / "fn-a").mkdir()
    (CG / "fn-b").mkdir()
    (CG / "fn-probe").mkdir()
    report["created_cgroups"] = ["fn-a", "fn-b", "fn-probe"]
    kill_a = (CG / "fn-a" / "cgroup.kill").stat()
    report["cgroup_kill_a_owner"] = {"uid": kill_a.st_uid, "mode": oct(stat.S_IMODE(kill_a.st_mode))}

    _jobs = {
        "a": launch(
            "a",
            WORK / "a",
            CG / "fn-a" / "cgroup.procs",
            ["probe", "--mode", "sleep", "--attempt", str(WORK / "a"), "--result", str(OUT / "a-sleep.json")],
        ),
        "b": launch(
            "b",
            WORK / "b",
            CG / "fn-b" / "cgroup.procs",
            ["probe", "--mode", "sleep", "--attempt", str(WORK / "b"), "--result", str(OUT / "b-sleep.json")],
        ),
    }
    # The tool publishes its pid files only after the launcher has written and
    # closed its status file, applied the domain, and exec'd the tool. Popen
    # alone is not a readiness barrier. Missing/malformed caps still fail.
    report["launcher_caps"] = ready_launcher_caps()
    report["jobs_ready"] = True
    tracked: dict[str, dict] = {}
    for name in ("a", "b"):
        parent = pid_from(WORK / name / "parent.pid")
        child = pid_from(WORK / name / "child.pid")
        parent_obs = observe(parent)
        child_obs = observe(child)
        tracked[name] = {
            "parent": parent_obs,
            "child": child_obs,
            "heartbeat_parent": file_size(WORK / name / "parent.hb"),
            "heartbeat_child": file_size(WORK / name / "child.hb"),
            "procs": (CG / f"fn-{name}" / "cgroup.procs").read_text(),
        }
    report["before"] = tracked

    abstract = start_abstract()
    probe = launch(
        "probe",
        WORK / "probe",
        CG / "fn-probe" / "cgroup.procs",
        [
            "probe",
            "--mode",
            "full",
            "--attempt",
            str(WORK / "probe"),
            "--result",
            str(WORK / "probe" / "result.json"),
            "--sibling",
            str(WORK / "b" / "canary"),
            "--published",
            str(WORK / "published" / "canary"),
            "--symlink",
            str(WORK / "probe" / "escape"),
            "--socket",
            str(SOCKET_PATH),
            "--abstract",
            ABSTRACT,
            "--parent-procs",
            str(CG / "cgroup.procs"),
            "--cgroup-kill",
            str(CG / "fn-b" / "cgroup.kill"),
            "--sibling-pid",
            str(tracked["b"]["parent"]["pid"]),
            "--executor-pid",
            "1",
        ],
    )
    try:
        probe.wait(timeout=15)
    except subprocess.TimeoutExpired:
        probe.kill()
        report["probe_timeout"] = True
        return 1
    finally:
        abstract.close()
    report["probe_rc"] = probe.returncode
    report.setdefault("launcher_caps", {})["probe"] = status_caps(OUT / "probe-launcher-status.txt")
    probe_path = WORK / "probe" / "result.json"
    with as_worker_group():
        present = probe_path.is_file()
        payload = probe_path.read_text() if present else ""
    if not present:
        report["error"] = "probe result missing"
        return 1
    report["probe"] = json.loads(payload)

    # Cancellation is a write by this uid-0 supervisor to the file it created.
    kill_path = CG / "fn-a" / "cgroup.kill"
    before_owner = kill_path.stat().st_uid
    kill_path.write_text("1\n")
    report["kill_writer_euid"] = os.geteuid()
    report["kill_owner_unchanged"] = kill_path.stat().st_uid == before_owner == 0
    a_parent = tracked["a"]["parent"]["pid"]
    a_child = tracked["a"]["child"]["pid"]
    a_parent_start = tracked["a"]["parent"]["starttime"]
    a_child_start = tracked["a"]["child"]["starttime"]
    deadline = time.monotonic() + 3
    waited: list[int] = []
    while time.monotonic() < deadline and not {a_parent, a_child}.issubset(waited):
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        if pid == 0:
            time.sleep(0.05)
            continue
        waited.append(pid)
        report.setdefault("wait_status", {})[str(pid)] = status
    report["waited"] = waited
    report["after_a"] = {
        "parent": observe(a_parent, a_parent_start),
        "child": observe(a_child, a_child_start),
    }
    b_before_parent = tracked["b"]["heartbeat_parent"]
    b_before_child = tracked["b"]["heartbeat_child"]
    grew = False
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if file_size(WORK / "b" / "parent.hb") > b_before_parent and file_size(
            WORK / "b" / "child.hb"
        ) > b_before_child:
            grew = True
            break
        time.sleep(0.1)
    report["b_after"] = {
        "parent": observe(tracked["b"]["parent"]["pid"], tracked["b"]["parent"]["starttime"]),
        "child": observe(tracked["b"]["child"]["pid"], tracked["b"]["child"]["starttime"]),
        "heartbeat_parent": file_size(WORK / "b" / "parent.hb"),
        "heartbeat_child": file_size(WORK / "b" / "child.hb"),
        "grew": grew,
    }
    try:
        os.kill(listener, 15)
        os.waitpid(listener, 0)
    except OSError:
        pass

    local_max = CG / "memory.max"
    report["memory_max_before"] = local_max.read_text().strip() if local_max.exists() else "missing"
    try:
        local_max.write_text("max\n")
        report["memory_write"] = "ok"
    except OSError as exc:
        report["memory_write"] = f"errno {exc.errno}"
    report["memory_max_after"] = local_max.read_text().strip() if local_max.exists() else "missing"
    # A modest allocation shows the process is still accounted, without stressing the 64M ceiling.
    blob = bytearray(1024 * 1024)
    report["small_alloc_bytes"] = len(blob)
    del blob
    report["b_still"] = observe(tracked["b"]["parent"]["pid"], tracked["b"]["parent"]["starttime"])
    report["cgroup_top"] = sorted(path.name for path in CG.iterdir())
    report["cgroup_dirs"] = sorted(path.name for path in CG.iterdir() if path.is_dir())
    atomic_write(OUT / "supervisor.json", report)
    (OUT / "hold").write_text("1\n")
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline and not (OUT / "please-kill").exists():
        time.sleep(0.1)
    report["released"] = (OUT / "please-kill").exists()
    return 0


def dac_smoke(report: dict) -> int:
    """Filesystem contract only. This mode does not create cgroups."""
    report["cgroups_created"] = []
    report["supervisor_creds"] = creds()
    report["socket_dir"] = prepare_socket_dir()
    listener = start_listener()
    report["socket"] = {
        "uid": SOCKET_PATH.stat().st_uid,
        "gid": SOCKET_PATH.stat().st_gid,
        "mode": oct(stat.S_IMODE(SOCKET_PATH.stat().st_mode)),
    }
    report["connect_tool"] = connect_as(UID_TOOL, UID_TOOL)
    report["connect_worker"] = connect_as(UID_WORKER, UID_WORKER)
    report["work"] = prepare_work()
    result = WORK / "a" / "result.json"
    proc = launch(
        "marker",
        WORK / "a",
        None,
        ["probe", "--mode", "marker", "--attempt", str(WORK / "a"), "--result", str(result)],
    )
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        report["error"] = "marker timeout"
        return 1
    report["marker_rc"] = proc.returncode
    report["launcher_caps"] = status_caps(OUT / "marker-launcher-status.txt")
    report["launcher_status"] = (OUT / "marker-launcher-status.txt").read_text() if (
        OUT / "marker-launcher-status.txt"
    ).is_file() else ""
    with as_worker_group():
        report["supervisor_read_creds"] = creds()
        report["result_text"] = result.read_text() if result.is_file() else ""
        result_mode = oct(stat.S_IMODE(result.stat().st_mode)) if result.is_file() else ""
    report["result_mode"] = result_mode

    def worker_read() -> str:
        return result.read_text()

    report["worker_read"] = run_as(UID_WORKER, UID_WORKER, worker_read)
    try:
        os.kill(listener, 15)
        os.waitpid(listener, 0)
    except OSError:
        pass
    text = report["result_text"]
    modes = report["work"].get("modes") or []
    proven = [item.get("proven") for item in modes]
    checks = {
        "workspace_mode_02750": proven == ["0o2750"] * len(proven) and len(proven) == 5,
        "mkdir_result_recorded": all(item.get("after_mkdir") != item.get("proven") or item.get("after_mkdir") == "0o2750" for item in modes)
        and any(item.get("after_mkdir") != item.get("requested") for item in modes),
        "marker_exit_0": proc.returncode == 0,
        "launcher_caps_narrow": {report["launcher_caps"].get(name) for name in ("CapEff", "CapPrm", "CapBnd")}
        == {"00000000000001c0"},
        "launcher_group_is_worker": "resgid 10001 10001 10001" in report["launcher_status"],
        "tool_uid_10003": "uid 10003 10003 10003" in text,
        "tool_gid_10003": "gid 10003 10003 10003" in text,
        "tool_groups_empty": "groups 0 " in text,
        "tool_caps_clear": "cap 00000000/00000000/00000000" in text,
        "result_mode_0640": result_mode == "0o640",
        "supervisor_egid_while_reading": report["supervisor_read_creds"]["egid"] == UID_WORKER,
        "worker_can_read": report["worker_read"][0] == 0 and "uid 10003 10003 10003" in report["worker_read"][1],
        "tool_socket_denied": str(report["connect_tool"]).startswith("errno 13"),
        "worker_socket_ok": report["connect_worker"] == "ok",
        "socket_not_tool_owned": report["socket"]["uid"] == UID_EXEC and report["socket"]["mode"] == "0o660",
    }
    report["checks"] = checks
    report["passed"] = all(checks.values())
    return 0 if report["passed"] else 1


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"
    report: dict = {"mode": mode, "phase": "start"}
    OUT.mkdir(parents=True, exist_ok=True)
    code = 1
    try:
        if mode == "fresh":
            code = fresh_mode(report)
        elif mode == "dac-smoke":
            code = dac_smoke(report)
        else:
            code = full_mode(report)
    except Exception as exc:  # noqa: BLE001 - the partial report is the evidence
        report["error"] = f"{type(exc).__name__}: {exc}"
        code = 1
    report["exit_code"] = code
    try:
        atomic_write(OUT / "supervisor.json", report)
    except OSError as exc:
        sys.stderr.write(f"report write failed: {exc}\n")
    if mode == "dac-smoke":
        modes = (report.get("work") or {}).get("modes") or []
        print(
            json.dumps(
                {
                    "passed": report.get("passed"),
                    "checks": report.get("checks"),
                    "error": report.get("error"),
                    "mkdir": [
                        {
                            "path": item.get("path"),
                            "after_mkdir": item.get("after_mkdir"),
                            "proven": item.get("proven"),
                        }
                        for item in modes
                    ],
                }
            )
        )
    return code


if __name__ == "__main__":
    sys.exit(main())
