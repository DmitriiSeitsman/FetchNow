"""Bounded approximate size for inspection formats.

Estimates are never exact. No extra CDN HEAD/GET is performed. Values above
the configured download limit are returned so callers can drop the option
instead of advertising an undownloadable file.
"""

from __future__ import annotations

import math
from typing import Any

# yt-dlp tbr/vbr/abr are kilobits per second. 100 Mbit/s is above any free-tier
# progressive option and still small enough that duration × rate cannot overflow
# a 64-bit byte count for the bounded inspection duration (≤ 3600 s).
MAX_BITRATE_KBPS = 100_000.0
_KBPS_TO_BYTES_PER_SEC = 1000.0 / 8.0
_MAX_INT64 = 2**63 - 1


def finite_positive_number(value: Any) -> float | None:
    """Accept finite numbers > 0; reject bool, NaN, Inf, zero, and negatives."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return float(value) if value > 0 else None
    if isinstance(value, float):
        if not math.isfinite(value) or value <= 0:
            return None
        return value
    return None


def bounded_bitrate_kbps(value: Any) -> float | None:
    rate = finite_positive_number(value)
    if rate is None or rate > MAX_BITRATE_KBPS:
        return None
    return rate


def bytes_from_reported_size(value: Any) -> int | None:
    size = finite_positive_number(value)
    if size is None:
        return None
    as_int = int(size)
    if as_int < 1 or as_int > _MAX_INT64:
        return None
    return as_int


def bytes_from_bitrate(
    *,
    duration_seconds: int | None,
    bitrate_kbps: float,
) -> int | None:
    """duration × kbps × 1000 / 8, overflow-safe. Duration must already be bounded."""
    if duration_seconds is None or type(duration_seconds) is bool:
        return None
    if not isinstance(duration_seconds, int) or duration_seconds <= 0:
        return None
    if not math.isfinite(bitrate_kbps) or bitrate_kbps <= 0:
        return None
    if bitrate_kbps > MAX_BITRATE_KBPS:
        return None
    max_rate = _MAX_INT64 / (_KBPS_TO_BYTES_PER_SEC * float(duration_seconds))
    if bitrate_kbps > max_rate:
        return None
    estimated = float(duration_seconds) * bitrate_kbps * _KBPS_TO_BYTES_PER_SEC
    if not math.isfinite(estimated) or estimated < 1:
        return None
    as_int = int(estimated)
    if as_int < 1 or as_int > _MAX_INT64:
        return None
    return as_int


def bitrate_for_stream(
    *,
    has_video: bool,
    has_audio: bool,
    tbr: Any,
    vbr: Any,
    abr: Any,
) -> float | None:
    """Progressive uses tbr; video-only vbr then tbr; audio-only abr then tbr."""
    if has_video and has_audio:
        return bounded_bitrate_kbps(tbr)
    if has_video and not has_audio:
        return bounded_bitrate_kbps(vbr) or bounded_bitrate_kbps(tbr)
    if has_audio and not has_video:
        return bounded_bitrate_kbps(abr) or bounded_bitrate_kbps(tbr)
    return None


def estimate_format_bytes(
    *,
    filesize: Any = None,
    filesize_approx: Any = None,
    tbr: Any = None,
    vbr: Any = None,
    abr: Any = None,
    has_video: bool,
    has_audio: bool,
    duration_seconds: int | None,
) -> int | None:
    """Precedence: filesize → filesize_approx → duration × bounded bitrate."""
    reported = bytes_from_reported_size(filesize)
    if reported is not None:
        return reported
    approx = bytes_from_reported_size(filesize_approx)
    if approx is not None:
        return approx
    rate = bitrate_for_stream(
        has_video=has_video,
        has_audio=has_audio,
        tbr=tbr,
        vbr=vbr,
        abr=abr,
    )
    if rate is None:
        return None
    return bytes_from_bitrate(duration_seconds=duration_seconds, bitrate_kbps=rate)


def sum_approx_bytes(left: int | None, right: int | None) -> int | None:
    """Mux estimate: video + audio when both are known. Overflow → None."""
    if left is None or right is None:
        return None
    if type(left) is bool or type(right) is bool:
        return None
    if not isinstance(left, int) or not isinstance(right, int):
        return None
    if left < 1 or right < 1:
        return None
    if left > _MAX_INT64 - right:
        return None
    total = left + right
    if total < 1 or total > _MAX_INT64:
        return None
    return total
