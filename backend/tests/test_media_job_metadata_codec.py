"""Unit tests for sanitized MediaMetadata JSON codec."""

from __future__ import annotations

import pytest

from fetchnow.jobs.errors import JobError, JobErrorCode
from fetchnow.jobs.metadata_codec import (
    media_metadata_from_jsonable,
    media_metadata_to_jsonable,
)
from fetchnow.media_inspection.models import (
    CodecFamily,
    FormatCategory,
    MediaFormat,
    MediaMetadata,
)


def _sample_metadata() -> MediaMetadata:
    return MediaMetadata(
        provider_id="vk",
        canonical_provider_url="https://vk.com/video-123_456",
        media_id="-123_456",
        title="Sample",
        duration_seconds=12,
        formats=(
            MediaFormat(
                format_option_id="fmt_abc",
                container="mp4",
                width=640,
                height=360,
                fps=30.0,
                has_video=True,
                has_audio=True,
                category=FormatCategory.PROGRESSIVE,
                video_codec=CodecFamily.AVC,
                audio_codec=CodecFamily.AAC,
                approx_bytes=1_000_000,
                quality_label="p360",
                free_tier_eligible=True,
            ),
        ),
        extraction_tool="ytdlp",
        extraction_tool_version="2024.01.01",
        muxing_required=False,
    )


def test_roundtrip_media_metadata() -> None:
    original = _sample_metadata()
    payload = media_metadata_to_jsonable(original, max_bytes=65_536)
    restored = media_metadata_from_jsonable(payload, max_bytes=65_536)
    assert restored.provider_id == original.provider_id
    assert restored.canonical_provider_url == original.canonical_provider_url
    assert restored.media_id == original.media_id
    assert restored.title == original.title
    assert restored.duration_seconds == original.duration_seconds
    assert restored.extraction_tool == original.extraction_tool
    assert restored.muxing_required is False
    assert len(restored.formats) == 1
    assert restored.formats[0].format_option_id == "fmt_abc"
    assert "url" not in payload
    assert "directUrl" not in payload
    assert all("url" not in fmt for fmt in payload["formats"])


def test_pr8_snapshot_without_muxing_required_still_loads() -> None:
    payload = media_metadata_to_jsonable(_sample_metadata(), max_bytes=65_536)
    del payload["muxingRequired"]
    restored = media_metadata_from_jsonable(payload, max_bytes=65_536)
    assert restored.muxing_required is False
    split = dict(payload)
    split["formats"] = [
        {
            **payload["formats"][0],
            "hasAudio": False,
            "category": "video_only",
            "audioCodec": "none",
            "freeTierEligible": False,
        }
    ]
    restored_split = media_metadata_from_jsonable(split, max_bytes=65_536)
    assert restored_split.muxing_required is True


def test_byte_bound_enforced() -> None:
    metadata = _sample_metadata()
    with pytest.raises(JobError) as exc_info:
        media_metadata_to_jsonable(metadata, max_bytes=32)
    assert exc_info.value.code == JobErrorCode.INTERNAL_ERROR
    assert exc_info.value.internal_reason == "METADATA_TOO_LARGE"

    payload = media_metadata_to_jsonable(metadata, max_bytes=65_536)
    with pytest.raises(JobError) as load_exc:
        media_metadata_from_jsonable(payload, max_bytes=32)
    assert load_exc.value.code == JobErrorCode.INTERNAL_ERROR
    assert load_exc.value.internal_reason == "METADATA_TOO_LARGE"


def test_forbidden_keys_rejected() -> None:
    base = media_metadata_to_jsonable(_sample_metadata(), max_bytes=65_536)
    for key in ("url", "directUrl", "downloadUrl", "token", "accessToken", "stderr"):
        bad = dict(base)
        bad[key] = "https://evil.example/x"
        with pytest.raises(JobError) as exc_info:
            media_metadata_from_jsonable(bad, max_bytes=65_536)
        assert exc_info.value.code == JobErrorCode.INTERNAL_ERROR
        assert exc_info.value.internal_reason == "METADATA_FORBIDDEN_KEY"

    fmt_bad = dict(base)
    fmt = dict(fmt_bad["formats"][0])
    fmt["signedUrl"] = "https://cdn.example/x"
    fmt_bad["formats"] = [fmt]
    with pytest.raises(JobError) as fmt_exc:
        media_metadata_from_jsonable(fmt_bad, max_bytes=65_536)
    assert fmt_exc.value.internal_reason == "METADATA_FORBIDDEN_KEY"


def test_unknown_keys_rejected() -> None:
    base = media_metadata_to_jsonable(_sample_metadata(), max_bytes=65_536)
    bad = dict(base)
    bad["extraField"] = "nope"
    with pytest.raises(JobError) as exc_info:
        media_metadata_from_jsonable(bad, max_bytes=65_536)
    assert exc_info.value.internal_reason == "METADATA_UNKNOWN_KEY"
