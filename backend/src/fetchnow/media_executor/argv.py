"""Server-built argv. Nothing in the UDS request selects a binary or a flag."""

from __future__ import annotations

from pathlib import Path


def mux_argv(
    *,
    ffmpeg: str,
    video: Path,
    audio: Path,
    output: Path,
    container: str,
) -> list[str]:
    if container not in {"mp4", "webm"}:
        raise ValueError("container")
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-loglevel",
        "error",
        "-n",
        "-protocol_whitelist",
        "file",
        "-i",
        str(video),
        "-i",
        str(audio),
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
        container,
        str(output),
    ]


def ffprobe_argv(*, ffprobe: str, source: Path) -> list[str]:
    return [
        ffprobe,
        "-hide_banner",
        "-loglevel",
        "error",
        "-protocol_whitelist",
        "file",
        "-print_format",
        "json",
        "-show_entries",
        "stream=codec_type,codec_name:format=format_name,duration,size",
        "-i",
        str(source),
    ]


def tool_env() -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
    }
