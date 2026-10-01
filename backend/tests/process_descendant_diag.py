"""Test-only diagnostics for a single known descendant PID.

Used by process-hardening tests when a descendant is still observable after the
runner's kill path. Does not dump environment variables or scan unrelated PIDs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _read_text(path: Path, *, limit: int = 4096) -> str | None:
    try:
        data = path.read_bytes()[:limit]
    except FileNotFoundError:
        return None
    except PermissionError:
        return "PERMISSION_DENIED"
    except OSError as exc:
        return f"OSERROR:{exc.errno}"
    return data.decode("utf-8", errors="replace").rstrip("\x00")


def _parse_status(status: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if not status or status.startswith(("PERMISSION_", "OSERROR:")):
        return out
    for line in status.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if key in {"Name", "State", "Pid", "PPid", "Uid", "Gid", "NSpid"}:
            out[key] = value.strip()
    return out


def _parse_stat(stat_line: str | None) -> dict[str, Any]:
    """Best-effort fields from /proc/<pid>/stat (Linux)."""
    if not stat_line or stat_line.startswith(("PERMISSION_", "OSERROR:")):
        return {}
    # comm may contain spaces/parens: find last ')' then split the rest.
    rparen = stat_line.rfind(")")
    if rparen < 0 or " " not in stat_line[rparen:]:
        return {"raw_prefix": stat_line[:80]}
    prefix = stat_line[: rparen + 1]
    rest = stat_line[rparen + 2 :].split()
    # After ')': state ppid pgrp session ...
    out: dict[str, Any] = {"stat_prefix": prefix[:120]}
    if rest:
        out["state"] = rest[0]
    if len(rest) > 1:
        out["ppid"] = rest[1]
    if len(rest) > 2:
        out["pgrp"] = rest[2]
    if len(rest) > 3:
        out["session"] = rest[3]
    return out


def snapshot_descendant(pid: int, *, role: str) -> dict[str, Any]:
    """Snapshot one test-owned PID. Safe fields only."""
    if type(pid) is not int or isinstance(pid, bool) or pid <= 1:
        return {"error": "INVALID_PID", "pid": pid, "role": role}

    proc = Path("/proc") / str(pid)
    exists = proc.exists()
    kill0: str
    try:
        os.kill(pid, 0)
        kill0 = "alive_or_zombie"
    except ProcessLookupError:
        kill0 = "gone"
    except PermissionError:
        kill0 = "permission_denied"

    status_raw = _read_text(proc / "status") if exists else None
    stat_raw = _read_text(proc / "stat") if exists else None
    cmdline_raw = _read_text(proc / "cmdline") if exists else None
    status = _parse_status(status_raw)
    stat = _parse_stat(stat_raw)
    cmdline = None
    if cmdline_raw and not cmdline_raw.startswith(("PERMISSION_", "OSERROR:")):
        # Keep short; strip NULs to spaces for readability.
        cmdline = " ".join(cmdline_raw.split("\x00")).strip()[:200]

    state = status.get("State") or stat.get("state")
    zombie = bool(state) and str(state).startswith("Z")

    return {
        "role": role,
        "pid": pid,
        "observer_uid": os.getuid(),
        "proc_exists": exists,
        "kill0": kill0,
        "zombie": zombie,
        "state": state,
        "ppid": status.get("PPid") or stat.get("ppid"),
        "pgid": stat.get("pgrp"),
        "sid": stat.get("session"),
        "uid_line": status.get("Uid"),
        "gid_line": status.get("Gid"),
        "comm": status.get("Name"),
        "cmdline": cmdline,
        # kill(pid,0) success is NOT proof of runnable code (zombies still exist).
        "kill0_implies_runnable_code": False,
    }


def emit_descendant_diag_if_observable(pid: int, *, role: str, alive: bool) -> None:
    """Print one JSON line when a descendant is still observable after kill wait."""
    if not alive:
        return
    payload = snapshot_descendant(pid, role=role)
    print("PROCESS_DESCENDANT_DIAG " + json.dumps(payload, sort_keys=True), flush=True)
