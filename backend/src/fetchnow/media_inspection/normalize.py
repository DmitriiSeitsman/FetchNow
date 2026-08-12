"""Normalize extractor drafts into bounded public MediaMetadata."""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING

from fetchnow.media_inspection.errors import (
    InspectionErrorKind,
    raise_inspection_error,
)
from fetchnow.media_inspection.models import (
    CodecFamily,
    ExtractedMediaDraft,
    FormatCategory,
    InternalFormatCandidate,
    MediaFormat,
    MediaMetadata,
    require_finite_non_negative,
    sanitize_title,
)

if TYPE_CHECKING:
    from fetchnow.core.config import Settings

_FREE_TIER_MAX_HEIGHT = 720
_CONTAINER_RE = re.compile(r"^[a-z0-9]{1,8}$")

_VIDEO_CODEC_MAP: dict[str, CodecFamily] = {
    "none": CodecFamily.NONE,
    "avc": CodecFamily.AVC,
    "avc1": CodecFamily.AVC,
    "h264": CodecFamily.AVC,
    "h.264": CodecFamily.AVC,
    "hevc": CodecFamily.HEVC,
    "h265": CodecFamily.HEVC,
    "h.265": CodecFamily.HEVC,
    "vp9": CodecFamily.VP9,
    "vp09": CodecFamily.VP9,
    "av01": CodecFamily.AV1,
    "av1": CodecFamily.AV1,
}

_AUDIO_CODEC_MAP: dict[str, CodecFamily] = {
    "none": CodecFamily.NONE,
    "aac": CodecFamily.AAC,
    "mp4a": CodecFamily.AAC,
    "mp3": CodecFamily.MP3,
    "opus": CodecFamily.OPUS,
    "vorbis": CodecFamily.VORBIS,
    "flac": CodecFamily.FLAC,
}


def normalize_codec(raw: str | None, *, kind: str) -> CodecFamily:
    if raw is None:
        return CodecFamily.NONE
    if not isinstance(raw, str):
        return CodecFamily.UNKNOWN
    token = raw.strip().lower()
    if not token or token == "none":
        return CodecFamily.NONE
    base = token.split(".")[0]
    table = _VIDEO_CODEC_MAP if kind == "video" else _AUDIO_CODEC_MAP
    if base in table:
        return table[base]
    if token in table:
        return table[token]
    return CodecFamily.UNKNOWN


def quality_label_for(height: int | None, *, has_video: bool) -> str:
    if not has_video:
        return "audio"
    if height is None:
        return "unknown"
    for label in (2160, 1440, 1080, 720, 480, 360, 240, 144):
        if height >= label:
            return f"p{label}"
    return "low"


def _category_for(has_video: bool, has_audio: bool) -> FormatCategory:
    if has_video and has_audio:
        return FormatCategory.PROGRESSIVE
    if has_video:
        return FormatCategory.VIDEO_ONLY
    return FormatCategory.AUDIO_ONLY


def _token_digest(token: str | None) -> str:
    """Domain-separated digest of a provider format token (never the token)."""
    material = token.encode("utf-8") if token else b""
    digest = hashlib.sha256(b"fetchnow-fmt-token-v1\0" + material).hexdigest()
    return digest[:12]


def format_option_id_for(
    candidate: InternalFormatCandidate, quality_label: str
) -> str:
    """Build a bounded server-side option id independent of input order."""
    category = _category_for(candidate.has_video, candidate.has_audio).value
    fps_milli = (
        0 if candidate.fps is None else int(round(float(candidate.fps) * 1000))
    )
    material = "|".join(
        [
            quality_label,
            candidate.container,
            candidate.video_codec.value,
            candidate.audio_codec.value,
            category,
            str(candidate.width if candidate.width is not None else ""),
            str(candidate.height if candidate.height is not None else ""),
            str(fps_milli),
            str(candidate.approx_bytes if candidate.approx_bytes is not None else ""),
            _token_digest(candidate.provider_format_token),
        ]
    )
    digest = hashlib.sha256(
        b"fetchnow-fmt-option-v1\0" + material.encode("utf-8")
    ).hexdigest()[:28]
    return f"fmt_{digest}"


def _candidate_sort_key(
    candidate: InternalFormatCandidate, option_id: str
) -> tuple[int, int, int, int, int, str]:
    """Deterministic preference: progressive, height, size, width, fps, id."""
    progressive = (
        0
        if candidate.has_video and candidate.has_audio
        else 1
    )
    return (
        progressive,
        -(candidate.height or 0),
        -(candidate.approx_bytes or 0),
        -(candidate.width or 0),
        -(0 if candidate.fps is None else int(round(candidate.fps * 1000))),
        option_id,
    )


def project_metadata(
    draft: ExtractedMediaDraft,
    settings: Settings,
    *,
    trusted_canonical_url: str,
    trusted_media_id: str,
    trusted_provider_id: str,
) -> MediaMetadata:
    """Apply bounds, order-independent dedupe, and free-tier eligibility."""
    title = sanitize_title(
        draft.title, max_length=settings.media_inspection_max_title_length
    )
    duration = require_finite_non_negative(
        draft.duration_seconds,
        field_name="duration_seconds",
        maximum=float(settings.max_source_duration_seconds),
    )
    if duration is not None:
        duration = int(duration)

    max_formats = settings.media_inspection_max_formats
    max_height = settings.media_inspection_max_height
    max_bytes = settings.max_source_file_bytes

    prepared: list[tuple[tuple[int, int, int, int, int, str], MediaFormat]] = []
    for candidate in draft.candidates:
        if not candidate.has_video and not candidate.has_audio:
            continue
        if not _CONTAINER_RE.fullmatch(candidate.container):
            continue
        height = candidate.height
        width = candidate.width
        if height is not None and height > max_height:
            continue
        if width is not None and width > settings.media_inspection_max_width:
            continue
        approx = candidate.approx_bytes
        if approx is not None and approx > max_bytes:
            continue
        label = quality_label_for(height, has_video=candidate.has_video)
        option_id = format_option_id_for(candidate, label)
        category = _category_for(candidate.has_video, candidate.has_audio)
        free_ok = (
            category is FormatCategory.PROGRESSIVE
            and height is not None
            and height <= _FREE_TIER_MAX_HEIGHT
        )
        fmt = MediaFormat(
            format_option_id=option_id,
            container=candidate.container,
            width=width,
            height=height,
            fps=candidate.fps,
            has_video=candidate.has_video,
            has_audio=candidate.has_audio,
            category=category,
            video_codec=candidate.video_codec,
            audio_codec=candidate.audio_codec,
            approx_bytes=approx,
            quality_label=label,
            free_tier_eligible=free_ok,
        )
        prepared.append((_candidate_sort_key(candidate, option_id), fmt))

    if not prepared:
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_POLICY_REJECTED,
            internal_reason="NO_USABLE_FORMATS",
        )

    # Sort first so duplicate option_ids are permutation-invariant (not first-wins).
    prepared.sort(key=lambda item: item[0])
    projected: list[MediaFormat] = []
    by_option: dict[str, MediaFormat] = {}
    for _key, fmt in prepared:
        existing = by_option.get(fmt.format_option_id)
        if existing is None:
            by_option[fmt.format_option_id] = fmt
            projected.append(fmt)
            continue
        if existing != fmt:
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_POLICY_REJECTED,
                internal_reason="OPTION_AMBIGUOUS",
            )
        # Byte-for-byte equivalent MediaFormat: collapse duplicates.

    if len(projected) > max_formats:
        projected = projected[:max_formats]

    has_progressive = any(
        f.category is FormatCategory.PROGRESSIVE for f in projected
    )
    return MediaMetadata(
        provider_id=trusted_provider_id,
        canonical_provider_url=trusted_canonical_url,
        media_id=trusted_media_id,
        title=title,
        duration_seconds=duration,
        formats=tuple(projected),
        extraction_tool="ytdlp",
        extraction_tool_version=draft.tool_version,
        muxing_required=not has_progressive,
    )
