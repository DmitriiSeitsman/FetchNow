"""Bounded ffprobe JSON parse. Raw JSON is never returned to callers."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from fetchnow.downloads.errors import DownloadErrorCode, raise_download_error

_MAX_JSON_DEPTH = 8
_MAX_JSON_NODES = 512
_MAX_MAPPING_KEYS = 64
_MAX_LIST_LEN = 32
_MAX_STRING_LEN = 256
_MAX_STREAMS = 8
_MAX_JSON_BYTES = 262_144

_CONTAINER_HINTS = {
    "mp4": frozenset({"mp4", "mov,mp4,m4a,3gp,3g2,mj2", "isom", "iso5"}),
    "webm": frozenset({"webm", "matroska,webm"}),
}
_CONTAINER_CODECS = {
    "mp4": (
        frozenset({"h264", "avc", "avc1"}),
        frozenset({"aac", "mp4a"}),
    ),
    "webm": (
        frozenset({"vp8", "vp9", "vp09", "av1", "av01"}),
        frozenset({"opus", "vorbis"}),
    ),
}


@dataclass(frozen=True, slots=True)
class MuxProbeResult:
    """Public-safe structural summary of a muxed file (no raw JSON)."""

    duration_seconds: float
    video_streams: int
    audio_streams: int
    other_streams: int
    format_name: str
    size_bytes: int


def _walk_limits(root: object) -> None:
    nodes = 0

    def walk(value: object, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_JSON_NODES:
            raise_download_error(
                DownloadErrorCode.MUXED_OUTPUT_INVALID,
                internal_reason="JSON_NODE_LIMIT",
            )
        if depth > _MAX_JSON_DEPTH:
            raise_download_error(
                DownloadErrorCode.MUXED_OUTPUT_INVALID,
                internal_reason="JSON_DEPTH_LIMIT",
            )
        if isinstance(value, str):
            if len(value) > _MAX_STRING_LEN:
                raise_download_error(
                    DownloadErrorCode.MUXED_OUTPUT_INVALID,
                    internal_reason="JSON_STRING_LIMIT",
                )
            return
        if isinstance(value, dict):
            if len(value) > _MAX_MAPPING_KEYS:
                raise_download_error(
                    DownloadErrorCode.MUXED_OUTPUT_INVALID,
                    internal_reason="JSON_KEY_LIMIT",
                )
            for nested in value.values():
                walk(nested, depth + 1)
            return
        if isinstance(value, list):
            if len(value) > _MAX_LIST_LEN:
                raise_download_error(
                    DownloadErrorCode.MUXED_OUTPUT_INVALID,
                    internal_reason="JSON_LIST_LIMIT",
                )
            for nested in value:
                walk(nested, depth + 1)
            return
        if isinstance(value, bool):
            return
        if isinstance(value, int):
            return
        if isinstance(value, float):
            if not math.isfinite(value):
                raise_download_error(
                    DownloadErrorCode.MUXED_OUTPUT_INVALID,
                    internal_reason="JSON_NONFINITE",
                )
            return
        if value is None:
            return
        raise_download_error(
            DownloadErrorCode.MUXED_OUTPUT_INVALID,
            internal_reason="JSON_TYPE",
        )

    walk(root, 0)


def parse_ffprobe_json(
    raw: bytes,
    *,
    expected_container: str,
    expected_size_bytes: int,
    expected_duration_seconds: float | None,
    duration_tolerance_seconds: float,
    max_bytes: int,
    max_json_bytes: int = _MAX_JSON_BYTES,
) -> MuxProbeResult:
    """Parse ffprobe JSON into a structural result; discard the raw payload."""
    if not isinstance(raw, bytes | bytearray) or not raw:
        raise_download_error(
            DownloadErrorCode.MUXED_OUTPUT_INVALID,
            internal_reason="PROBE_EMPTY",
        )
    json_limit = min(_MAX_JSON_BYTES, int(max_json_bytes))
    if json_limit < 1 or len(raw) > json_limit:
        raise_download_error(
            DownloadErrorCode.MUXED_OUTPUT_INVALID,
            internal_reason="PROBE_TOO_LARGE",
        )
    try:
        payload: Any = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise_download_error(
            DownloadErrorCode.MUXED_OUTPUT_INVALID,
            internal_reason="PROBE_JSON",
        )
    # Drop the original buffer reference as soon as JSON is loaded.
    del raw
    _walk_limits(payload)
    if not isinstance(payload, dict):
        raise_download_error(
            DownloadErrorCode.MUXED_OUTPUT_INVALID,
            internal_reason="PROBE_ROOT",
        )
    streams = payload.get("streams")
    fmt = payload.get("format")
    if not isinstance(streams, list) or not isinstance(fmt, dict):
        raise_download_error(
            DownloadErrorCode.MUXED_OUTPUT_INVALID,
            internal_reason="PROBE_SHAPE",
        )
    if len(streams) > _MAX_STREAMS:
        raise_download_error(
            DownloadErrorCode.MUXED_OUTPUT_INVALID,
            internal_reason="PROBE_STREAM_LIMIT",
        )
    video = 0
    audio = 0
    other = 0
    allowed_video, allowed_audio = _CONTAINER_CODECS.get(
        expected_container, (frozenset(), frozenset())
    )
    if not allowed_video or not allowed_audio:
        raise_download_error(
            DownloadErrorCode.MUXED_OUTPUT_INVALID,
            internal_reason="PROBE_CONTAINER",
        )
    for stream in streams:
        if not isinstance(stream, dict):
            raise_download_error(
                DownloadErrorCode.MUXED_OUTPUT_INVALID,
                internal_reason="PROBE_STREAM_TYPE",
            )
        codec_type = str(stream.get("codec_type") or "").lower()
        codec_name = str(stream.get("codec_name") or "").lower()
        if codec_type == "video":
            video += 1
            if codec_name not in allowed_video:
                raise_download_error(
                    DownloadErrorCode.MUXED_OUTPUT_INVALID,
                    internal_reason="PROBE_VIDEO_CODEC",
                )
        elif codec_type == "audio":
            audio += 1
            if codec_name not in allowed_audio:
                raise_download_error(
                    DownloadErrorCode.MUXED_OUTPUT_INVALID,
                    internal_reason="PROBE_AUDIO_CODEC",
                )
        else:
            other += 1
    if video != 1 or audio != 1 or other != 0:
        raise_download_error(
            DownloadErrorCode.MUXED_OUTPUT_INVALID,
            internal_reason="PROBE_STREAMS",
        )
    format_name = str(fmt.get("format_name") or "").lower()
    allowed = _CONTAINER_HINTS.get(expected_container, frozenset())
    names = {part.strip() for part in format_name.split(",") if part.strip()}
    if not allowed or names.isdisjoint(allowed):
        raise_download_error(
            DownloadErrorCode.MUXED_OUTPUT_INVALID,
            internal_reason="PROBE_CONTAINER",
        )
    duration_raw = fmt.get("duration")
    try:
        duration = float(duration_raw) if duration_raw is not None else float("nan")
    except (TypeError, ValueError):
        duration = float("nan")
    if not math.isfinite(duration) or duration < 0:
        raise_download_error(
            DownloadErrorCode.MUXED_OUTPUT_INVALID,
            internal_reason="PROBE_DURATION",
        )
    if expected_duration_seconds is not None:
        delta = abs(duration - float(expected_duration_seconds))
        if delta > float(duration_tolerance_seconds):
            raise_download_error(
                DownloadErrorCode.MUXED_OUTPUT_INVALID,
                internal_reason="PROBE_DURATION_MISMATCH",
            )
    if expected_size_bytes <= 0 or expected_size_bytes > max_bytes:
        raise_download_error(
            DownloadErrorCode.MUXED_OUTPUT_INVALID,
            internal_reason="PROBE_SIZE",
        )
    size_raw = fmt.get("size")
    if size_raw is None:
        reported = expected_size_bytes
    else:
        try:
            reported = int(size_raw)
        except (TypeError, ValueError):
            raise_download_error(
                DownloadErrorCode.MUXED_OUTPUT_INVALID,
                internal_reason="PROBE_SIZE",
            )
        if reported != expected_size_bytes:
            raise_download_error(
                DownloadErrorCode.MUXED_OUTPUT_INVALID,
                internal_reason="PROBE_SIZE_MISMATCH",
            )
    if reported <= 0 or reported > max_bytes:
        raise_download_error(
            DownloadErrorCode.MUXED_OUTPUT_INVALID,
            internal_reason="PROBE_SIZE",
        )
    return MuxProbeResult(
        duration_seconds=duration,
        video_streams=video,
        audio_streams=audio,
        other_streams=other,
        format_name=format_name,
        size_bytes=reported,
    )
