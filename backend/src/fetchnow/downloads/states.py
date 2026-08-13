"""Centralized media-download-job public state machine."""

from __future__ import annotations

from enum import StrEnum

from fetchnow.downloads.errors import DownloadErrorCode, raise_download_error


class MediaDownloadJobState(StrEnum):
    """Download-oriented public job states (PR6) plus public-only ``cancelled``.

    ``cancelled`` is an API projection. Durable rows keep a PR9-known
    ``public_state`` (``expired``) plus PR10 ``progress_stage`` /
    ``cancel_requested_at`` markers so a rolled-back PR9 application can
    still parse the row.
    """

    QUEUED = "queued"
    DOWNLOADING = "downloading"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


# Raw ``media_download_jobs.public_state`` values the PR9 application enum
# can construct. ``cancelled`` is never stored in that column.
PR9_STORED_PUBLIC_STATES = frozenset(
    {
        MediaDownloadJobState.QUEUED.value,
        MediaDownloadJobState.DOWNLOADING.value,
        MediaDownloadJobState.READY.value,
        MediaDownloadJobState.FAILED.value,
        MediaDownloadJobState.EXPIRED.value,
    }
)


def is_stored_cancelled(
    public_state: str,
    progress_stage: str | None,
    cancel_requested_at: object | None,
) -> bool:
    """True when durable columns encode a user cancellation."""
    return (
        public_state == MediaDownloadJobState.EXPIRED.value
        and str(progress_stage or "") == "cancelled"
        and cancel_requested_at is not None
    )


def project_public_state(
    public_state: str,
    progress_stage: str | None = None,
    cancel_requested_at: object | None = None,
) -> MediaDownloadJobState:
    """Map stored columns to the public API state (may raise ``ValueError``)."""
    if is_stored_cancelled(public_state, progress_stage, cancel_requested_at):
        return MediaDownloadJobState.CANCELLED
    return MediaDownloadJobState(public_state)


# Fail-closed allowlist: only listed edges are legal.
_ALLOWED_TRANSITIONS: dict[MediaDownloadJobState, frozenset[MediaDownloadJobState]] = {
    MediaDownloadJobState.QUEUED: frozenset(
        {
            MediaDownloadJobState.DOWNLOADING,
            MediaDownloadJobState.CANCELLED,
            MediaDownloadJobState.EXPIRED,
        }
    ),
    MediaDownloadJobState.DOWNLOADING: frozenset(
        {
            MediaDownloadJobState.QUEUED,  # retry / reclaim / worker shutdown
            MediaDownloadJobState.READY,
            MediaDownloadJobState.FAILED,
            MediaDownloadJobState.CANCELLED,
            MediaDownloadJobState.EXPIRED,
        }
    ),
    MediaDownloadJobState.READY: frozenset({MediaDownloadJobState.EXPIRED}),
    MediaDownloadJobState.FAILED: frozenset({MediaDownloadJobState.EXPIRED}),
    MediaDownloadJobState.CANCELLED: frozenset({MediaDownloadJobState.EXPIRED}),
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
        MediaDownloadJobState.CANCELLED,
        MediaDownloadJobState.EXPIRED,
    }
