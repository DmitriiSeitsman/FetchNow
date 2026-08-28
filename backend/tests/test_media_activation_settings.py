"""Production downloader activation settings stay fail-closed and role-split."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from fetchnow.core.config import Settings

_DB = "postgresql+asyncpg://fetchnow:fetchnow@localhost:5432/fetchnow"


def test_api_activation_bundle_does_not_require_worker_tool_path() -> None:
    s = Settings(
        APP_ENV="test",
        DATABASE_URL=_DB,
        MEDIA_JOBS_ENABLED=True,
        MEDIA_DOWNLOADS_ENABLED=True,
        MEDIA_BROWSER_DELIVERY_ENABLED=True,
        MEDIA_MUXING_ENABLED=False,
    )
    assert s.media_jobs_enabled is True
    assert s.media_downloads_enabled is True
    assert s.media_browser_delivery_enabled is True
    assert s.media_inspection_enabled is False
    assert s.media_inspection_ytdlp_path == ""
    assert s.media_muxing_enabled is False


def test_worker_activation_bundle_requires_absolute_ytdlp_path() -> None:
    s = Settings(
        APP_ENV="test",
        DATABASE_URL=_DB,
        MEDIA_INSPECTION_ENABLED=True,
        MEDIA_INSPECTION_YTDLP_PATH="/opt/venv/bin/yt-dlp",
        MEDIA_JOBS_ENABLED=True,
        MEDIA_DOWNLOADS_ENABLED=True,
        MEDIA_BROWSER_DELIVERY_ENABLED=True,
        MEDIA_MUXING_ENABLED=False,
    )
    assert s.media_inspection_ytdlp_path == "/opt/venv/bin/yt-dlp"
    assert s.media_muxing_enabled is False


def test_delivery_activation_bundle_requires_absolute_root() -> None:
    with pytest.raises(ValidationError):
        Settings(
            APP_ENV="test",
            DATABASE_URL=_DB,
            MEDIA_DELIVERY_ENABLED=True,
            MEDIA_DELIVERY_ROOT="",
            MEDIA_BROWSER_DELIVERY_ENABLED=True,
        )
    s = Settings(
        APP_ENV="test",
        DATABASE_URL=_DB,
        MEDIA_DELIVERY_ENABLED=True,
        MEDIA_DELIVERY_ROOT="/var/lib/fetchnow/tmp/downloads",
        MEDIA_BROWSER_DELIVERY_ENABLED=True,
    )
    assert s.media_delivery_enabled is True
    assert s.media_browser_delivery_enabled is True


def test_worker_downloads_still_fail_closed_without_executable(
    tmp_path: Path,
) -> None:
    from fetchnow.downloads.errors import DownloadError
    from fetchnow.downloads.executor import DownloadExecutor

    settings = Settings(
        APP_ENV="test",
        DATABASE_URL=_DB,
        MEDIA_DOWNLOADS_ENABLED=True,
        MEDIA_INSPECTION_ENABLED=True,
        MEDIA_INSPECTION_YTDLP_PATH=str(tmp_path / "missing-ytdlp"),
        MEDIA_DOWNLOAD_TEMP_ROOT=str(tmp_path / "dl"),
    )
    (tmp_path / "dl").mkdir(mode=0o700)
    with pytest.raises(DownloadError):
        DownloadExecutor.validate_worker_execution_settings(settings)
