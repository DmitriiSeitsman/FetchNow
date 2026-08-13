"""Unit tests for media-download-job public state machine."""

from __future__ import annotations

import pytest

from fetchnow.downloads.errors import DownloadError, DownloadErrorCode
from fetchnow.downloads.states import (
    PR9_STORED_PUBLIC_STATES,
    MediaDownloadJobState,
    assert_transition,
    is_stored_cancelled,
    is_terminal,
    project_public_state,
)


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (MediaDownloadJobState.QUEUED, MediaDownloadJobState.DOWNLOADING),
        (MediaDownloadJobState.QUEUED, MediaDownloadJobState.CANCELLED),
        (MediaDownloadJobState.DOWNLOADING, MediaDownloadJobState.CANCELLED),
        (MediaDownloadJobState.CANCELLED, MediaDownloadJobState.EXPIRED),
        (MediaDownloadJobState.DOWNLOADING, MediaDownloadJobState.QUEUED),
        (MediaDownloadJobState.DOWNLOADING, MediaDownloadJobState.READY),
        (MediaDownloadJobState.DOWNLOADING, MediaDownloadJobState.FAILED),
        (MediaDownloadJobState.DOWNLOADING, MediaDownloadJobState.EXPIRED),
        (MediaDownloadJobState.READY, MediaDownloadJobState.EXPIRED),
        (MediaDownloadJobState.FAILED, MediaDownloadJobState.EXPIRED),
    ],
)
def test_legal_transitions(
    src: MediaDownloadJobState, dst: MediaDownloadJobState
) -> None:
    assert_transition(src, dst)
    assert_transition(src.value, dst.value)


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (MediaDownloadJobState.QUEUED, MediaDownloadJobState.READY),
        (MediaDownloadJobState.QUEUED, MediaDownloadJobState.FAILED),
        (MediaDownloadJobState.QUEUED, MediaDownloadJobState.QUEUED),
        (MediaDownloadJobState.READY, MediaDownloadJobState.QUEUED),
        (MediaDownloadJobState.READY, MediaDownloadJobState.DOWNLOADING),
        (MediaDownloadJobState.FAILED, MediaDownloadJobState.QUEUED),
        (MediaDownloadJobState.FAILED, MediaDownloadJobState.DOWNLOADING),
        (MediaDownloadJobState.EXPIRED, MediaDownloadJobState.QUEUED),
        (MediaDownloadJobState.EXPIRED, MediaDownloadJobState.DOWNLOADING),
        (MediaDownloadJobState.EXPIRED, MediaDownloadJobState.EXPIRED),
        (MediaDownloadJobState.CANCELLED, MediaDownloadJobState.QUEUED),
        (MediaDownloadJobState.CANCELLED, MediaDownloadJobState.DOWNLOADING),
        (MediaDownloadJobState.CANCELLED, MediaDownloadJobState.READY),
        (MediaDownloadJobState.CANCELLED, MediaDownloadJobState.FAILED),
        (MediaDownloadJobState.READY, MediaDownloadJobState.CANCELLED),
        (MediaDownloadJobState.FAILED, MediaDownloadJobState.CANCELLED),
    ],
)
def test_illegal_transitions_fail_closed(
    src: MediaDownloadJobState, dst: MediaDownloadJobState
) -> None:
    with pytest.raises(DownloadError) as exc_info:
        assert_transition(src, dst)
    assert exc_info.value.code == DownloadErrorCode.INTERNAL_ERROR
    assert exc_info.value.internal_reason == "ILLEGAL_STATE_TRANSITION"


def test_unknown_state_fail_closed() -> None:
    with pytest.raises(DownloadError) as exc_info:
        assert_transition("inspecting", MediaDownloadJobState.QUEUED)
    assert exc_info.value.code == DownloadErrorCode.INTERNAL_ERROR
    assert exc_info.value.internal_reason == "UNKNOWN_DOWNLOAD_STATE"


def test_terminal_helpers() -> None:
    assert is_terminal(MediaDownloadJobState.READY) is True
    assert is_terminal(MediaDownloadJobState.FAILED) is True
    assert is_terminal(MediaDownloadJobState.CANCELLED) is True
    assert is_terminal(MediaDownloadJobState.QUEUED) is False
    assert is_terminal(MediaDownloadJobState.DOWNLOADING) is False
    assert is_terminal("bogus") is True


def test_stored_cancelled_encoding_is_pr9_expired() -> None:
    marker = object()
    assert is_stored_cancelled("expired", "cancelled", marker) is True
    assert is_stored_cancelled("expired", "expired", marker) is False
    assert is_stored_cancelled("expired", "cancelled", None) is False
    assert is_stored_cancelled("cancelled", "cancelled", marker) is False
    assert is_stored_cancelled("queued", "cancelled", marker) is False
    assert project_public_state("expired", "cancelled", marker) is (
        MediaDownloadJobState.CANCELLED
    )
    assert project_public_state("expired", "expired", None) is (
        MediaDownloadJobState.EXPIRED
    )
    assert "cancelled" not in PR9_STORED_PUBLIC_STATES
    assert MediaDownloadJobState.EXPIRED.value in PR9_STORED_PUBLIC_STATES
