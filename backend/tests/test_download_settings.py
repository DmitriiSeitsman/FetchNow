"""Settings tests for durable download execution fail-closed configuration."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from fetchnow.core.config import Settings

_DB = "postgresql+asyncpg://fetchnow:fetchnow@localhost:5432/fetchnow"


def test_media_downloads_disabled_by_default() -> None:
    s = Settings(APP_ENV="test", DATABASE_URL=_DB)
    assert s.media_downloads_enabled is False
    assert s.media_download_concurrency == 1
    assert s.media_download_temp_root == ""


def test_api_settings_enabled_without_ytdlp_or_temp_root() -> None:
    """API may enable downloads without worker-only tool path / artifact root."""
    s = Settings(
        APP_ENV="test",
        DATABASE_URL=_DB,
        MEDIA_DOWNLOADS_ENABLED=True,
    )
    assert s.media_downloads_enabled is True
    assert s.media_inspection_ytdlp_path == ""
    assert s.media_download_temp_root == ""


def test_concurrency_must_be_exactly_one() -> None:
    for bad in (0, 2, 4, 5):
        with pytest.raises(ValidationError):
            Settings(
                APP_ENV="test",
                DATABASE_URL=_DB,
                MEDIA_DOWNLOAD_CONCURRENCY=bad,
            )
    s = Settings(
        APP_ENV="test",
        DATABASE_URL=_DB,
        MEDIA_DOWNLOAD_CONCURRENCY=1,
    )
    assert s.media_download_concurrency == 1


def test_temp_root_must_be_absolute_when_set() -> None:
    with pytest.raises(ValidationError):
        Settings(
            APP_ENV="test",
            DATABASE_URL=_DB,
            MEDIA_DOWNLOAD_TEMP_ROOT="relative/downloads",
        )


def test_socket_timeout_must_not_exceed_hard_timeout() -> None:
    with pytest.raises(ValidationError):
        Settings(
            APP_ENV="test",
            DATABASE_URL=_DB,
            MEDIA_DOWNLOAD_TIMEOUT_SECONDS=10,
            MEDIA_DOWNLOAD_SOCKET_TIMEOUT_SECONDS=30,
        )


def test_retry_base_must_not_exceed_max() -> None:
    with pytest.raises(ValidationError):
        Settings(
            APP_ENV="test",
            DATABASE_URL=_DB,
            MEDIA_DOWNLOAD_RETRY_BASE_SECONDS=60,
            MEDIA_DOWNLOAD_RETRY_MAX_SECONDS=10,
        )


def test_download_max_bytes_must_not_exceed_source_cap() -> None:
    with pytest.raises(ValidationError):
        Settings(
            APP_ENV="test",
            DATABASE_URL=_DB,
            MAX_SOURCE_FILE_BYTES=1000,
            MEDIA_DOWNLOAD_MAX_BYTES=2000,
        )


def test_orphan_grace_must_stay_above_publication_race_window() -> None:
    for bad in (0, 1, 59):
        with pytest.raises(ValidationError):
            Settings(
                APP_ENV="test",
                DATABASE_URL=_DB,
                MEDIA_DOWNLOAD_ORPHAN_GRACE_SECONDS=bad,
            )
    s = Settings(
        APP_ENV="test",
        DATABASE_URL=_DB,
        MEDIA_DOWNLOAD_ORPHAN_GRACE_SECONDS=60,
    )
    assert s.media_download_orphan_grace_seconds == 60


def test_muxing_disabled_by_default() -> None:
    s = Settings(APP_ENV="test", DATABASE_URL=_DB)
    assert s.media_muxing_enabled is False
    assert s.media_muxing_ffmpeg_path == ""
    assert s.media_muxing_ffprobe_path == ""


def test_api_settings_muxing_enabled_without_tool_paths() -> None:
    """API may enable muxing projection without worker ffmpeg/ffprobe paths."""
    s = Settings(
        APP_ENV="test",
        DATABASE_URL=_DB,
        MEDIA_MUXING_ENABLED=True,
    )
    assert s.media_muxing_enabled is True
    assert s.media_muxing_ffmpeg_path == ""
    assert s.media_muxing_ffprobe_path == ""


def test_muxing_tool_paths_must_be_absolute_when_set() -> None:
    with pytest.raises(ValidationError):
        Settings(
            APP_ENV="test",
            DATABASE_URL=_DB,
            MEDIA_MUXING_FFMPEG_PATH="usr/bin/ffmpeg",
        )
    with pytest.raises(ValidationError):
        Settings(
            APP_ENV="test",
            DATABASE_URL=_DB,
            MEDIA_MUXING_FFPROBE_PATH="usr/bin/ffprobe",
        )


def test_worker_execution_settings_fail_closed_without_ytdlp(
    tmp_path: Path,
) -> None:
    from fetchnow.downloads.errors import DownloadError
    from fetchnow.downloads.executor import DownloadExecutor

    settings = Settings(
        APP_ENV="test",
        DATABASE_URL=_DB,
        MEDIA_DOWNLOADS_ENABLED=True,
        MEDIA_DOWNLOAD_TEMP_ROOT=str(tmp_path / "dl"),
        MEDIA_INSPECTION_YTDLP_PATH="",
    )
    (tmp_path / "dl").mkdir(mode=0o700)
    with pytest.raises(DownloadError):
        DownloadExecutor.validate_worker_execution_settings(settings)
