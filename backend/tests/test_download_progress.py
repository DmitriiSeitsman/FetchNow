"""Public progress stage transitions and sanitization."""

from __future__ import annotations

import pytest

from fetchnow.downloads.errors import DownloadError, DownloadErrorCode
from fetchnow.downloads.progress import (
    DownloadProgressStage,
    assert_progress_transition,
    is_terminal_progress,
    progress_for_public_state,
)
from fetchnow.downloads.states import MediaDownloadJobState


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (DownloadProgressStage.QUEUED, DownloadProgressStage.INSPECTING),
        (DownloadProgressStage.INSPECTING, DownloadProgressStage.DOWNLOADING_VIDEO),
        (
            DownloadProgressStage.DOWNLOADING_VIDEO,
            DownloadProgressStage.DOWNLOADING_AUDIO,
        ),
        (DownloadProgressStage.DOWNLOADING_AUDIO, DownloadProgressStage.MUXING),
        (DownloadProgressStage.MUXING, DownloadProgressStage.VERIFYING),
        (DownloadProgressStage.VERIFYING, DownloadProgressStage.PUBLISHING),
        (DownloadProgressStage.PUBLISHING, DownloadProgressStage.READY),
        (DownloadProgressStage.DOWNLOADING_VIDEO, DownloadProgressStage.RETRYING),
        (DownloadProgressStage.RETRYING, DownloadProgressStage.INSPECTING),
        (DownloadProgressStage.QUEUED, DownloadProgressStage.CANCELLED),
        (DownloadProgressStage.READY, DownloadProgressStage.EXPIRED),
    ],
)
def test_legal_progress_transitions(
    src: DownloadProgressStage, dst: DownloadProgressStage
) -> None:
    assert_progress_transition(src, dst)


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (DownloadProgressStage.READY, DownloadProgressStage.QUEUED),
        (DownloadProgressStage.READY, DownloadProgressStage.INSPECTING),
        (DownloadProgressStage.FAILED, DownloadProgressStage.DOWNLOADING_VIDEO),
        (DownloadProgressStage.CANCELLED, DownloadProgressStage.MUXING),
        (DownloadProgressStage.EXPIRED, DownloadProgressStage.QUEUED),
        (DownloadProgressStage.QUEUED, DownloadProgressStage.READY),
        (DownloadProgressStage.INSPECTING, DownloadProgressStage.READY),
        (DownloadProgressStage.PUBLISHING, DownloadProgressStage.QUEUED),
    ],
)
def test_illegal_and_terminal_progress_rejected(
    src: DownloadProgressStage, dst: DownloadProgressStage
) -> None:
    with pytest.raises(DownloadError) as exc:
        assert_progress_transition(src, dst)
    assert exc.value.code == DownloadErrorCode.INTERNAL_ERROR
    assert exc.value.internal_reason in {
        "ILLEGAL_PROGRESS_TRANSITION",
        "TERMINAL_PROGRESS_IMMUTABLE",
    }


def test_terminal_progress_is_immutable_except_expiry() -> None:
    assert is_terminal_progress(DownloadProgressStage.READY) is True
    assert is_terminal_progress(DownloadProgressStage.FAILED) is True
    assert is_terminal_progress(DownloadProgressStage.CANCELLED) is True
    assert is_terminal_progress(DownloadProgressStage.EXPIRED) is True
    assert is_terminal_progress(DownloadProgressStage.MUXING) is False
    assert_progress_transition(
        DownloadProgressStage.CANCELLED, DownloadProgressStage.EXPIRED
    )
    with pytest.raises(DownloadError):
        assert_progress_transition(
            DownloadProgressStage.CANCELLED, DownloadProgressStage.READY
        )


def test_progress_maps_from_public_state() -> None:
    assert progress_for_public_state(MediaDownloadJobState.QUEUED) is (
        DownloadProgressStage.QUEUED
    )
    assert progress_for_public_state(MediaDownloadJobState.DOWNLOADING) is (
        DownloadProgressStage.INSPECTING
    )
    assert progress_for_public_state("cancelled") is DownloadProgressStage.CANCELLED
    assert progress_for_public_state("bogus") is DownloadProgressStage.FAILED
