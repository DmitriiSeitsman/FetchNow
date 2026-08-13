"""Deterministic conservative pairing of video-only + audio-only streams.

Derived public options use the existing progressive MediaFormat schema.
Provider tokens stay on InternalFormatCandidate only (never public/DB/logs).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fetchnow.media_inspection.errors import (
    InspectionErrorKind,
    raise_inspection_error,
)
from fetchnow.media_inspection.models import (
    CodecFamily,
    FormatCategory,
    InternalFormatCandidate,
    MediaFormat,
)

if TYPE_CHECKING:
    from fetchnow.core.config import Settings

_FREE_TIER_MAX_HEIGHT = 720

_MP4_VIDEO = frozenset({CodecFamily.AVC})
_MP4_AUDIO = frozenset({CodecFamily.AAC})
_WEBM_VIDEO = frozenset({CodecFamily.VP8, CodecFamily.VP9, CodecFamily.AV1})
_WEBM_AUDIO = frozenset({CodecFamily.OPUS, CodecFamily.VORBIS})
_HTTP_PROTOCOLS = frozenset({"http", "https"})


def _token_digest(token: str | None) -> str:
    material = token.encode("utf-8") if token else b""
    digest = hashlib.sha256(b"fetchnow-fmt-token-v1\0" + material).hexdigest()
    return digest[:12]


def _quality_label_for(height: int | None) -> str:
    if height is None:
        return "unknown"
    for label in (2160, 1440, 1080, 720, 480, 360, 240, 144):
        if height >= label:
            return f"p{label}"
    return "low"


@dataclass(frozen=True, slots=True, repr=False)
class DerivedMuxOption:
    """Internal binding for one derived muxed progressive option."""

    public: MediaFormat
    video: InternalFormatCandidate
    audio: InternalFormatCandidate
    output_container: str

    def __repr__(self) -> str:
        return (
            "DerivedMuxOption("
            f"format_option_id={self.public.format_option_id!r}, "
            f"output_container={self.output_container!r}, "
            f"quality_label={self.public.quality_label!r})"
        )


def mux_option_id_for(
    video: InternalFormatCandidate,
    audio: InternalFormatCandidate,
    *,
    output_container: str,
    quality_label: str,
) -> str:
    """Opaque order-invariant id over both bound stream identities."""
    fps_milli = 0 if video.fps is None else int(round(float(video.fps) * 1000))
    video_bytes = video.approx_bytes if video.approx_bytes is not None else ""
    audio_bytes = audio.approx_bytes if audio.approx_bytes is not None else ""
    material = "|".join(
        [
            "muxed",
            quality_label,
            output_container,
            video.video_codec.value,
            audio.audio_codec.value,
            str(video.width if video.width is not None else ""),
            str(video.height if video.height is not None else ""),
            str(fps_milli),
            str(video_bytes),
            str(audio_bytes),
            _token_digest(video.provider_format_token),
            _token_digest(audio.provider_format_token),
        ]
    )
    digest = hashlib.sha256(
        b"fetchnow-fmt-mux-option-v1\0" + material.encode("utf-8")
    ).hexdigest()[:28]
    return f"fmt_{digest}"


def output_policy_for(
    video: InternalFormatCandidate, audio: InternalFormatCandidate
) -> str | None:
    """Return the approved output container, or None when incompatible.

    Codec family and container family must agree. H.264 in WebM or AAC in
    WebM is not a stream-copy pair.
    """
    if (
        video.container == "mp4"
        and audio.container in {"m4a", "mp4"}
        and video.video_codec in _MP4_VIDEO
        and audio.audio_codec in _MP4_AUDIO
    ):
        return "mp4"
    if (
        video.container == "webm"
        and audio.container in {"webm", "ogg", "opus"}
        and video.video_codec in _WEBM_VIDEO
        and audio.audio_codec in _WEBM_AUDIO
    ):
        return "webm"
    return None


def _usable_video(candidate: InternalFormatCandidate) -> bool:
    if not candidate.has_video or candidate.has_audio:
        return False
    if candidate.has_drm:
        return False
    if candidate.protocol is not None and candidate.protocol not in _HTTP_PROTOCOLS:
        return False
    if candidate.video_codec in {CodecFamily.NONE, CodecFamily.UNKNOWN}:
        return False
    if not candidate.provider_format_token:
        return False
    if candidate.height is None or candidate.height > _FREE_TIER_MAX_HEIGHT:
        return False
    if candidate.container == "mp4":
        return candidate.video_codec in _MP4_VIDEO
    if candidate.container == "webm":
        return candidate.video_codec in _WEBM_VIDEO
    return False


def _usable_audio(candidate: InternalFormatCandidate) -> bool:
    if not candidate.has_audio or candidate.has_video:
        return False
    if candidate.has_drm:
        return False
    if candidate.protocol is not None and candidate.protocol not in _HTTP_PROTOCOLS:
        return False
    if candidate.audio_codec in {CodecFamily.NONE, CodecFamily.UNKNOWN}:
        return False
    if not candidate.provider_format_token:
        return False
    if candidate.container in {"m4a", "mp4"}:
        return candidate.audio_codec in _MP4_AUDIO
    if candidate.container in {"webm", "ogg", "opus"}:
        return candidate.audio_codec in _WEBM_AUDIO
    return False


def _video_sort_key(candidate: InternalFormatCandidate) -> tuple[int, int, str]:
    return (
        -(candidate.height or 0),
        -(candidate.width or 0),
        _token_digest(candidate.provider_format_token),
    )


def _audio_sort_key(candidate: InternalFormatCandidate) -> tuple[int, str, str]:
    approx = candidate.approx_bytes if candidate.approx_bytes is not None else 10**18
    return (
        approx,
        _token_digest(candidate.provider_format_token),
        candidate.container,
    )


def _binding_equivalent(left: DerivedMuxOption, right: DerivedMuxOption) -> bool:
    if left.output_container != right.output_container:
        return False
    if left.video.provider_format_token != right.video.provider_format_token:
        return False
    if left.audio.provider_format_token != right.audio.provider_format_token:
        return False
    return left.public == right.public


def derive_mux_options(
    candidates: tuple[InternalFormatCandidate, ...],
    settings: Settings,
) -> tuple[DerivedMuxOption, ...]:
    """Build bounded derived mux options. Empty when muxing is disabled."""
    if not settings.media_muxing_enabled:
        return ()

    videos = [c for c in candidates if _usable_video(c)]
    audios = [c for c in candidates if _usable_audio(c)]
    videos.sort(key=_video_sort_key)
    audios.sort(key=_audio_sort_key)
    videos = videos[: settings.media_muxing_max_video_candidates]
    audios = audios[: settings.media_muxing_max_audio_candidates]
    if not videos or not audios:
        return ()

    prepared: list[DerivedMuxOption] = []
    for video in videos:
        chosen: InternalFormatCandidate | None = None
        output: str | None = None
        for audio in audios:
            policy = output_policy_for(video, audio)
            if policy is None:
                continue
            chosen = audio
            output = policy
            break
        if chosen is None or output is None:
            continue
        label = _quality_label_for(video.height)
        option_id = mux_option_id_for(
            video, chosen, output_container=output, quality_label=label
        )
        video_bytes = video.approx_bytes
        audio_bytes = chosen.approx_bytes
        approx: int | None
        if video_bytes is None or audio_bytes is None:
            approx = None
        else:
            approx = video_bytes + audio_bytes
        public = MediaFormat(
            format_option_id=option_id,
            container=output,
            width=video.width,
            height=video.height,
            fps=video.fps,
            has_video=True,
            has_audio=True,
            category=FormatCategory.PROGRESSIVE,
            video_codec=video.video_codec,
            audio_codec=chosen.audio_codec,
            approx_bytes=approx,
            quality_label=label,
            free_tier_eligible=True,
        )
        prepared.append(
            DerivedMuxOption(
                public=public,
                video=video,
                audio=chosen,
                output_container=output,
            )
        )

    prepared.sort(key=lambda item: item.public.format_option_id)
    unique: list[DerivedMuxOption] = []
    by_id: dict[str, DerivedMuxOption] = {}
    for item in prepared:
        existing = by_id.get(item.public.format_option_id)
        if existing is None:
            by_id[item.public.format_option_id] = item
            unique.append(item)
            continue
        if not _binding_equivalent(existing, item):
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_POLICY_REJECTED,
                internal_reason="MUX_OPTION_AMBIGUOUS",
            )
    unique.sort(
        key=lambda item: (
            -(item.video.height or 0),
            item.public.format_option_id,
        )
    )
    return tuple(unique[: settings.media_muxing_max_derived_options])
