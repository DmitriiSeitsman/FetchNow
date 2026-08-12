"""Centralized media-job public state machine."""

from __future__ import annotations

from enum import StrEnum

from fetchnow.jobs.errors import JobErrorCode, raise_job_error


class MediaJobState(StrEnum):
    """Inspection-oriented public job states (PR5)."""

    QUEUED = "queued"
    INSPECTING = "inspecting"
    INSPECTED = "inspected"
    FAILED = "failed"
    EXPIRED = "expired"


# Fail-closed allowlist: only listed edges are legal.
_ALLOWED_TRANSITIONS: dict[MediaJobState, frozenset[MediaJobState]] = {
    MediaJobState.QUEUED: frozenset(
        {
            MediaJobState.INSPECTING,
            MediaJobState.EXPIRED,
        }
    ),
    MediaJobState.INSPECTING: frozenset(
        {
            MediaJobState.QUEUED,  # retry / reclaim / cancel release
            MediaJobState.INSPECTED,
            MediaJobState.FAILED,
            MediaJobState.EXPIRED,
        }
    ),
    MediaJobState.INSPECTED: frozenset({MediaJobState.EXPIRED}),
    MediaJobState.FAILED: frozenset({MediaJobState.EXPIRED}),
    MediaJobState.EXPIRED: frozenset(),
}


def assert_transition(
    from_state: MediaJobState | str,
    to_state: MediaJobState | str,
) -> None:
    """Raise INTERNAL_ERROR when a state transition is not explicitly allowed."""
    try:
        src = (
            from_state
            if isinstance(from_state, MediaJobState)
            else MediaJobState(str(from_state))
        )
        dst = (
            to_state
            if isinstance(to_state, MediaJobState)
            else MediaJobState(str(to_state))
        )
    except ValueError:
        raise_job_error(
            JobErrorCode.INTERNAL_ERROR,
            internal_reason="UNKNOWN_JOB_STATE",
        )
    allowed = _ALLOWED_TRANSITIONS.get(src, frozenset())
    if dst not in allowed:
        raise_job_error(
            JobErrorCode.INTERNAL_ERROR,
            internal_reason="ILLEGAL_STATE_TRANSITION",
        )


def is_terminal(state: MediaJobState | str) -> bool:
    """Return True for states that no longer accept worker claims."""
    try:
        value = state if isinstance(state, MediaJobState) else MediaJobState(str(state))
    except ValueError:
        return True
    return value in {
        MediaJobState.INSPECTED,
        MediaJobState.FAILED,
        MediaJobState.EXPIRED,
    }
