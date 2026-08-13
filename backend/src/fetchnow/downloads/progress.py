"""Fenced public progress stages for download jobs."""

from __future__ import annotations

from enum import StrEnum

from fetchnow.downloads.errors import DownloadErrorCode, raise_download_error
from fetchnow.downloads.states import MediaDownloadJobState


class DownloadProgressStage(StrEnum):
    """Sanitized public progress. No provider internals or percentages."""

    QUEUED = "queued"
    INSPECTING = "inspecting"
    DOWNLOADING_VIDEO = "downloading_video"
    DOWNLOADING_AUDIO = "downloading_audio"
    MUXING = "muxing"
    VERIFYING = "verifying"
    PUBLISHING = "publishing"
    RETRYING = "retrying"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


TERMINAL_PROGRESS = frozenset(
    {
        DownloadProgressStage.READY,
        DownloadProgressStage.FAILED,
        DownloadProgressStage.CANCELLED,
        DownloadProgressStage.EXPIRED,
    }
)

ACTIVE_PROGRESS = frozenset(
    {
        DownloadProgressStage.QUEUED,
        DownloadProgressStage.INSPECTING,
        DownloadProgressStage.DOWNLOADING_VIDEO,
        DownloadProgressStage.DOWNLOADING_AUDIO,
        DownloadProgressStage.MUXING,
        DownloadProgressStage.VERIFYING,
        DownloadProgressStage.PUBLISHING,
        DownloadProgressStage.RETRYING,
    }
)

_ALLOWED: dict[DownloadProgressStage, frozenset[DownloadProgressStage]] = {
    DownloadProgressStage.QUEUED: frozenset(
        {
            DownloadProgressStage.INSPECTING,
            DownloadProgressStage.DOWNLOADING_VIDEO,
            DownloadProgressStage.CANCELLED,
            DownloadProgressStage.EXPIRED,
            DownloadProgressStage.FAILED,
        }
    ),
    DownloadProgressStage.INSPECTING: frozenset(
        {
            DownloadProgressStage.DOWNLOADING_VIDEO,
            DownloadProgressStage.DOWNLOADING_AUDIO,
            DownloadProgressStage.RETRYING,
            DownloadProgressStage.FAILED,
            DownloadProgressStage.CANCELLED,
            DownloadProgressStage.EXPIRED,
        }
    ),
    DownloadProgressStage.DOWNLOADING_VIDEO: frozenset(
        {
            DownloadProgressStage.DOWNLOADING_AUDIO,
            DownloadProgressStage.MUXING,
            DownloadProgressStage.VERIFYING,
            DownloadProgressStage.PUBLISHING,
            DownloadProgressStage.RETRYING,
            DownloadProgressStage.FAILED,
            DownloadProgressStage.CANCELLED,
            DownloadProgressStage.EXPIRED,
        }
    ),
    DownloadProgressStage.DOWNLOADING_AUDIO: frozenset(
        {
            DownloadProgressStage.MUXING,
            DownloadProgressStage.RETRYING,
            DownloadProgressStage.FAILED,
            DownloadProgressStage.CANCELLED,
            DownloadProgressStage.EXPIRED,
        }
    ),
    DownloadProgressStage.MUXING: frozenset(
        {
            DownloadProgressStage.VERIFYING,
            DownloadProgressStage.RETRYING,
            DownloadProgressStage.FAILED,
            DownloadProgressStage.CANCELLED,
            DownloadProgressStage.EXPIRED,
        }
    ),
    DownloadProgressStage.VERIFYING: frozenset(
        {
            DownloadProgressStage.PUBLISHING,
            DownloadProgressStage.RETRYING,
            DownloadProgressStage.FAILED,
            DownloadProgressStage.CANCELLED,
            DownloadProgressStage.EXPIRED,
        }
    ),
    DownloadProgressStage.PUBLISHING: frozenset(
        {
            DownloadProgressStage.READY,
            DownloadProgressStage.RETRYING,
            DownloadProgressStage.FAILED,
            DownloadProgressStage.CANCELLED,
            DownloadProgressStage.EXPIRED,
        }
    ),
    DownloadProgressStage.RETRYING: frozenset(
        {
            DownloadProgressStage.QUEUED,
            DownloadProgressStage.INSPECTING,
            DownloadProgressStage.FAILED,
            DownloadProgressStage.CANCELLED,
            DownloadProgressStage.EXPIRED,
        }
    ),
    DownloadProgressStage.READY: frozenset({DownloadProgressStage.EXPIRED}),
    DownloadProgressStage.FAILED: frozenset({DownloadProgressStage.EXPIRED}),
    DownloadProgressStage.CANCELLED: frozenset({DownloadProgressStage.EXPIRED}),
    DownloadProgressStage.EXPIRED: frozenset(),
}


def parse_progress_stage(raw: str | DownloadProgressStage) -> DownloadProgressStage:
    if isinstance(raw, DownloadProgressStage):
        return raw
    try:
        return DownloadProgressStage(str(raw))
    except ValueError:
        raise_download_error(
            DownloadErrorCode.INTERNAL_ERROR,
            internal_reason="UNKNOWN_PROGRESS_STAGE",
        )


def is_terminal_progress(stage: DownloadProgressStage | str) -> bool:
    return parse_progress_stage(stage) in TERMINAL_PROGRESS


def assert_progress_transition(
    from_stage: DownloadProgressStage | str,
    to_stage: DownloadProgressStage | str,
) -> None:
    src = parse_progress_stage(from_stage)
    dst = parse_progress_stage(to_stage)
    if src is dst:
        return
    if src in TERMINAL_PROGRESS and dst not in _ALLOWED.get(src, frozenset()):
        raise_download_error(
            DownloadErrorCode.INTERNAL_ERROR,
            internal_reason="TERMINAL_PROGRESS_IMMUTABLE",
        )
    allowed = _ALLOWED.get(src, frozenset())
    if dst not in allowed:
        raise_download_error(
            DownloadErrorCode.INTERNAL_ERROR,
            internal_reason="ILLEGAL_PROGRESS_TRANSITION",
        )


def progress_for_public_state(
    state: MediaDownloadJobState | str,
) -> DownloadProgressStage:
    """Best-effort mapping used by migration and expired/forced views."""
    try:
        value = (
            state
            if isinstance(state, MediaDownloadJobState)
            else MediaDownloadJobState(str(state))
        )
    except ValueError:
        return DownloadProgressStage.FAILED
    return {
        MediaDownloadJobState.QUEUED: DownloadProgressStage.QUEUED,
        MediaDownloadJobState.DOWNLOADING: DownloadProgressStage.INSPECTING,
        MediaDownloadJobState.READY: DownloadProgressStage.READY,
        MediaDownloadJobState.FAILED: DownloadProgressStage.FAILED,
        MediaDownloadJobState.CANCELLED: DownloadProgressStage.CANCELLED,
        MediaDownloadJobState.EXPIRED: DownloadProgressStage.EXPIRED,
    }.get(value, DownloadProgressStage.QUEUED)
