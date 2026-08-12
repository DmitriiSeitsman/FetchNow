"""Output normalization, JSON limits, and format policy tests."""

from __future__ import annotations

import copy
import math
from itertools import permutations

import pytest

from fetchnow.media_inspection.errors import InspectionError, InspectionErrorKind
from fetchnow.media_inspection.models import (
    CodecFamily,
    FormatCategory,
    InternalFormatCandidate,
    MediaFormat,
    sanitize_title,
)
from fetchnow.media_inspection.normalize import (
    format_option_id_for,
    project_metadata,
)
from fetchnow.media_inspection.ytdlp_parse import parse_ytdlp_json
from media_inspection_fixtures import RUTUBE_FIXTURE, VK_FIXTURE, dumps_fixture
from media_inspection_helpers import settings


def _parse(payload: dict, **kwargs: object):
    s = settings()
    return parse_ytdlp_json(
        dumps_fixture(payload),
        expected_provider_id=str(kwargs.get("provider_id", "vk")),
        expected_canonical_url=str(
            kwargs.get("canonical", "https://vk.com/video-123_456239017")
        ),
        expected_media_id=str(kwargs.get("media_id", "-123_456239017")),
        allowed_extractor_keys=frozenset(
            kwargs.get("extractors", frozenset({"vk"}))  # type: ignore[arg-type]
        ),
        allowed_hostnames=frozenset(
            kwargs.get(
                "hosts",
                frozenset(
                    {
                        "vk.com",
                        "www.vk.com",
                        "m.vk.com",
                        "vkvideo.ru",
                        "www.vkvideo.ru",
                        "m.vkvideo.ru",
                        "rutube.ru",
                        "www.rutube.ru",
                    }
                ),
            )  # type: ignore[arg-type]
        ),
        max_height=s.media_inspection_max_height,
        max_width=s.media_inspection_max_width,
        max_bytes=s.max_source_file_bytes,
        max_duration=s.max_source_duration_seconds,
        tool_version="2026.7.4",
        max_json_depth=int(kwargs.get("max_json_depth", 12)),
        max_json_nodes=int(kwargs.get("max_json_nodes", 8192)),
        max_format_entries=int(kwargs.get("max_format_entries", 256)),
    )


def _project(draft, **overrides: object):
    return project_metadata(
        draft,
        settings(**overrides),
        trusted_canonical_url=draft.canonical_provider_url,
        trusted_media_id=draft.media_id,
        trusted_provider_id=draft.provider_id,
    )


def test_vk_fixture_normalization() -> None:
    draft = _parse(VK_FIXTURE)
    meta = _project(draft)
    assert meta.provider_id == "vk"
    assert meta.media_id == "-123_456239017"
    assert meta.title == "Sample VK clip"
    assert "p720" in [f.quality_label for f in meta.formats]
    progressive = [f for f in meta.formats if f.has_video and f.has_audio]
    heights = [f.height for f in progressive]
    assert heights == sorted(heights, reverse=True)
    for fmt in meta.formats:
        if fmt.height and fmt.height > 720 and fmt.has_video and fmt.has_audio:
            assert fmt.free_tier_eligible is False
        if fmt.free_tier_eligible:
            assert fmt.height is not None and fmt.height <= 720
            assert fmt.has_video and fmt.has_audio
    blob = repr(meta) + "".join(repr(f) for f in meta.formats)
    assert "example.invalid" not in blob
    assert "PLACEHOLDER" not in blob


def test_rutube_fixture_normalization() -> None:
    draft = _parse(
        RUTUBE_FIXTURE,
        provider_id="rutube",
        canonical="https://rutube.ru/video/abc123def456/",
        media_id="abc123def456",
        extractors=frozenset({"rutube"}),
    )
    meta = _project(draft)
    assert meta.provider_id == "rutube"
    assert meta.muxing_required is False


def test_format_output_permutation_invariant() -> None:
    formats = [
        {
            "format_id": "a",
            "ext": "mp4",
            "width": 1280,
            "height": 720,
            "fps": 30,
            "vcodec": "avc1",
            "acodec": "mp4a",
            "filesize": 5_000_000,
            "url": "https://example.invalid/a",
        },
        {
            "format_id": "b",
            "ext": "mp4",
            "width": 1270,
            "height": 720,
            "fps": 25,
            "vcodec": "avc1",
            "acodec": "mp4a",
            "filesize": 4_000_000,
            "url": "https://example.invalid/b",
        },
        {
            "format_id": "c",
            "ext": "mp4",
            "width": 1280,
            "height": 720,
            "fps": 30,
            "vcodec": "avc1",
            "acodec": "mp4a",
            "filesize": 6_000_000,
            "url": "https://example.invalid/c",
        },
    ]
    metas = []
    for ordered in permutations(formats):
        payload = copy.deepcopy(VK_FIXTURE)
        payload["formats"] = list(ordered)
        draft = _parse(payload)
        metas.append(_project(draft))
    first = metas[0]
    for meta in metas[1:]:
        assert meta.formats == first.formats
        assert [f.format_option_id for f in meta.formats] == [
            f.format_option_id for f in first.formats
        ]


def test_distinct_formats_share_option_id_false() -> None:
    c1 = InternalFormatCandidate(
        container="mp4",
        width=1280,
        height=720,
        fps=30.0,
        has_video=True,
        has_audio=True,
        video_codec=CodecFamily.AVC,
        audio_codec=CodecFamily.AAC,
        approx_bytes=1_000_000,
        provider_format_token="url720a",
    )
    c2 = InternalFormatCandidate(
        container="mp4",
        width=1270,
        height=720,
        fps=30.0,
        has_video=True,
        has_audio=True,
        video_codec=CodecFamily.AVC,
        audio_codec=CodecFamily.AAC,
        approx_bytes=1_000_000,
        provider_format_token="url720b",
    )
    assert format_option_id_for(c1, "p720") != format_option_id_for(c2, "p720")


def test_provider_format_token_public_false() -> None:
    draft = _parse(VK_FIXTURE)
    meta = _project(draft)
    blob = repr(meta) + "".join(repr(f) for f in meta.formats)
    assert "url720" not in blob
    assert "format_id" not in blob


def test_title_sanitization_and_bound() -> None:
    assert sanitize_title("  hello\x00\nworld  ", max_length=80) == "hello world"
    payload = copy.deepcopy(VK_FIXTURE)
    payload["title"] = "x" * 500
    draft = _parse(payload)
    meta = _project(draft, MEDIA_INSPECTION_MAX_TITLE_LENGTH=40)
    assert meta.title is not None and len(meta.title) == 40


def test_max_format_count() -> None:
    payload = copy.deepcopy(VK_FIXTURE)
    formats = []
    for i in range(24):
        formats.append(
            {
                "format_id": f"f{i}",
                "ext": ("mp4", "webm", "mkv")[i % 3],
                "width": (2160, 1440, 1080, 720, 480, 360, 240, 144)[i % 8],
                "height": (2160, 1440, 1080, 720, 480, 360, 240, 144)[i % 8],
                "fps": 30 + (i % 5),
                "vcodec": ("avc1", "vp9", "av01")[i % 3],
                "acodec": ("mp4a", "opus", "vorbis")[(i // 3) % 3],
                "filesize": 1000 + i,
                "url": f"https://example.invalid/{i}",
            }
        )
    payload["formats"] = formats
    draft = _parse(payload)
    meta = _project(draft, MEDIA_INSPECTION_MAX_FORMATS=5)
    assert len(meta.formats) == 5


def test_nan_infinity_negative_rejected() -> None:
    payload = copy.deepcopy(VK_FIXTURE)
    payload["formats"] = [
        {
            "format_id": "bad",
            "ext": "mp4",
            "width": 1280,
            "height": 720,
            "fps": math.nan,
            "vcodec": "avc1",
            "acodec": "mp4a",
            "url": "https://example.invalid/x",
        },
        VK_FIXTURE["formats"][0],  # type: ignore[index]
    ]
    with pytest.raises(InspectionError) as exc:
        _parse(payload)
    assert exc.value.kind is InspectionErrorKind.INSPECTION_INVALID_OUTPUT

    payload2 = copy.deepcopy(VK_FIXTURE)
    payload2["formats"] = [
        {
            "format_id": "neg",
            "ext": "mp4",
            "width": -1,
            "height": 720,
            "vcodec": "avc1",
            "acodec": "mp4a",
            "url": "https://example.invalid/y",
        },
        VK_FIXTURE["formats"][0],  # type: ignore[index]
    ]
    draft = _parse(payload2)
    meta = _project(draft)
    # Negative width is skipped at candidate parse; valid formats remain.
    assert meta.formats
    assert all((f.width or 0) >= 0 for f in meta.formats)


def test_absurd_duration_rejected() -> None:
    payload = copy.deepcopy(VK_FIXTURE)
    payload["duration"] = 10_000_000
    with pytest.raises(InspectionError) as exc:
        _parse(payload)
    assert exc.value.kind is InspectionErrorKind.INSPECTION_POLICY_REJECTED


def test_muxing_required_when_no_progressive() -> None:
    payload = copy.deepcopy(VK_FIXTURE)
    payload["formats"] = [
        {
            "format_id": "v",
            "ext": "mp4",
            "width": 1280,
            "height": 720,
            "vcodec": "avc1",
            "acodec": "none",
            "url": "https://example.invalid/v",
        },
        {
            "format_id": "a",
            "ext": "m4a",
            "vcodec": "none",
            "acodec": "mp4a",
            "url": "https://example.invalid/a",
        },
    ]
    draft = _parse(payload)
    meta = _project(draft)
    assert meta.muxing_required is True
    assert all(not f.free_tier_eligible for f in meta.formats)


def test_generic_extractor_rejected() -> None:
    payload = copy.deepcopy(VK_FIXTURE)
    payload["extractor_key"] = "generic"
    payload["extractor"] = "generic"
    with pytest.raises(InspectionError) as exc:
        _parse(payload)
    assert exc.value.kind is InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH


def test_conflicting_extractor_fields_rejected() -> None:
    payload = copy.deepcopy(VK_FIXTURE)
    payload["extractor_key"] = "vk"
    payload["extractor"] = "rutube"
    with pytest.raises(InspectionError) as exc:
        _parse(payload)
    assert exc.value.kind is InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH


def test_generic_secondary_field_rejected() -> None:
    payload = copy.deepcopy(VK_FIXTURE)
    payload["extractor_key"] = "vk"
    payload["extractor"] = "generic"
    with pytest.raises(InspectionError):
        _parse(payload)


def test_playlist_rejected() -> None:
    payload = {
        "_type": "playlist",
        "entries": [],
        "extractor_key": "vk",
        "id": "-1_2",
    }
    with pytest.raises(InspectionError) as exc:
        _parse(payload, media_id="-1_2", canonical="https://vk.com/video-1_2")
    assert exc.value.kind is InspectionErrorKind.INSPECTION_POLICY_REJECTED


def test_json_depth_limit() -> None:
    payload: dict = {"extractor_key": "vk", "extractor": "vk", "id": "-123_456239017"}
    cur: dict = payload
    for _ in range(20):
        nxt: dict = {}
        cur["nested"] = nxt
        cur = nxt
    payload["webpage_url"] = "https://vk.com/video-123_456239017"
    payload["original_url"] = "https://vk.com/video-123_456239017"
    payload["formats"] = [VK_FIXTURE["formats"][0]]  # type: ignore[index]
    with pytest.raises(InspectionError) as exc:
        _parse(payload, max_json_depth=4)
    assert exc.value.kind is InspectionErrorKind.INSPECTION_INVALID_OUTPUT


def test_json_node_limit() -> None:
    payload = copy.deepcopy(VK_FIXTURE)
    payload["noise"] = {f"k{i}": i for i in range(100)}
    with pytest.raises(InspectionError) as exc:
        _parse(payload, max_json_nodes=20)
    assert exc.value.kind is InspectionErrorKind.INSPECTION_INVALID_OUTPUT


def test_format_entry_limit() -> None:
    payload = copy.deepcopy(VK_FIXTURE)
    payload["formats"] = [
        {
            "format_id": f"f{i}",
            "ext": "mp4",
            "width": 640,
            "height": 360,
            "vcodec": "avc1",
            "acodec": "mp4a",
            "url": f"https://example.invalid/{i}",
        }
        for i in range(30)
    ]
    with pytest.raises(InspectionError) as exc:
        _parse(payload, max_format_entries=5)
    assert exc.value.kind is InspectionErrorKind.INSPECTION_INVALID_OUTPUT


def test_malformed_utf8_and_json() -> None:
    with pytest.raises(InspectionError) as exc:
        parse_ytdlp_json(
            b"\xff\xfe",
            expected_provider_id="vk",
            expected_canonical_url="https://vk.com/video-1_2",
            expected_media_id="-1_2",
            allowed_extractor_keys=frozenset({"vk"}),
            allowed_hostnames=frozenset({"vk.com"}),
            max_height=2160,
            max_width=3840,
            max_bytes=10**9,
            max_duration=3600,
            tool_version=None,
        )
    assert exc.value.kind is InspectionErrorKind.INSPECTION_INVALID_OUTPUT


def test_format_repr_hides_secrets() -> None:
    fmt = MediaFormat(
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
        approx_bytes=100,
        quality_label="p720",
        free_tier_eligible=True,
    )
    assert "url720" not in repr(fmt)
