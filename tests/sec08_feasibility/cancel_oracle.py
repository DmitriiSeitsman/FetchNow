"""Pure classifier for SEC-08 cancellation observations.

kill(pid, 0) is not an input. A live task and an unreaped zombie both still
accept signal 0, and this module keeps those outcomes apart.
"""

from __future__ import annotations

RUNNING_CLASSES = frozenset({"running", "sleeping", "stopped"})
TERMINATED_CLASSES = frozenset({"zombie", "reaped"})


def parse_proc_stat(text: str) -> dict | None:
    end = text.rfind(")")
    if end < 2 or text[end + 1 : end + 2] != " ":
        return None
    fields = text[end + 2 :].split()
    if len(fields) < 20:
        return None
    state = fields[0]
    if len(state) != 1:
        return None
    try:
        return {
            "state": state,
            "ppid": int(fields[1]),
            "pgrp": int(fields[2]),
            "session": int(fields[3]),
            "starttime": int(fields[19]),
        }
    except ValueError:
        return None


def _state_class(state: str) -> str:
    if state == "R":
        return "running"
    if state in {"S", "D", "I", "W"}:
        return "sleeping"
    if state in {"T", "t"}:
        return "stopped"
    if state == "Z":
        return "zombie"
    return "observation_error"


def classify_pid(
    stat_text: str | None,
    *,
    expected_starttime: int | None,
    read_error: str | None = None,
) -> dict:
    if read_error == "absent":
        return {
            "class": "reaped",
            "state": None,
            "ppid": None,
            "pgrp": None,
            "session": None,
            "starttime": None,
            "expected_starttime": expected_starttime,
        }
    if read_error:
        return {
            "class": "observation_error",
            "state": None,
            "ppid": None,
            "pgrp": None,
            "session": None,
            "starttime": None,
            "expected_starttime": expected_starttime,
            "error": read_error,
        }
    parsed = parse_proc_stat(stat_text or "")
    if parsed is None:
        return {
            "class": "observation_error",
            "state": None,
            "ppid": None,
            "pgrp": None,
            "session": None,
            "starttime": None,
            "expected_starttime": expected_starttime,
            "error": "unparsed stat",
        }
    if expected_starttime is not None and parsed["starttime"] != expected_starttime:
        return {
            "class": "pid_reused",
            "state": parsed["state"],
            "ppid": parsed["ppid"],
            "pgrp": parsed["pgrp"],
            "session": parsed["session"],
            "starttime": parsed["starttime"],
            "expected_starttime": expected_starttime,
        }
    return {
        "class": _state_class(parsed["state"]),
        "state": parsed["state"],
        "ppid": parsed["ppid"],
        "pgrp": parsed["pgrp"],
        "session": parsed["session"],
        "starttime": parsed["starttime"],
        "expected_starttime": expected_starttime,
    }


def targets_were_running(observations: list[dict]) -> bool:
    if not observations:
        return False
    return all(item.get("class") in {"running", "sleeping"} for item in observations)


def termination_proven(observations: list[dict]) -> bool:
    """No tracked task is still executing. A zombie counts as exited, not as running."""
    if not observations:
        return False
    return all(item.get("class") in TERMINATED_CLASSES for item in observations)


def reaping_proven(wait_pids: set[int], expected_pids: set[int]) -> bool:
    """The supervisor's own wait collected every tracked pid."""
    if not expected_pids:
        return False
    return expected_pids <= wait_pids
