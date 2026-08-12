"""Unit tests for media-download-job public state machine."""

from __future__ import annotations

import pytest

from fetchnow.downloads.errors import DownloadError, DownloadErrorCode
from fetchnow.downloads.states import (
    MediaDownloadJobState,
    assert_transition,
    is_terminal,
)


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (MediaDownloadJobState.QUEUED, MediaDownloadJobState.DOWNLOADING),
        (MediaDownloadJobState.QUEUED, MediaDownloadJobState.EXPIRED),
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
        (MediaDownloadJobState.READY, MediaDownloadJobState.FAILED),
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
    assert is_terminal(MediaDownloadJobState.EXPIRED) is True
    assert is_terminal(MediaDownloadJobState.QUEUED) is False
    assert is_terminal(MediaDownloadJobState.DOWNLOADING) is False
    assert is_terminal("bogus") is True
