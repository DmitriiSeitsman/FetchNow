"""Fail-closed Free quota settings tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fetchnow.core.config import Settings


def test_free_quota_defaults_fail_closed_with_intended_policy() -> None:
    settings = Settings()
    assert settings.free_download_quota_enabled is False
    assert settings.free_download_limit == 3
    assert settings.free_download_window_seconds == 86_400
    assert settings.anonymous_client_ttl_seconds == 31_536_000
    assert settings.free_download_quota_retention_seconds == 172_800


@pytest.mark.parametrize(
    "overrides",
    [
        {"FREE_DOWNLOAD_LIMIT": 0},
        {"FREE_DOWNLOAD_WINDOW_SECONDS": 59},
        {
            "FREE_DOWNLOAD_WINDOW_SECONDS": 86_400,
            "ANONYMOUS_CLIENT_TTL_SECONDS": 86_400,
        },
        {
            "FREE_DOWNLOAD_WINDOW_SECONDS": 172_800,
            "FREE_DOWNLOAD_QUOTA_RETENTION_SECONDS": 172_800,
        },
    ],
)
def test_invalid_quota_policy_fails_startup(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings(**overrides)  # type: ignore[arg-type]
