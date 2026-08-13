"""Deterministic mux pairing proofs. Tokens/URLs never appear in public models."""

from __future__ import annotations

import copy
from itertools import permutations

import pytest

from fetchnow.downloads.errors import DownloadError, DownloadErrorCode
from fetchnow.downloads.selection import (
    MuxedDownloadSelection,
    resolve_selection_from_draft,
)
from fetchnow.media_inspection.models import (
    CodecFamily,
    ExtractedMediaDraft,
    FormatCategory,
    InternalFormatCandidate,
)
from fetchnow.media_inspection.mux_pairing import derive_mux_options, mux_option_id_for
from fetchnow.media_inspection.normalize import project_metadata
from fetchnow.media_inspection.ytdlp_parse import parse_ytdlp_json
from media_inspection_fixtures import (
    VK_FIXTURE,
    YANDEX_PREVIEW_VK_SPLIT_FIXTURE,
    dumps_fixture,
)
from media_inspection_helpers import settings

_SECRET_VIDEO = "url720-secret-token"
_SECRET_AUDIO = "audio-aac-secret"


def _mux_settings(**overrides: object):
    return settings(MEDIA_MUXING_ENABLED=True, **overrides)


def _video(
    *,
    height: int = 720,
    width: int = 1280,
    token: str = _SECRET_VIDEO,
    codec: CodecFamily = CodecFamily.AVC,
    container: str = "mp4",
    protocol: str | None = "https",
    has_drm: bool = False,
    approx: int = 8_000_000,
) -> InternalFormatCandidate:
    return InternalFormatCandidate(
        container=container,
        width=width,
        height=height,
        fps=30.0,
        has_video=True,
        has_audio=False,
        video_codec=codec,
        audio_codec=CodecFamily.NONE,
        approx_bytes=approx,
        provider_format_token=token,
        protocol=protocol,
        has_drm=has_drm,
    )


def _audio(
    *,
    token: str = _SECRET_AUDIO,
    codec: CodecFamily = CodecFamily.AAC,
    container: str = "m4a",
    protocol: str | None = "https",
    has_drm: bool = False,
    approx: int = 400_000,
) -> InternalFormatCandidate:
    return InternalFormatCandidate(
        container=container,
        width=None,
        height=None,
        fps=None,
        has_video=False,
        has_audio=True,
        video_codec=CodecFamily.NONE,
        audio_codec=codec,
        approx_bytes=approx,
        provider_format_token=token,
        protocol=protocol,
        has_drm=has_drm,
    )


def _assert_no_secrets(blob: str) -> None:
    lowered = blob.lower()
    assert _SECRET_VIDEO.lower() not in lowered
    assert _SECRET_AUDIO.lower() not in lowered
    assert "https://example.invalid" not in lowered
    assert "format_id" not in lowered


def test_disabled_by_default_yields_no_derived_options() -> None:
    opts = derive_mux_options((_video(), _audio()), settings())
    assert opts == ()


def test_permutation_invariant_derived_ids() -> None:
    videos = (
        _video(height=360, width=640, token="v360"),
        _video(height=480, width=854, token="v480"),
        _video(height=720, width=1280, token="v720"),
    )
    audios = (_audio(token="a1", approx=400_000), _audio(token="a2", approx=800_000))
    ids: set[tuple[str, ...]] = set()
    for perm in permutations((*videos, *audios)):
        derived = derive_mux_options(perm, _mux_settings())
        ids.add(tuple(item.public.format_option_id for item in derived))
    assert len(ids) == 1
    only = next(iter(ids))
    assert len(only) == 3


def test_free_tier_caps_at_720() -> None:
    derived = derive_mux_options(
        (
            _video(height=360, width=640, token="v360"),
            _video(height=720, width=1280, token="v720"),
            _video(height=1080, width=1920, token="v1080"),
            _audio(),
        ),
        _mux_settings(),
    )
    heights = {item.public.height for item in derived}
    assert 1080 not in heights
    assert heights <= {360, 720}
    assert all(item.public.free_tier_eligible for item in derived)
    assert all(item.public.has_video and item.public.has_audio for item in derived)
    assert all(item.public.category is FormatCategory.PROGRESSIVE for item in derived)


def test_incompatible_and_unknown_codecs_not_paired() -> None:
    derived = derive_mux_options(
        (
            _video(codec=CodecFamily.HEVC, token="hevc"),
            _video(codec=CodecFamily.UNKNOWN, token="unk"),
            _audio(codec=CodecFamily.MP3, container="mp3", token="mp3"),
            _audio(codec=CodecFamily.UNKNOWN, token="aunk"),
        ),
        _mux_settings(),
    )
    assert derived == ()


def test_cross_container_avc_opus_not_paired() -> None:
    derived = derive_mux_options(
        (_video(), _audio(codec=CodecFamily.OPUS, container="webm", token="opus")),
        _mux_settings(),
    )
    assert derived == ()


def test_container_family_not_mixed() -> None:
    derived = derive_mux_options(
        (
            _video(codec=CodecFamily.AVC, container="webm", token="avc-webm"),
            _audio(codec=CodecFamily.AAC, container="m4a"),
        ),
        _mux_settings(),
    )
    assert derived == ()
    derived_webm_aac = derive_mux_options(
        (
            _video(codec=CodecFamily.VP9, container="webm", token="vp9"),
            _audio(codec=CodecFamily.AAC, container="m4a"),
        ),
        _mux_settings(),
    )
    assert derived_webm_aac == ()


def test_manifest_drm_rejected() -> None:
    derived = derive_mux_options(
        (
            _video(protocol="m3u8_native", token="hls"),
            _video(has_drm=True, token="drmv"),
            _audio(),
            _audio(has_drm=True, token="drma"),
        ),
        _mux_settings(),
    )
    assert derived == ()


def test_duplicate_candidates_stable() -> None:
    left = _video()
    right = _video()
    audio = _audio()
    a = derive_mux_options((left, right, audio), _mux_settings())
    b = derive_mux_options((right, audio, left), _mux_settings())
    assert [item.public.format_option_id for item in a] == [
        item.public.format_option_id for item in b
    ]


def test_option_id_changes_if_either_stream_changes() -> None:
    video = _video()
    audio = _audio()
    base = mux_option_id_for(video, audio, output_container="mp4", quality_label="p720")
    other_video = mux_option_id_for(
        _video(token="other-video"),
        audio,
        output_container="mp4",
        quality_label="p720",
    )
    other_audio = mux_option_id_for(
        video,
        _audio(token="other-audio"),
        output_container="mp4",
        quality_label="p720",
    )
    assert base != other_video
    assert base != other_audio


def test_public_models_and_repr_hide_tokens() -> None:
    derived = derive_mux_options((_video(), _audio()), _mux_settings())
    assert derived
    public = derived[0].public
    _assert_no_secrets(repr(derived[0]))
    _assert_no_secrets(repr(public))
    dumped = public.format_option_id + public.quality_label + public.container
    _assert_no_secrets(dumped)


def test_yandex_vk_split_fixture_derives_free_options() -> None:
    s = _mux_settings()
    draft = parse_ytdlp_json(
        dumps_fixture(YANDEX_PREVIEW_VK_SPLIT_FIXTURE),
        expected_provider_id="vk",
        expected_canonical_url="https://vk.com/video-123_456239017",
        expected_media_id="-123_456239017",
        allowed_extractor_keys=frozenset({"vk"}),
        allowed_hostnames=frozenset({"vk.com"}),
        max_height=s.media_inspection_max_height,
        max_width=s.media_inspection_max_width,
        max_bytes=s.max_source_file_bytes,
        max_duration=s.max_source_duration_seconds,
        tool_version="2026.7.4",
        max_json_depth=12,
        max_json_nodes=8192,
        max_format_entries=256,
    )
    permuted = copy.deepcopy(YANDEX_PREVIEW_VK_SPLIT_FIXTURE)
    formats = list(permuted["formats"])  # type: ignore[arg-type]
    formats.reverse()
    permuted["formats"] = formats
    draft_rev = parse_ytdlp_json(
        dumps_fixture(permuted),
        expected_provider_id="vk",
        expected_canonical_url="https://vk.com/video-123_456239017",
        expected_media_id="-123_456239017",
        allowed_extractor_keys=frozenset({"vk"}),
        allowed_hostnames=frozenset({"vk.com"}),
        max_height=s.media_inspection_max_height,
        max_width=s.media_inspection_max_width,
        max_bytes=s.max_source_file_bytes,
        max_duration=s.max_source_duration_seconds,
        tool_version="2026.7.4",
        max_json_depth=12,
        max_json_nodes=8192,
        max_format_entries=256,
    )
    meta = project_metadata(
        draft,
        s,
        trusted_canonical_url=draft.canonical_provider_url,
        trusted_media_id=draft.media_id,
        trusted_provider_id=draft.provider_id,
    )
    meta_rev = project_metadata(
        draft_rev,
        s,
        trusted_canonical_url=draft_rev.canonical_provider_url,
        trusted_media_id=draft_rev.media_id,
        trusted_provider_id=draft_rev.provider_id,
    )
    assert meta.muxing_required is False
    executable = [
        fmt
        for fmt in meta.formats
        if fmt.free_tier_eligible
        and fmt.category is FormatCategory.PROGRESSIVE
        and fmt.has_video
        and fmt.has_audio
    ]
    assert executable
    assert all((fmt.height or 0) <= 720 for fmt in executable)
    assert {fmt.format_option_id for fmt in executable} == {
        fmt.format_option_id
        for fmt in meta_rev.formats
        if fmt.free_tier_eligible and fmt.category is FormatCategory.PROGRESSIVE
    }
    public_blob = repr(meta) + "".join(repr(fmt) for fmt in meta.formats)
    _assert_no_secrets(public_blob)
    assert "url720" not in public_blob
    assert "audio-aac" not in public_blob


def test_yandex_vk_split_binds_exact_tokens() -> None:
    s = _mux_settings()
    draft = parse_ytdlp_json(
        dumps_fixture(YANDEX_PREVIEW_VK_SPLIT_FIXTURE),
        expected_provider_id="vk",
        expected_canonical_url="https://vk.com/video-123_456239017",
        expected_media_id="-123_456239017",
        allowed_extractor_keys=frozenset({"vk"}),
        allowed_hostnames=frozenset({"vk.com"}),
        max_height=s.media_inspection_max_height,
        max_width=s.media_inspection_max_width,
        max_bytes=s.max_source_file_bytes,
        max_duration=s.max_source_duration_seconds,
        tool_version="2026.7.4",
        max_json_depth=12,
        max_json_nodes=8192,
        max_format_entries=256,
    )
    meta = project_metadata(
        draft,
        s,
        trusted_canonical_url=draft.canonical_provider_url,
        trusted_media_id=draft.media_id,
        trusted_provider_id=draft.provider_id,
    )
    option = next(
        fmt for fmt in meta.formats if fmt.free_tier_eligible and fmt.height == 720
    )
    selection = resolve_selection_from_draft(draft, s, option.format_option_id, option)
    assert isinstance(selection, MuxedDownloadSelection)
    assert selection.video_format_token == "url720"
    assert selection.audio_format_token == "audio-aac"
    assert "url720" not in repr(selection)
    assert "audio-aac" not in repr(selection)
    assert "url720" not in repr(option)
    drifted = tuple(
        InternalFormatCandidate(
            container=c.container,
            width=c.width,
            height=c.height,
            fps=c.fps,
            has_video=c.has_video,
            has_audio=c.has_audio,
            video_codec=c.video_codec,
            audio_codec=c.audio_codec,
            approx_bytes=c.approx_bytes,
            provider_format_token=(
                "url720-drifted"
                if c.provider_format_token == "url720"
                else c.provider_format_token
            ),
            protocol=c.protocol,
            has_drm=c.has_drm,
        )
        for c in draft.candidates
    )
    drifted_draft = ExtractedMediaDraft(
        provider_id=draft.provider_id,
        canonical_provider_url=draft.canonical_provider_url,
        media_id=draft.media_id,
        title=draft.title,
        duration_seconds=draft.duration_seconds,
        candidates=drifted,
        extractor_key=draft.extractor_key,
        tool_version=draft.tool_version,
    )
    with pytest.raises(DownloadError) as exc:
        resolve_selection_from_draft(drifted_draft, s, option.format_option_id, option)
    assert exc.value.code == DownloadErrorCode.FORMAT_UNAVAILABLE


def test_old_progressive_option_unchanged_when_muxing_on() -> None:
    s = _mux_settings()
    draft = parse_ytdlp_json(
        dumps_fixture(VK_FIXTURE),
        expected_provider_id="vk",
        expected_canonical_url="https://vk.com/video-123_456239017",
        expected_media_id="-123_456239017",
        allowed_extractor_keys=frozenset({"vk"}),
        allowed_hostnames=frozenset({"vk.com"}),
        max_height=s.media_inspection_max_height,
        max_width=s.media_inspection_max_width,
        max_bytes=s.max_source_file_bytes,
        max_duration=s.max_source_duration_seconds,
        tool_version="2026.7.4",
        max_json_depth=12,
        max_json_nodes=8192,
        max_format_entries=256,
    )
    off = project_metadata(
        draft,
        settings(),
        trusted_canonical_url=draft.canonical_provider_url,
        trusted_media_id=draft.media_id,
        trusted_provider_id=draft.provider_id,
    )
    on = project_metadata(
        draft,
        s,
        trusted_canonical_url=draft.canonical_provider_url,
        trusted_media_id=draft.media_id,
        trusted_provider_id=draft.provider_id,
    )
    progressive_off = [
        f.format_option_id
        for f in off.formats
        if f.category is FormatCategory.PROGRESSIVE and f.free_tier_eligible
    ]
    progressive_on = [
        f.format_option_id
        for f in on.formats
        if f.category is FormatCategory.PROGRESSIVE and f.free_tier_eligible
    ]
    assert progressive_off
    assert progressive_off == progressive_on
    assert off.muxing_required is False
    assert on.muxing_required is False


def test_resolve_muxed_selection_hides_tokens() -> None:
    video = _video()
    audio = _audio()
    derived = derive_mux_options((video, audio), _mux_settings())
    public = derived[0].public
    draft = ExtractedMediaDraft(
        provider_id="vk",
        canonical_provider_url="https://vk.com/video-1_2",
        media_id="-1_2",
        title="x",
        duration_seconds=10,
        candidates=(video, audio),
        extractor_key="vk",
        tool_version="2026.7.4",
    )
    selection = resolve_selection_from_draft(
        draft, _mux_settings(), public.format_option_id, public
    )
    assert isinstance(selection, MuxedDownloadSelection)
    _assert_no_secrets(repr(selection))
    with pytest.raises(DownloadError) as exc:
        resolve_selection_from_draft(draft, settings(), public.format_option_id, public)
    assert exc.value.code == DownloadErrorCode.FORMAT_UNAVAILABLE
