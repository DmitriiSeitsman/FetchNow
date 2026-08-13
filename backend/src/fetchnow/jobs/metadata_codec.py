"""Bounded JSON codec for sanitized MediaMetadata persistence."""

from __future__ import annotations

import json
from typing import Any

from fetchnow.jobs.errors import JobErrorCode, raise_job_error
from fetchnow.media_inspection.models import (
    CodecFamily,
    FormatCategory,
    MediaFormat,
    MediaMetadata,
)

# Storage keys match the public API camelCase contract.
_TOP_LEVEL_KEYS = frozenset(
    {
        "providerId",
        "canonicalProviderUrl",
        "mediaId",
        "title",
        "durationSeconds",
        "formats",
        "extractionTool",
        "extractionToolVersion",
        "muxingRequired",
    }
)

_FORMAT_KEYS = frozenset(
    {
        "formatOptionId",
        "container",
        "width",
        "height",
        "fps",
        "hasVideo",
        "hasAudio",
        "category",
        "videoCodec",
        "audioCodec",
        "approxBytes",
        "qualityLabel",
        "freeTierEligible",
    }
)

_FORBIDDEN_KEYS = frozenset(
    {
        "url",
        "urls",
        "directUrl",
        "directUrls",
        "signedUrl",
        "downloadUrl",
        "streamUrl",
        "manifestUrl",
        "cookie",
        "cookies",
        "authorization",
        "token",
        "accessToken",
        "password",
        "secret",
        "stderr",
        "stdout",
        "raw",
        "path",
        "filepath",
        "tempPath",
        "query",
        "fragment",
        "headers",
        "argv",
        "command",
    }
)


def _json_byte_size(payload: Any) -> int:
    return len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def _reject_forbidden(mapping: dict[str, Any], *, allow: frozenset[str]) -> None:
    for key in mapping:
        if not isinstance(key, str):
            raise_job_error(
                JobErrorCode.INTERNAL_ERROR,
                internal_reason="METADATA_KEY_TYPE",
            )
        lowered = key.strip().lower()
        if key in _FORBIDDEN_KEYS or lowered in {k.lower() for k in _FORBIDDEN_KEYS}:
            raise_job_error(
                JobErrorCode.INTERNAL_ERROR,
                internal_reason="METADATA_FORBIDDEN_KEY",
            )
        if key not in allow:
            raise_job_error(
                JobErrorCode.INTERNAL_ERROR,
                internal_reason="METADATA_UNKNOWN_KEY",
            )


def media_metadata_to_jsonable(
    metadata: MediaMetadata,
    *,
    max_bytes: int,
) -> dict[str, Any]:
    """Serialize sanitized MediaMetadata to a bounded camelCase JSON object."""
    if not isinstance(metadata, MediaMetadata):
        raise_job_error(
            JobErrorCode.INTERNAL_ERROR,
            internal_reason="METADATA_TYPE",
        )
    formats: list[dict[str, Any]] = []
    for fmt in metadata.formats:
        formats.append(
            {
                "formatOptionId": fmt.format_option_id,
                "container": fmt.container,
                "width": fmt.width,
                "height": fmt.height,
                "fps": fmt.fps,
                "hasVideo": fmt.has_video,
                "hasAudio": fmt.has_audio,
                "category": fmt.category.value,
                "videoCodec": fmt.video_codec.value,
                "audioCodec": fmt.audio_codec.value,
                "approxBytes": fmt.approx_bytes,
                "qualityLabel": fmt.quality_label,
                "freeTierEligible": fmt.free_tier_eligible,
            }
        )
    payload: dict[str, Any] = {
        "providerId": metadata.provider_id,
        "canonicalProviderUrl": metadata.canonical_provider_url,
        "mediaId": metadata.media_id,
        "title": metadata.title,
        "durationSeconds": metadata.duration_seconds,
        "formats": formats,
        "extractionTool": metadata.extraction_tool,
        "extractionToolVersion": metadata.extraction_tool_version,
        "muxingRequired": metadata.muxing_required,
    }
    size = _json_byte_size(payload)
    if size > max_bytes:
        raise_job_error(
            JobErrorCode.INTERNAL_ERROR,
            internal_reason="METADATA_TOO_LARGE",
        )
    return payload


def _muxing_required_from_payload(
    payload: dict[str, Any], formats: list[MediaFormat]
) -> bool:
    """Read muxingRequired; PR8 snapshots always had it, missing is derived."""
    if "muxingRequired" not in payload:
        return not any(
            fmt.category is FormatCategory.PROGRESSIVE
            and fmt.free_tier_eligible
            and fmt.has_video
            and fmt.has_audio
            for fmt in formats
        )
    raw = payload["muxingRequired"]
    if not isinstance(raw, bool):
        raise_job_error(
            JobErrorCode.INTERNAL_ERROR,
            internal_reason="METADATA_INVALID",
        )
    return raw


def media_metadata_from_jsonable(
    payload: Any,
    *,
    max_bytes: int,
) -> MediaMetadata:
    """Load MediaMetadata from stored camelCase JSON; reject unknown keys."""
    if not isinstance(payload, dict):
        raise_job_error(
            JobErrorCode.INTERNAL_ERROR,
            internal_reason="METADATA_PAYLOAD_TYPE",
        )
    size = _json_byte_size(payload)
    if size > max_bytes:
        raise_job_error(
            JobErrorCode.INTERNAL_ERROR,
            internal_reason="METADATA_TOO_LARGE",
        )
    _reject_forbidden(payload, allow=_TOP_LEVEL_KEYS)

    raw_formats = payload.get("formats")
    if not isinstance(raw_formats, list):
        raise_job_error(
            JobErrorCode.INTERNAL_ERROR,
            internal_reason="METADATA_FORMATS_TYPE",
        )
    formats: list[MediaFormat] = []
    for item in raw_formats:
        if not isinstance(item, dict):
            raise_job_error(
                JobErrorCode.INTERNAL_ERROR,
                internal_reason="METADATA_FORMAT_TYPE",
            )
        _reject_forbidden(item, allow=_FORMAT_KEYS)
        try:
            formats.append(
                MediaFormat(
                    format_option_id=str(item["formatOptionId"]),
                    container=str(item["container"]),
                    width=item.get("width"),
                    height=item.get("height"),
                    fps=item.get("fps"),
                    has_video=bool(item["hasVideo"]),
                    has_audio=bool(item["hasAudio"]),
                    category=FormatCategory(str(item["category"])),
                    video_codec=CodecFamily(str(item["videoCodec"])),
                    audio_codec=CodecFamily(str(item["audioCodec"])),
                    approx_bytes=item.get("approxBytes"),
                    quality_label=str(item["qualityLabel"]),
                    free_tier_eligible=bool(item["freeTierEligible"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            raise_job_error(
                JobErrorCode.INTERNAL_ERROR,
                internal_reason="METADATA_FORMAT_INVALID",
            )
    try:
        return MediaMetadata(
            provider_id=str(payload["providerId"]),
            canonical_provider_url=str(payload["canonicalProviderUrl"]),
            media_id=str(payload["mediaId"]),
            title=payload.get("title"),
            duration_seconds=payload.get("durationSeconds"),
            formats=tuple(formats),
            extraction_tool=str(payload["extractionTool"]),
            extraction_tool_version=payload.get("extractionToolVersion"),
            muxing_required=_muxing_required_from_payload(payload, formats),
        )
    except (KeyError, TypeError, ValueError):
        raise_job_error(
            JobErrorCode.INTERNAL_ERROR,
            internal_reason="METADATA_INVALID",
        )
