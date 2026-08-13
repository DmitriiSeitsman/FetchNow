"""Fixed argv for bounded ffprobe structural validation."""

from __future__ import annotations

import os


def build_ffprobe_argv(*, executable: str, input_path: str) -> list[str]:
    """Build a fully server-controlled ffprobe argv for one local file."""
    if not executable or executable.startswith("-") or not os.path.isabs(executable):
        raise ValueError("executable path invalid")
    if not input_path or input_path.startswith("-") or not os.path.isabs(input_path):
        raise ValueError("input path invalid")
    if "\x00" in input_path:
        raise ValueError("input path invalid")
    return [
        executable,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-protocol_whitelist",
        "file",
        "-print_format",
        "json",
        "-show_entries",
        "stream=codec_type,codec_name:format=format_name,duration,size",
        "-i",
        input_path,
    ]
