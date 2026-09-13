"""Pure interval-union tests for delivery accounting."""

import pytest
from pydantic import ValidationError

from fetchnow.core.config import Settings
from fetchnow.quota.delivery import normalize_intervals


def test_interval_union_merges_overlap_adjacency_and_replay() -> None:
    assert normalize_intervals(
        [(8, 10), (0, 4), (3, 6), (6, 8), (0, 4), (5, 5)]
    ) == [(0, 10)]


def test_interval_union_preserves_disjoint_gaps() -> None:
    assert normalize_intervals([(4, 7), (0, 1), (9, 10)]) == [
        (0, 1),
        (4, 7),
        (9, 10),
    ]


def test_ready_compatibility_mode_defaults_fail_safe_and_accepts_false() -> None:
    assert Settings(APP_ENV="test").free_download_quota_ready_compatibility_mode
    assert (
        Settings(
            APP_ENV="test",
            FREE_DOWNLOAD_QUOTA_READY_COMPATIBILITY_MODE="false",
        ).free_download_quota_ready_compatibility_mode
        is False
    )


def test_invalid_ready_compatibility_mode_fails_startup() -> None:
    with pytest.raises(ValidationError):
        Settings(
            APP_ENV="test",
            FREE_DOWNLOAD_QUOTA_READY_COMPATIBILITY_MODE="invalid",
        )
