#!/usr/bin/env python3
"""Native F2 disk probe using production LandlockRunner/job_process.

Starts a disposable SEC-08-style one-shot container from an already-built
media-executor image. Does NOT replace prctl, cgroup.kill, or identity-aware
measurement.

Trusted controller: disk_native_inside.py (mounted RO). Small ceilings are
passed directly to LandlockRunner — not proof of untrusted RPC limit mapping.
"""

from __future__ import annotations

import json
import os
import platform
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
INSIDE = ROOT / "backend" / "tests" / "media_executor" / "disk_native_inside.py"
NAME_PREFIX = "fetchnow-sec09-disk-"


def _run(argv: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        argv,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"{argv[0]} failed ({proc.returncode}): {(proc.stderr or '')[-1500:]}"
        )
    return proc


def _bounded_cleanup(argv: list[str], *, timeout: int = 30) -> dict[str, Any]:
    """Run cleanup with checked return code; never hide failure."""
    try:
        proc = subprocess.run(
            argv,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except OSError as exc:
        return {"ok": False, "error": type(exc).__name__, "argv0": argv[0]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout", "argv0": argv[0]}
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "argv0": argv[0],
        "stderr_tail": (proc.stderr or "")[-400:],
    }


def run_disk_probe(
    *,
    image_ref: str,
    image_id: str,
    evidence_dir: Path,
) -> dict[str, Any]:
    """Run F2 cases; returns structured evidence (ok + per-case results)."""
    if (
        platform.system() != "Linux"
        or platform.machine() not in {"x86_64", "amd64"}
        or os.geteuid() != 0
    ):
        return {
            "ok": False,
            "status": "NOT_RUN",
            "error": "requires native Linux AMD64/root",
            "cases": [],
        }
    if not INSIDE.is_file():
        raise RuntimeError(f"missing probe fixture: {INSIDE}")
    observed = _run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image_ref]
    ).stdout.strip()
    if observed != image_id:
        raise RuntimeError(
            f"image id mismatch: expected {image_id}, inspect={observed}"
        )

    suffix = secrets.token_hex(4)
    name = f"{NAME_PREFIX}{suffix}"
    unit_name = f"{name}.slice"
    unit = Path("/run/systemd/system") / unit_name
    work = Path(tempfile.mkdtemp(prefix=f"{name}-"))
    private = work / "private"
    for part in ("work", "ipc"):
        (private / part).mkdir(parents=True)
    result: dict[str, Any] = {
        "ok": False,
        "status": "FAIL",
        "image_ref": image_ref,
        "image_id": image_id,
        "slice": unit_name,
        "cases": [],
        "trust_boundary": (
            "trusted disk_native_inside controller; LandlockRunner real; "
            "not untrusted RPC ceiling proof"
        ),
        "fixture_roots": [str(INSIDE.relative_to(ROOT))],
        "cleanup": [],
    }
    installed = False
    try:
        if unit.exists():
            raise RuntimeError(f"refusing existing slice {unit}")
        unit.write_text(
            "[Unit]\nDescription=FetchNow disposable F2 disk probe\n"
            "[Slice]\nMemoryMax=512M\nTasksMax=128\n"
        )
        _run(["systemctl", "daemon-reload"])
        _run(["systemctl", "start", unit_name])
        installed = True
        argv = [
            "docker",
            "run",
            "--rm",
            "--name",
            f"{name}-media",
            "--network",
            "none",
            "--cgroupns",
            "private",
            "--cgroup-parent",
            unit_name,
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
            "--read-only",
            "--tmpfs",
            "/tmp:mode=1777,size=32m",
            "-v",
            f"{private / 'work'}:/var/lib/fetchnow/executor",
            "-v",
            f"{private / 'ipc'}:/run/fetchnow-executor",
            "-v",
            f"{INSIDE}:/opt/sec08-tests/disk_native_inside.py:ro",
            "-e",
            "MEDIA_EXECUTOR_WORK_ROOT=/var/lib/fetchnow/executor",
            "-e",
            "MEDIA_EXECUTOR_SOCKET=/run/fetchnow-executor/ctrl/worker.sock",
            "-e",
            "MEDIA_EXECUTOR_LAUNCHER=/usr/local/bin/sec08-launch",
            "-e",
            "MEDIA_EXECUTOR_PROFILE=offline",
            "-e",
            "DISK_PROBE_WORK=/var/lib/fetchnow/executor/disk-probe",
            "--entrypoint",
            "python",
            image_ref,
            "/opt/sec08-tests/disk_native_inside.py",
        ]
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
            raise RuntimeError(
                "probe produced no JSON: "
                f"rc={proc.returncode} stderr={(proc.stderr or '')[-1200:]}"
            )
        result["cases"] = list(payload.get("cases") or [])
        result["positive_control_gate"] = bool(payload.get("positive_control_gate"))
        result["ok"] = (
            bool(payload.get("ok"))
            and bool(payload.get("positive_control_gate"))
            and proc.returncode == 0
        )
        result["status"] = "PASS" if result["ok"] else "FAIL"
        result["probe_payload"] = {
            "ok": payload.get("ok"),
            "positive_control_gate": payload.get("positive_control_gate"),
            "trust_boundary": payload.get("trust_boundary"),
            "fixture_roots": payload.get("fixture_roots"),
            "image_platform": payload.get("image_platform"),
        }
        result["container_exit"] = proc.returncode
        result["container_stderr_tail"] = (proc.stderr or "")[-800:]
        return result
    finally:
        cleanup_ok = True
        if installed:
            stop = _bounded_cleanup(["systemctl", "stop", unit_name])
            result["cleanup"].append({"step": "systemctl_stop", **stop})
            if not stop["ok"]:
                cleanup_ok = False
            if unit.exists():
                try:
                    unit.unlink()
                    result["cleanup"].append({"step": "unlink_unit", "ok": True})
                except OSError as exc:
                    cleanup_ok = False
                    result["cleanup"].append(
                        {
                            "step": "unlink_unit",
                            "ok": False,
                            "error": type(exc).__name__,
                        }
                    )
            reload = _bounded_cleanup(["systemctl", "daemon-reload"])
            result["cleanup"].append({"step": "daemon_reload", **reload})
            if not reload["ok"]:
                cleanup_ok = False
        try:
            shutil.rmtree(work)
            result["cleanup"].append({"step": "rmtree_work", "ok": True})
        except OSError as exc:
            cleanup_ok = False
            result["cleanup"].append(
                {"step": "rmtree_work", "ok": False, "error": type(exc).__name__}
            )
        result["cleanup_ok"] = cleanup_ok
        if not cleanup_ok:
            result["cleanup_slice_error"] = True
            # Cleanup failure must not flip a failed probe to PASS.
            if result.get("ok"):
                result["ok"] = False
                result["status"] = "FAIL"
                result["error"] = "probe_cleanup_failed"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (evidence_dir / "f2-disk-probe.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    raise SystemExit("import run_disk_probe from the release matrix harness")
