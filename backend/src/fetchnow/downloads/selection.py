"""Ephemeral opaque formatOptionId → provider_format_token binding."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fetchnow.downloads.errors import DownloadErrorCode, raise_download_error
from fetchnow.downloads.format_token import validate_provider_format_token
from fetchnow.downloads.snapshot_codec import encode_selected_format_snapshot
from fetchnow.media_inspection.models import (
    ExtractedMediaDraft,
    FormatCategory,
    InternalFormatCandidate,
    MediaFormat,
)
from fetchnow.media_inspection.normalize import (
    format_option_id_for,
    quality_label_for,
)

if TYPE_CHECKING:
    from fetchnow.core.config import Settings

_FREE_TIER_MAX_HEIGHT = 720
_CONTAINER_RE = re.compile(r"^[a-z0-9]{1,8}$")


@dataclass(frozen=True, slots=True, repr=False)
class ResolvedDownloadSelection:
    """Sealed in-memory selection for one download attempt.

    ``provider_format_token`` is ephemeral and must never be persisted, logged,
    or included in repr/str of public objects.
    """

    format_option_id: str
    container: str
    width: int | None
    height: int | None
    fps: float | None
    has_video: bool
    has_audio: bool
    category: FormatCategory
    quality_label: str
    free_tier_eligible: bool
    approx_bytes: int | None
    provider_format_token: str = field(repr=False)

    def __repr__(self) -> str:
        return (
            "ResolvedDownloadSelection("
            f"format_option_id={self.format_option_id!r}, "
            f"container={self.container!r}, "
            f"width={self.width!r}, "
            f"height={self.height!r}, "
            f"category={self.category!r}, "
            f"quality_label={self.quality_label!r}, "
            f"free_tier_eligible={self.free_tier_eligible!r})"
        )


def format_snapshot_from_media_format(fmt: MediaFormat) -> dict[str, object]:
    """Serialize bounded public format fields (no provider token)."""
    return encode_selected_format_snapshot(fmt)


def _category_for(has_video: bool, has_audio: bool) -> FormatCategory:
    if has_video and has_audio:
        return FormatCategory.PROGRESSIVE
    if has_video:
        return FormatCategory.VIDEO_ONLY
    return FormatCategory.AUDIO_ONLY


def _free_tier_eligible(candidate: InternalFormatCandidate) -> bool:
    category = _category_for(candidate.has_video, candidate.has_audio)
    return (
        category is FormatCategory.PROGRESSIVE
        and candidate.height is not None
        and candidate.height <= _FREE_TIER_MAX_HEIGHT
    )


def _props_match(candidate: InternalFormatCandidate, persisted: MediaFormat) -> bool:
    """Compare normalized public properties against the parent selection snapshot."""
    label = quality_label_for(candidate.height, has_video=candidate.has_video)
    category = _category_for(candidate.has_video, candidate.has_audio)
    if candidate.container != persisted.container:
        return False
    if candidate.width != persisted.width:
        return False
    if candidate.height != persisted.height:
        return False
    if candidate.fps != persisted.fps:
        return False
    if candidate.has_video != persisted.has_video:
        return False
    if candidate.has_audio != persisted.has_audio:
        return False
    if category != persisted.category:
        return False
    if candidate.video_codec != persisted.video_codec:
        return False
    if candidate.audio_codec != persisted.audio_codec:
        return False
    if candidate.approx_bytes != persisted.approx_bytes:
        return False
    if label != persisted.quality_label:
        return False
    return _free_tier_eligible(candidate) == persisted.free_tier_eligible


def _binding_equivalent(
    left: InternalFormatCandidate, right: InternalFormatCandidate
) -> bool:
    """True when two candidates bind identically (same token + normalized props)."""
    if left.provider_format_token != right.provider_format_token:
        return False
    if left.container != right.container:
        return False
    if left.width != right.width:
        return False
    if left.height != right.height:
        return False
    if left.fps != right.fps:
        return False
    if left.has_video != right.has_video:
        return False
    if left.has_audio != right.has_audio:
        return False
    if left.video_codec != right.video_codec:
        return False
    if left.audio_codec != right.audio_codec:
        return False
    return left.approx_bytes == right.approx_bytes


def _candidate_sort_key(
    item: tuple[str, InternalFormatCandidate],
) -> tuple[str, str, str, int, int, str]:
    option_id, candidate = item
    token = candidate.provider_format_token or ""
    return (
        option_id,
        candidate.container,
        token,
        candidate.height or -1,
        candidate.width or -1,
        f"{candidate.approx_bytes if candidate.approx_bytes is not None else ''}",
    )


def resolve_selection_from_draft(
    draft: ExtractedMediaDraft,
    settings: Settings,
    requested_option_id: str,
    persisted_format: MediaFormat,
) -> ResolvedDownloadSelection:
    """Build an ephemeral option→token map and require an exact durable match.

    No closest/best/first fallback. Missing, drifted, or ambiguous options raise
    ``FORMAT_UNAVAILABLE``.
    """
    if requested_option_id != persisted_format.format_option_id:
        raise_download_error(
            DownloadErrorCode.FORMAT_UNAVAILABLE,
            internal_reason="OPTION_ID_MISMATCH",
        )

    max_height = settings.media_inspection_max_height
    max_width = settings.media_inspection_max_width
    max_bytes = settings.max_source_file_bytes

    prepared: list[tuple[str, InternalFormatCandidate]] = []
    for candidate in draft.candidates:
        if not candidate.has_video and not candidate.has_audio:
            continue
        if not _CONTAINER_RE.fullmatch(candidate.container):
            continue
        if candidate.height is not None and candidate.height > max_height:
            continue
        if candidate.width is not None and candidate.width > max_width:
            continue
        if (
            candidate.approx_bytes is not None
            and candidate.approx_bytes > max_bytes
        ):
            continue
        label = quality_label_for(candidate.height, has_video=candidate.has_video)
        option_id = format_option_id_for(candidate, label)
        prepared.append((option_id, candidate))

    # Deterministic order, then conflict detection — never first-wins by input.
    prepared.sort(key=_candidate_sort_key)
    by_option: dict[str, InternalFormatCandidate] = {}
    for option_id, candidate in prepared:
        existing = by_option.get(option_id)
        if existing is None:
            by_option[option_id] = candidate
            continue
        if not _binding_equivalent(existing, candidate):
            raise_download_error(
                DownloadErrorCode.FORMAT_UNAVAILABLE,
                internal_reason="OPTION_AMBIGUOUS",
            )
        # Byte-equivalent for binding: collapse duplicates.

    matched = by_option.get(requested_option_id)
    if matched is None:
        raise_download_error(
            DownloadErrorCode.FORMAT_UNAVAILABLE,
            internal_reason="OPTION_MISSING",
        )

    raw_token = matched.provider_format_token
    if not isinstance(raw_token, str) or not raw_token.strip():
        raise_download_error(
            DownloadErrorCode.FORMAT_UNAVAILABLE,
            internal_reason="TOKEN_MISSING",
        )
    try:
        token = validate_provider_format_token(raw_token)
    except ValueError:
        raise_download_error(
            DownloadErrorCode.FORMAT_UNAVAILABLE,
            internal_reason="TOKEN_INVALID",
        )

    if not _props_match(matched, persisted_format):
        raise_download_error(
            DownloadErrorCode.FORMAT_UNAVAILABLE,
            internal_reason="PROPS_MISMATCH",
        )

    if not (
        matched.has_video
        and matched.has_audio
        and _free_tier_eligible(matched)
        and persisted_format.free_tier_eligible
        and persisted_format.category is FormatCategory.PROGRESSIVE
    ):
        raise_download_error(
            DownloadErrorCode.FORMAT_UNAVAILABLE,
            internal_reason="NOT_ELIGIBLE",
        )

    category = _category_for(matched.has_video, matched.has_audio)
    label = quality_label_for(matched.height, has_video=matched.has_video)
    return ResolvedDownloadSelection(
        format_option_id=requested_option_id,
        container=matched.container,
        width=matched.width,
        height=matched.height,
        fps=matched.fps,
        has_video=matched.has_video,
        has_audio=matched.has_audio,
        category=category,
        quality_label=label,
        free_tier_eligible=True,
        approx_bytes=matched.approx_bytes,
        provider_format_token=token,
    )
