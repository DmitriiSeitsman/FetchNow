"""Selection binding tests for exact formatOptionId → token resolution."""

from __future__ import annotations

import pytest

from fetchnow.core.config import Settings
from fetchnow.downloads.errors import DownloadError, DownloadErrorCode
from fetchnow.downloads.selection import resolve_selection_from_draft
from fetchnow.media_inspection.models import (
    CodecFamily,
    ExtractedMediaDraft,
    FormatCategory,
    InternalFormatCandidate,
    MediaFormat,
)
from fetchnow.media_inspection.normalize import format_option_id_for, quality_label_for

_DB = "postgresql+asyncpg://fetchnow:fetchnow@localhost:5432/fetchnow"
_SECRET_TOKEN = "provider-secret-token-xyz"


def _settings() -> Settings:
    return Settings(
        APP_ENV="test",
        DATABASE_URL=_DB,
        MAX_SOURCE_FILE_BYTES=10**9,
        MEDIA_DOWNLOAD_MAX_BYTES=10**9,
        MEDIA_INSPECTION_MAX_HEIGHT=2160,
        MEDIA_INSPECTION_MAX_WIDTH=3840,
    )


def _candidate(
    *,
    height: int = 720,
    width: int = 1280,
    token: str | None = _SECRET_TOKEN,
    has_video: bool = True,
    has_audio: bool = True,
    container: str = "mp4",
) -> InternalFormatCandidate:
    return InternalFormatCandidate(
        container=container,
        width=width,
        height=height,
        fps=30.0,
        has_video=has_video,
        has_audio=has_audio,
        video_codec=CodecFamily.AVC if has_video else CodecFamily.NONE,
        audio_codec=CodecFamily.AAC if has_audio else CodecFamily.NONE,
        approx_bytes=1_000_000,
        provider_format_token=token,
    )


def _persisted(candidate: InternalFormatCandidate) -> MediaFormat:
    label = quality_label_for(candidate.height, has_video=candidate.has_video)
    option_id = format_option_id_for(candidate, label)
    category = (
        FormatCategory.PROGRESSIVE
        if candidate.has_video and candidate.has_audio
        else (
            FormatCategory.VIDEO_ONLY
            if candidate.has_video
            else FormatCategory.AUDIO_ONLY
        )
    )
    return MediaFormat(
        format_option_id=option_id,
        container=candidate.container,
        width=candidate.width,
        height=candidate.height,
        fps=candidate.fps,
        has_video=candidate.has_video,
        has_audio=candidate.has_audio,
        category=category,
        video_codec=candidate.video_codec,
        audio_codec=candidate.audio_codec,
        approx_bytes=candidate.approx_bytes,
        quality_label=label,
        free_tier_eligible=bool(
            candidate.has_video
            and candidate.has_audio
            and candidate.height is not None
            and candidate.height <= 720
        ),
    )


def _draft(*candidates: InternalFormatCandidate) -> ExtractedMediaDraft:
    return ExtractedMediaDraft(
        provider_id="vk",
        canonical_provider_url="https://vk.com/video-1_2",
        media_id="-1_2",
        title=None,
        duration_seconds=60,
        candidates=candidates,
        extractor_key="vk",
        tool_version="2024.01.01",
    )


def test_exact_option_resolves_token() -> None:
    candidate = _candidate()
    persisted = _persisted(candidate)
    selection = resolve_selection_from_draft(
        _draft(candidate),
        _settings(),
        persisted.format_option_id,
        persisted,
    )
    assert selection.format_option_id == persisted.format_option_id
    assert selection.provider_format_token == _SECRET_TOKEN
    assert _SECRET_TOKEN not in repr(selection)
    assert _SECRET_TOKEN not in str(selection)


def test_property_drift_fails() -> None:
    candidate = _candidate(height=720)
    persisted = _persisted(candidate)
    bad_persisted = MediaFormat(
        format_option_id=persisted.format_option_id,
        container=persisted.container,
        width=persisted.width,
        height=480,
        fps=persisted.fps,
        has_video=persisted.has_video,
        has_audio=persisted.has_audio,
        category=persisted.category,
        video_codec=persisted.video_codec,
        audio_codec=persisted.audio_codec,
        approx_bytes=persisted.approx_bytes,
        quality_label=persisted.quality_label,
        free_tier_eligible=persisted.free_tier_eligible,
    )
    with pytest.raises(DownloadError) as exc:
        resolve_selection_from_draft(
            _draft(candidate),
            _settings(),
            persisted.format_option_id,
            bad_persisted,
        )
    assert exc.value.code == DownloadErrorCode.FORMAT_UNAVAILABLE
    assert _SECRET_TOKEN not in repr(exc.value)


def test_ineligible_fails() -> None:
    candidate = _candidate(height=1080)
    persisted = _persisted(candidate)
    assert persisted.free_tier_eligible is False
    with pytest.raises(DownloadError) as exc:
        resolve_selection_from_draft(
            _draft(candidate),
            _settings(),
            persisted.format_option_id,
            persisted,
        )
    assert exc.value.code == DownloadErrorCode.FORMAT_UNAVAILABLE


def test_audio_only_ineligible() -> None:
    candidate = InternalFormatCandidate(
        container="m4a",
        width=None,
        height=None,
        fps=None,
        has_video=False,
        has_audio=True,
        video_codec=CodecFamily.NONE,
        audio_codec=CodecFamily.AAC,
        approx_bytes=1000,
        provider_format_token=_SECRET_TOKEN,
    )
    persisted = _persisted(candidate)
    with pytest.raises(DownloadError) as exc:
        resolve_selection_from_draft(
            _draft(candidate),
            _settings(),
            persisted.format_option_id,
            persisted,
        )
    assert exc.value.code == DownloadErrorCode.FORMAT_UNAVAILABLE


def test_permutation_invariant_option_ids() -> None:
    a = _candidate(height=360, token="tok-a")
    b = _candidate(height=720, token="tok-b")
    c = _candidate(height=480, width=854, token="tok-c")
    drafts = (
        _draft(a, b, c),
        _draft(c, b, a),
        _draft(b, a, c),
        _draft(c, a, b),
    )
    persisted_b = _persisted(b)
    selections = [
        resolve_selection_from_draft(
            draft, _settings(), persisted_b.format_option_id, persisted_b
        )
        for draft in drafts
    ]
    assert {s.provider_format_token for s in selections} == {"tok-b"}
    assert len({s.format_option_id for s in selections}) == 1
    assert all(s.format_option_id == persisted_b.format_option_id for s in selections)


def test_ambiguous_duplicate_option_ids_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "fetchnow.downloads.selection.format_option_id_for",
        lambda _candidate, _label: "fmt_collision_option_id",
    )
    left = _candidate(height=720, token="tok-left", width=1280)
    right = _candidate(height=720, token="tok-right", width=1280)
    persisted = MediaFormat(
        format_option_id="fmt_collision_option_id",
        container=left.container,
        width=left.width,
        height=left.height,
        fps=left.fps,
        has_video=left.has_video,
        has_audio=left.has_audio,
        category=FormatCategory.PROGRESSIVE,
        video_codec=left.video_codec,
        audio_codec=left.audio_codec,
        approx_bytes=left.approx_bytes,
        quality_label="p720",
        free_tier_eligible=True,
    )
    with pytest.raises(DownloadError) as exc:
        resolve_selection_from_draft(
            _draft(left, right),
            _settings(),
            "fmt_collision_option_id",
            persisted,
        )
    assert exc.value.code == DownloadErrorCode.FORMAT_UNAVAILABLE
    assert "tok-left" not in repr(exc.value)
    assert "tok-right" not in repr(exc.value)


def test_reserved_selector_token_rejected_via_selection() -> None:
    candidate = _candidate(token="best")
    persisted = _persisted(candidate)
    with pytest.raises(DownloadError) as exc:
        resolve_selection_from_draft(
            _draft(candidate),
            _settings(),
            persisted.format_option_id,
            persisted,
        )
    assert exc.value.code == DownloadErrorCode.FORMAT_UNAVAILABLE
    assert "best" not in repr(exc.value)
