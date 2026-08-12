"""Centralized media-download-job public state machine."""

from __future__ import annotations

from enum import StrEnum

from fetchnow.downloads.errors import DownloadErrorCode, raise_download_error


class MediaDownloadJobState(StrEnum):
    """Download-oriented public job states (PR6)."""

    QUEUED = "queued"
    DOWNLOADING = "downloading"
    READY = "ready"
    FAILED = "failed"
    EXPIRED = "expired"


# Fail-closed allowlist: only listed edges are legal.
_ALLOWED_TRANSITIONS: dict[MediaDownloadJobState, frozenset[MediaDownloadJobState]] = {
    MediaDownloadJobState.QUEUED: frozenset(
        {
            MediaDownloadJobState.DOWNLOADING,
            MediaDownloadJobState.EXPIRED,
        }
    ),
    MediaDownloadJobState.DOWNLOADING: frozenset(
        {
            MediaDownloadJobState.QUEUED,  # retry / reclaim / cancel release
            MediaDownloadJobState.READY,
            MediaDownloadJobState.FAILED,
            MediaDownloadJobState.EXPIRED,
        }
    ),
    MediaDownloadJobState.READY: frozenset({MediaDownloadJobState.EXPIRED}),
    MediaDownloadJobState.FAILED: frozenset({MediaDownloadJobState.EXPIRED}),
    MediaDownloadJobState.EXPIRED: frozenset(),
}


def assert_transition(
    from_state: MediaDownloadJobState | str,
    to_state: MediaDownloadJobState | str,
) -> None:
    """Raise INTERNAL_ERROR when a state transition is not explicitly allowed."""
    try:
        src = (
            from_state
            if isinstance(from_state, MediaDownloadJobState)
            else MediaDownloadJobState(str(from_state))
        )
        dst = (
            to_state
            if isinstance(to_state, MediaDownloadJobState)
            else MediaDownloadJobState(str(to_state))
        )
    except ValueError:
        raise_download_error(
            DownloadErrorCode.INTERNAL_ERROR,
            internal_reason="UNKNOWN_DOWNLOAD_STATE",
        )
    allowed = _ALLOWED_TRANSITIONS.get(src, frozenset())
    if dst not in allowed:
        raise_download_error(
            DownloadErrorCode.INTERNAL_ERROR,
            internal_reason="ILLEGAL_STATE_TRANSITION",
        )


def is_terminal(state: MediaDownloadJobState | str) -> bool:
    """Return True for states that no longer accept worker claims."""
    try:
        value = (
            state
            if isinstance(state, MediaDownloadJobState)
            else MediaDownloadJobState(str(state))
        )
    except ValueError:
        return True
    return value in {
        MediaDownloadJobState.READY,
        MediaDownloadJobState.FAILED,
        MediaDownloadJobState.EXPIRED,
    }
