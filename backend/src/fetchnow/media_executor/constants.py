"""Numeric identities and modes for the SEC-08/SEC-09 executor.

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
MAX_URL_CHARS = 2048
MAX_FORMAT_TOKEN_CHARS = 256
# Artifact size ceiling (matches MEDIA_DOWNLOAD_MAX_BYTES default).
MAX_ARTIFACT_BYTES = 3_221_225_472
# Free-space field is independent: worker sends min_free + headroom + handoff.
# Bound generously but finitely (default min_free 2GiB + peaks + copy).
MAX_MIN_FREE_BYTES = 34_359_738_368  # 32 GiB

# Server-side attempt disk ceilings for tool ops that do not carry client
# max_bytes (mux/ffprobe/inspect). NOT an approved production budget — only a
# fail-closed disposable/runtime safety rail.
#
# Semantics (job_process / LocalRunner):
# - baseline = tree bytes measured at tool start (inputs already present).
# - growth = current_tree - baseline  (new writes: output / cache / temp).
# - tree = absolute attempt usage including inputs + output + cache.
# Growth and tree are independent; large legal mux inputs must not count as
# "new output" against the growth ceiling.
TOOL_MIN_FREE_BYTES = 268_435_456  # 256 MiB floor when client omits min_free
OFFLINE_MUX_MAX_GROWTH_BYTES = MAX_ARTIFACT_BYTES
OFFLINE_MUX_MAX_TREE_BYTES = 3 * MAX_ARTIFACT_BYTES  # video+audio+output peak
OFFLINE_FFPROBE_MAX_GROWTH_BYTES = 16 * 1024 * 1024
OFFLINE_FFPROBE_MAX_TREE_BYTES = 3 * MAX_ARTIFACT_BYTES + 16 * 1024 * 1024
INSPECT_MAX_GROWTH_BYTES = 64 * 1024 * 1024  # yt-dlp metadata cache bound
INSPECT_MAX_TREE_BYTES = 64 * 1024 * 1024

INPUT_VIDEO = "input-video"
INPUT_AUDIO = "input-audio"
OUTPUT_MUX = "output-mux"
OUTPUT_ARTIFACT_GLOB = "output-artifact.*"

PROFILE_OFFLINE = "offline"
PROFILE_NETWORK = "network"

OPS_LIFECYCLE = frozenset({"reserve", "cancel", "release"})
OPS_OFFLINE_TOOLS = frozenset({"mux_copy", "ffprobe_validate"})
OPS_NETWORK_TOOLS = frozenset(
    {
        "inspect_metadata",
        "download_progressive",
        "download_video",
        "download_audio",
    }
)
OPS_OFFLINE = OPS_LIFECYCLE | OPS_OFFLINE_TOOLS
OPS_NETWORK = OPS_LIFECYCLE | OPS_NETWORK_TOOLS
# Historical alias used by offline protocol rejection tests.
OPS_DEFERRED = OPS_NETWORK_TOOLS

PROVIDER_EXTRACTORS: dict[str, frozenset[str]] = {
    "vk": frozenset({"vk"}),
    "rutube": frozenset({"rutube"}),
    "ok": frozenset({"odnoklassniki"}),
    "dzen": frozenset({"dzen.ru"}),
}
