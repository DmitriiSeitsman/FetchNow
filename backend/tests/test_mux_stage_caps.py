"""yt-dlp stage caps need headroom over the reserved approximate stage size."""

from __future__ import annotations

from fetchnow.downloads.executor import (
    _TOOL_CAP_MIN_PAD_BYTES,
    _mux_stage_byte_caps,
    _peak_reservation_bytes,
    _stage_cap,
    _ytdlp_stage_cap,
)
from fetchnow.downloads.selection import MuxedDownloadSelection
from fetchnow.media_inspection.models import FormatCategory

_MAX_BYTES = 536_870_912


def _selection() -> MuxedDownloadSelection:
    return MuxedDownloadSelection(
        format_option_id="fmt_test",
        container="mp4",
        width=360,
        height=640,
        fps=30.0,
        has_video=True,
        has_audio=True,
        category=FormatCategory.PROGRESSIVE,
        quality_label="p480",
        free_tier_eligible=True,
        approx_bytes=3_294_929,
        video_approx_bytes=3_020_017,
        audio_approx_bytes=274_912,
        expected_duration_seconds=60.0,
        video_format_token="dash_sep-3",
        audio_format_token="dash_sep-8",
    )


def test_stage_cap_tracks_the_estimate_without_padding() -> None:
    assert _stage_cap(274_912, max_bytes=_MAX_BYTES) == 274_912


def test_stage_cap_unknown_or_invalid_uses_max_bytes() -> None:
    assert _stage_cap(None, max_bytes=1_000_000) == 1_000_000
    assert _stage_cap(0, max_bytes=1_000_000) == 1_000_000
    assert _stage_cap(True, max_bytes=1_000_000) == 1_000_000


def test_stage_cap_never_exceeds_max_bytes() -> None:
    assert _stage_cap(9_000_000, max_bytes=1_000_000) == 1_000_000


def test_ytdlp_cap_pads_small_estimates_by_a_flat_floor() -> None:
    approx = 274_912
    cap = _ytdlp_stage_cap(_stage_cap(approx, _MAX_BYTES), _MAX_BYTES)
    assert cap == approx + _TOOL_CAP_MIN_PAD_BYTES


def test_ytdlp_cap_pads_large_estimates_proportionally() -> None:
    approx = 40_000_000
    cap = _ytdlp_stage_cap(_stage_cap(approx, _MAX_BYTES), _MAX_BYTES)
    assert cap == 50_000_000


def test_ytdlp_cap_never_exceeds_max_bytes() -> None:
    assert _ytdlp_stage_cap(1_000_000, max_bytes=1_000_000) == 1_000_000


def test_reservation_stays_on_unpadded_estimates() -> None:
    selection = _selection()
    video_cap, audio_cap, output_cap = _mux_stage_byte_caps(selection, _MAX_BYTES)
    assert (video_cap, audio_cap) == (3_020_017, 274_912)
    assert output_cap == 3_020_017 + 274_912
    assert _peak_reservation_bytes(selection, _MAX_BYTES) == (
        video_cap + audio_cap + output_cap
    )
