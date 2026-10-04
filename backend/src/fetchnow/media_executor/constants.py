"""Numeric identities and modes for the SEC-08 executor.

These match the container proof. uid 0 is not a DAC bypass: the supervisor
has only SETUID, SETGID, and SETPCAP.
"""

from __future__ import annotations

WORKER_UID = 10001
WORKER_GID = 10001
EXECUTOR_UID = 10002
TOOL_UID = 10003
TOOL_GID = 10003

# Sticky root, same idea as /var/tmp: the tool uid can create its own job dir.
WORK_ROOT_MODE = 0o1777
# Owner tool, group worker, setgid. Group write lets the worker stage inputs.
JOB_DIR_MODE = 0o2770
# Tool-created outputs. Group 10001 via the directory setgid bit.
TOOL_FILE_MODE = 0o640
SOCKET_DIR_MODE = 0o770
SOCKET_MODE = 0o660

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 4096
MAX_RESPONSE_BYTES = 400_000
MAX_JOBS = 2
MIN_LANDLOCK_ABI = 6

INPUT_VIDEO = "input-video"
INPUT_AUDIO = "input-audio"
OUTPUT_MUX = "output-mux"

OPS_OFFLINE = frozenset(
    {"reserve", "mux_copy", "ffprobe_validate", "cancel", "release"}
)
OPS_DEFERRED = frozenset({"inspect_metadata", "download_progressive"})
