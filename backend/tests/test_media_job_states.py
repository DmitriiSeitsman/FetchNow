"""Unit tests for media-job public state machine."""

from __future__ import annotations

import pytest

from fetchnow.jobs.errors import JobError, JobErrorCode
from fetchnow.jobs.states import MediaJobState, assert_transition, is_terminal


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (MediaJobState.QUEUED, MediaJobState.INSPECTING),
        (MediaJobState.QUEUED, MediaJobState.EXPIRED),
        (MediaJobState.INSPECTING, MediaJobState.QUEUED),
        (MediaJobState.INSPECTING, MediaJobState.INSPECTED),
        (MediaJobState.INSPECTING, MediaJobState.FAILED),
        (MediaJobState.INSPECTING, MediaJobState.EXPIRED),
        (MediaJobState.INSPECTED, MediaJobState.EXPIRED),
        (MediaJobState.FAILED, MediaJobState.EXPIRED),
    ],
)
def test_legal_transitions(src: MediaJobState, dst: MediaJobState) -> None:
    assert_transition(src, dst)
    assert_transition(src.value, dst.value)


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (MediaJobState.QUEUED, MediaJobState.INSPECTED),
        (MediaJobState.QUEUED, MediaJobState.FAILED),
        (MediaJobState.QUEUED, MediaJobState.QUEUED),
        (MediaJobState.INSPECTED, MediaJobState.QUEUED),
        (MediaJobState.INSPECTED, MediaJobState.INSPECTING),
        (MediaJobState.FAILED, MediaJobState.QUEUED),
        (MediaJobState.FAILED, MediaJobState.INSPECTING),
        (MediaJobState.EXPIRED, MediaJobState.QUEUED),
        (MediaJobState.EXPIRED, MediaJobState.INSPECTING),
        (MediaJobState.EXPIRED, MediaJobState.EXPIRED),
        (MediaJobState.INSPECTED, MediaJobState.FAILED),
    ],
)
def test_illegal_transitions_fail_closed(
    src: MediaJobState, dst: MediaJobState
) -> None:
    with pytest.raises(JobError) as exc_info:
        assert_transition(src, dst)
    assert exc_info.value.code == JobErrorCode.INTERNAL_ERROR
    assert exc_info.value.internal_reason == "ILLEGAL_STATE_TRANSITION"


def test_unknown_state_fail_closed() -> None:
    with pytest.raises(JobError) as exc_info:
        assert_transition("downloading", MediaJobState.QUEUED)
    assert exc_info.value.code == JobErrorCode.INTERNAL_ERROR
    assert exc_info.value.internal_reason == "UNKNOWN_JOB_STATE"


def test_terminal_helpers() -> None:
    assert is_terminal(MediaJobState.INSPECTED) is True
    assert is_terminal(MediaJobState.FAILED) is True
    assert is_terminal(MediaJobState.EXPIRED) is True
    assert is_terminal(MediaJobState.QUEUED) is False
    assert is_terminal(MediaJobState.INSPECTING) is False
    assert is_terminal("bogus") is True
