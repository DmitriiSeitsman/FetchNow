"""Immutable media-inspection domain models."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import StrEnum

_SAFE_TOKEN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_MEDIA_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class FormatCategory(StrEnum):
    """Normalized stream category (not a raw provider codec string)."""

    PROGRESSIVE = "progressive"
    VIDEO_ONLY = "video_only"
    AUDIO_ONLY = "audio_only"


class CodecFamily(StrEnum):
    """Bounded codec families — unknown maps to UNKNOWN, never raw strings."""

    AVC = "avc"
    HEVC = "hevc"
    VP8 = "vp8"
    VP9 = "vp9"
    AV1 = "av1"
    AAC = "aac"
    OPUS = "opus"
    MP3 = "mp3"
    VORBIS = "vorbis"
    FLAC = "flac"
    UNKNOWN = "unknown"
    NONE = "none"


def require_safe_token(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not _SAFE_TOKEN.fullmatch(value):
        raise ValueError(f"unsafe {field_name}")
    return value


def require_finite_non_negative(
    value: float | int | None,
    *,
    field_name: str,
    maximum: float | None = None,
) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"invalid {field_name}")
    if isinstance(value, float):
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"invalid {field_name}")
    elif isinstance(value, int):
        if value < 0:
            raise ValueError(f"invalid {field_name}")
    else:
        raise ValueError(f"invalid {field_name}")
    if maximum is not None and float(value) > maximum:
        raise ValueError(f"{field_name} exceeds policy maximum")
    return value


def sanitize_title(raw: str | None, *, max_length: int) -> str | None:
    """Strip controls and bound length; empty becomes None."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError("title must be a string")
    cleaned = _CONTROL_CHARS.sub("", raw).replace("\t", " ").strip()
    cleaned = re.sub(r"[ \n\r]+", " ", cleaned).strip()
    if not cleaned:
        return None
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip()
    return cleaned or None


@dataclass(frozen=True, slots=True, repr=False)
class MediaFormat:
    """Server-controlled quality option derived from normalized dimensions."""

    format_option_id: str
    container: str
    width: int | None
    height: int | None
    fps: float | None
    has_video: bool
    has_audio: bool
    category: FormatCategory
    video_codec: CodecFamily
    audio_codec: CodecFamily
    approx_bytes: int | None
    quality_label: str
    free_tier_eligible: bool

    def __post_init__(self) -> None:
        require_safe_token(self.format_option_id, field_name="format_option_id")
        require_safe_token(self.container, field_name="container")
        require_safe_token(self.quality_label, field_name="quality_label")
        require_finite_non_negative(self.width, field_name="width")
        require_finite_non_negative(self.height, field_name="height")
        require_finite_non_negative(self.fps, field_name="fps", maximum=240.0)
        require_finite_non_negative(self.approx_bytes, field_name="approx_bytes")
        if self.has_video and self.width is None and self.height is None:
            raise ValueError("video format requires dimensions or explicit null policy")
        if not self.has_video and not self.has_audio:
            raise ValueError("format must include audio or video")

    def __repr__(self) -> str:
        return (
            "MediaFormat("
            f"format_option_id={self.format_option_id!r}, "
            f"container={self.container!r}, "
            f"width={self.width!r}, "
            f"height={self.height!r}, "
            f"category={self.category!r}, "
            f"quality_label={self.quality_label!r}, "
            f"free_tier_eligible={self.free_tier_eligible!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class MediaMetadata:
    """Bounded metadata for a validated provider media identity."""

    provider_id: str
    canonical_provider_url: str
    media_id: str
    title: str | None
    duration_seconds: int | None
    formats: tuple[MediaFormat, ...]
    extraction_tool: str
    extraction_tool_version: str | None
    muxing_required: bool

    def __post_init__(self) -> None:
        require_safe_token(self.provider_id, field_name="provider_id")
        require_safe_token(self.extraction_tool, field_name="extraction_tool")
        if not isinstance(self.canonical_provider_url, str):
            raise ValueError("canonical_provider_url must be a string")
        if (
            "?" in self.canonical_provider_url
            or "#" in self.canonical_provider_url
            or "://" not in self.canonical_provider_url
        ):
            raise ValueError(
                "canonical_provider_url must be scheme URL without query/fragment"
            )
        if not isinstance(self.media_id, str) or not _SAFE_MEDIA_ID.fullmatch(
            self.media_id
        ):
            raise ValueError("unsafe media_id")
        version = self.extraction_tool_version
        if version is not None and (
            not isinstance(version, str)
            or len(version) > 64
            or not re.fullmatch(r"[A-Za-z0-9._+-]+", version)
        ):
            raise ValueError("unsafe extraction_tool_version")
        require_finite_non_negative(
            self.duration_seconds, field_name="duration_seconds"
        )
        if not isinstance(self.formats, tuple):
            raise ValueError("formats must be a tuple")

    def __repr__(self) -> str:
        # Title omitted — may contain unexpected user/provider text.
        return (
            "MediaMetadata("
            f"provider_id={self.provider_id!r}, "
            f"canonical_provider_url={self.canonical_provider_url!r}, "
            f"media_id={self.media_id!r}, "
            f"duration_seconds={self.duration_seconds!r}, "
            f"format_count={len(self.formats)}, "
            f"extraction_tool={self.extraction_tool!r}, "
            f"muxing_required={self.muxing_required!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class InternalFormatCandidate:
    """Internal candidate before public MediaFormat projection.

    Direct media URLs are intentionally absent from this model. Callers that
    need bytes download (future PR) must obtain URLs from a separate secret
    store never exposed via repr/logs.
    """

    container: str
    width: int | None
    height: int | None
    fps: float | None
    has_video: bool
    has_audio: bool
    video_codec: CodecFamily
    audio_codec: CodecFamily
    approx_bytes: int | None
    # Secret-bearing provider format token — never logged or repr'd.
    provider_format_token: str | None = field(default=None, repr=False)
    # Bounded transport token from yt-dlp (http/https/m3u8/...). Never a URL.
    protocol: str | None = None
    has_drm: bool = False

    def __repr__(self) -> str:
        return (
            "InternalFormatCandidate("
            f"container={self.container!r}, "
            f"width={self.width!r}, "
            f"height={self.height!r}, "
            f"has_video={self.has_video!r}, "
            f"has_audio={self.has_audio!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ExtractedMediaDraft:
    """Normalized extractor output before policy projection."""

    provider_id: str
    canonical_provider_url: str
    media_id: str
    title: str | None
    duration_seconds: int | None
    candidates: tuple[InternalFormatCandidate, ...]
    extractor_key: str
    tool_version: str | None
    # Any direct/signed URLs stay off this object entirely.

    def __repr__(self) -> str:
        return (
            "ExtractedMediaDraft("
            f"provider_id={self.provider_id!r}, "
            f"canonical_provider_url={self.canonical_provider_url!r}, "
            f"media_id={self.media_id!r}, "
            f"candidate_count={len(self.candidates)}, "
            f"extractor_key={self.extractor_key!r})"
        )
