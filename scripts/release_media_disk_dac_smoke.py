#!/usr/bin/env python3
"""Scoped DAC smoke for F2 attempt layout (not Landlock/cgroup acceptance).

Runs a disposable Linux container with SETUID/SETGID/SETPCAP only,
no-new-privileges, and production layout helpers. Proves tool uid can write
into make_job_directory attempts and cannot treat root-owned mkdir as PASS.

Safe on Docker Desktop without writable cgroups. Never touches production.
"""

from __future__ import annotations

import json
import platform
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


INSIDE_SOURCE = r'''
"""Executed inside the smoke container only."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

from fetchnow.media_executor.constants import (
    JOB_DIR_MODE,
    TOOL_UID,
    WORK_ROOT_MODE,
    WORKER_GID,
    WORKER_UID,
)
from fetchnow.media_executor.layout import make_job_directory, make_work_root


def _ident(path: Path) -> dict[str, int]:
    st = path.stat()
    return {
        "uid": int(st.st_uid),
        "gid": int(st.st_gid),
        "mode": int(stat.S_IMODE(st.st_mode)),
    }


def _write(uid: int, gid: int, path: Path, data: bytes) -> dict[str, object]:
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; "
            "Path(sys.argv[1]).write_bytes(bytes.fromhex(sys.argv[2]))",
            str(path),
            data.hex(),
        ],
        user=uid,
        group=gid,
        extra_groups=[],
        capture_output=True,
        timeout=5,
        check=False,
    )
    return {
        "returncode": proc.returncode,
        "stderr": (proc.stderr or b"")[:200].decode("utf-8", "replace"),
    }


def main() -> int:
    root = Path("/var/lib/fetchnow/executor")
    make_work_root(root)
    work = root / "dac-smoke"
    if not work.exists():
        work.mkdir(mode=WORK_ROOT_MODE)
    os.chmod(work, WORK_ROOT_MODE)

    attempt = work / "good"
    make_job_directory(attempt)
    good_id = _ident(attempt)
    marker = attempt / "marker.bin"
    tool_write = _write(TOOL_UID, WORKER_GID, marker, b"ok\n")
    # Without CAP_DAC_OVERRIDE, uid0 cannot read tool-owned files; trusted
    # controller reads via worker group identity (JOB_DIR_MODE / setgid).
    read_proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; "
            "sys.stdout.buffer.write(Path(sys.argv[1]).read_bytes())",
            str(marker),
        ],
        user=WORKER_UID,
        group=WORKER_GID,
        extra_groups=[],
        capture_output=True,
        timeout=5,
        check=False,
    )
    controller_read = read_proc.stdout if read_proc.returncode == 0 else b""

    bad = work / "root-owned"
    bad.mkdir()  # root Path.mkdir — incorrect for tool
    bad_id = _ident(bad)
    bad_marker = bad / "nope.bin"
    bad_write = _write(TOOL_UID, WORKER_GID, bad_marker, b"x")
    bad_exists_proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; "
            "raise SystemExit(0 if Path(sys.argv[1]).is_file() else 1)",
            str(bad_marker),
        ],
        user=TOOL_UID,
        group=WORKER_GID,
        extra_groups=[],
        capture_output=True,
        timeout=5,
        check=False,
    )

    payload = {
        "ok": False,
        "kind": "dac_smoke",
        "not": "native_landlock_cgroup_acceptance",
        "work_root": _ident(root),
        "work": _ident(work),
        "good_attempt": good_id,
        "good_mode_ok": good_id["mode"] == JOB_DIR_MODE
        and good_id["uid"] == TOOL_UID
        and good_id["gid"] == WORKER_GID,
        "tool_write": tool_write,
        "controller_read_ok": controller_read == b"ok\n",
        "controller_read_rc": read_proc.returncode,
        "root_owned_attempt": bad_id,
        "root_owned_tool_write": bad_write,
        "root_owned_rejected": bad_write["returncode"] != 0
        and bad_exists_proc.returncode != 0,
        "work_root_mode_ok": _ident(root)["mode"] == WORK_ROOT_MODE,
    }
    payload["ok"] = bool(
        payload["good_mode_ok"]
        and payload["work_root_mode_ok"]
        and tool_write["returncode"] == 0
        and payload["controller_read_ok"]
        and payload["root_owned_rejected"]
    )
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
'''


def main() -> int:
    if not (ROOT / "backend" / "src" / "fetchnow").is_dir():
        print(json.dumps({"ok": False, "error": "missing backend src"}))
        return 2
    # Host may be Darwin; smoke always uses a Linux container.
    work = Path(tempfile.mkdtemp(prefix="fetchnow-dac-smoke-"))
    inside = work / "smoke_inside.py"
    inside.write_text(INSIDE_SOURCE, encoding="utf-8")
    name = f"fetchnow-dac-smoke-{secrets.token_hex(4)}"
    # Executor work must be a Linux tmpfs/volume — Docker Desktop host bind
    # mounts do not preserve setuid ownership (everything looks like uid 0).
    argv = [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--network",
        "none",
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
        "--read-only",
        "--tmpfs",
        "/tmp:mode=1777,size=16m",
        "--tmpfs",
        "/var/lib/fetchnow/executor:mode=1777,size=32m",
        "-v",
        f"{ROOT / 'backend' / 'src'}:/opt/fetchnow-src:ro",
        "-v",
        f"{inside}:/opt/dac_smoke_inside.py:ro",
        "-e",
        "PYTHONPATH=/opt/fetchnow-src",
        "--entrypoint",
        "python3",
        "python:3.12.11-slim-bookworm",
        "/opt/dac_smoke_inside.py",
    ]
    try:
        # Ensure image present (pull once if needed).
        inspect = subprocess.run(
            ["docker", "image", "inspect", "python:3.12.11-slim-bookworm"],
            capture_output=True,
            check=False,
        )
        if inspect.returncode != 0:
            pull = subprocess.run(
                ["docker", "pull", "python:3.12.11-slim-bookworm"],
                capture_output=True,
                text=True,
                check=False,
            )
            if pull.returncode != 0:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "error": "image_pull_failed",
                            "stderr": (pull.stderr or "")[-500:],
                            "kind": "dac_smoke",
                            "host": f"{platform.system()}/{platform.machine()}",
                        },
                        sort_keys=True,
                    )
                )
                return 2
        proc = subprocess.run(
            argv,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        payload = None
        for line in reversed((proc.stdout or "").splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                payload = json.loads(line)
                break
        if payload is None:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": "no_json",
                        "rc": proc.returncode,
                        "stderr": (proc.stderr or "")[-800:],
                        "kind": "dac_smoke",
                    },
                    sort_keys=True,
                )
            )
            return 1
        payload["host"] = f"{platform.system()}/{platform.machine()}"
        payload["container_rc"] = proc.returncode
        print(json.dumps(payload, sort_keys=True))
        return 0 if payload.get("ok") and proc.returncode == 0 else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
