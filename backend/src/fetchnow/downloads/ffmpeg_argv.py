"""Fixed argv for stream-copy mux. No user-controlled arguments."""

from __future__ import annotations

import os

_ALLOWED_CONTAINERS = frozenset({"mp4", "webm"})


def build_ffmpeg_mux_argv(
    *,
    executable: str,
    video_path: str,
    audio_path: str,
    output_path: str,
    output_container: str,
) -> list[str]:
    """Build a fully server-controlled ffmpeg stream-copy argv.

    Inputs are local files only. Network protocols are excluded from the
    protocol whitelist. ``-c copy`` is the only codec operation.
    """
    if not executable or executable.startswith("-") or not os.path.isabs(executable):
        raise ValueError("executable path invalid")
    if output_container not in _ALLOWED_CONTAINERS:
        raise ValueError("output container invalid")
    for label, value in (
        ("video_path", video_path),
        ("audio_path", audio_path),
        ("output_path", output_path),
    ):
        if not value or not isinstance(value, str) or value.startswith("-"):
            raise ValueError(f"{label} invalid")
        if "\x00" in value:
            raise ValueError(f"{label} invalid")
        if not os.path.isabs(value):
            raise ValueError(f"{label} invalid")
    return [
        executable,
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-loglevel",
        "error",
        "-n",
        "-protocol_whitelist",
        "file",
        "-i",
        video_path,
        "-i",
        audio_path,
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c",
        "copy",
        "-sn",
        "-dn",
        "-map_metadata",
        "-1",
        "-map_metadata:s",
        "-1",
        "-map_chapters",
        "-1",
        "-f",
        output_container,
        output_path,
    ]
