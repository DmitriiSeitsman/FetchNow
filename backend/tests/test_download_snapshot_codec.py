"""Strict selected-format snapshot codec tests."""

from __future__ import annotations

import pytest

from fetchnow.downloads.errors import DownloadError, DownloadErrorCode
from fetchnow.downloads.snapshot_codec import (
    decode_selected_format_snapshot,
    encode_selected_format_snapshot,
)
from fetchnow.media_inspection.models import (
    CodecFamily,
    FormatCategory,
    MediaFormat,
)


def _format(**overrides: object) -> MediaFormat:
    base = dict(
        format_option_id="fmt_abc123def4567890abcdef123456",
        container="mp4",
        width=1280,
        height=720,
        fps=30.0,
        has_video=True,
        has_audio=True,
        category=FormatCategory.PROGRESSIVE,
        video_codec=CodecFamily.AVC,
        audio_codec=CodecFamily.AAC,
        approx_bytes=1_000_000,
        quality_label="p720",
        free_tier_eligible=True,
    )
    base.update(overrides)
    return MediaFormat(**base)  # type: ignore[arg-type]


def _payload(**overrides: object) -> dict[str, object]:
    encoded = encode_selected_format_snapshot(_format())
    encoded.update(overrides)
    return encoded


def test_roundtrip_encode_decode() -> None:
    fmt = _format()
    payload = encode_selected_format_snapshot(fmt)
    decoded = decode_selected_format_snapshot(
        payload, expected_format_option_id=fmt.format_option_id
    )
    assert decoded.format_option_id == fmt.format_option_id
    assert decoded.has_video is True
    assert decoded.free_tier_eligible is True


def test_string_false_for_has_video_rejected() -> None:
    payload = _payload(hasVideo="false")
    with pytest.raises(DownloadError) as exc:
        decode_selected_format_snapshot(
            payload, expected_format_option_id=str(payload["formatOptionId"])
        )
    assert exc.value.code == DownloadErrorCode.FORMAT_UNAVAILABLE


def test_exact_bool_required_for_flags() -> None:
    for field, bad in (
        ("hasVideo", 1),
        ("hasAudio", 0),
        ("freeTierEligible", "true"),
    ):
        payload = _payload(**{field: bad})
        with pytest.raises(DownloadError) as exc:
            decode_selected_format_snapshot(
                payload, expected_format_option_id=str(payload["formatOptionId"])
            )
        assert exc.value.code == DownloadErrorCode.FORMAT_UNAVAILABLE


def test_unknown_keys_rejected() -> None:
    payload = _payload(extraField="nope")
    with pytest.raises(DownloadError) as exc:
        decode_selected_format_snapshot(
            payload, expected_format_option_id=str(payload["formatOptionId"])
        )
    assert exc.value.code == DownloadErrorCode.FORMAT_UNAVAILABLE


def test_option_id_mismatch_rejected() -> None:
    payload = _payload()
    with pytest.raises(DownloadError) as exc:
        decode_selected_format_snapshot(
            payload, expected_format_option_id="fmt_other_option_id_xxxxx"
        )
    assert exc.value.code == DownloadErrorCode.FORMAT_UNAVAILABLE
