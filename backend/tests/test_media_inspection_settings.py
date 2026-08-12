"""Settings tests for media inspection fail-closed configuration."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fetchnow.core.config import Settings


def test_media_inspection_disabled_by_default() -> None:
    s = Settings(
        APP_ENV="test",
        DATABASE_URL="postgresql+asyncpg://fetchnow:fetchnow@localhost:5432/fetchnow",
    )
    assert s.media_inspection_enabled is False
    assert s.media_inspection_ytdlp_path == ""


def test_enabled_requires_executable_path() -> None:
    with pytest.raises(ValidationError):
        Settings(
            APP_ENV="test",
            DATABASE_URL="postgresql+asyncpg://fetchnow:fetchnow@localhost:5432/fetchnow",
            MEDIA_INSPECTION_ENABLED=True,
            MEDIA_INSPECTION_YTDLP_PATH="",
        )


def test_socket_timeout_must_not_exceed_hard_timeout() -> None:
    with pytest.raises(ValidationError):
        Settings(
            APP_ENV="test",
            DATABASE_URL="postgresql+asyncpg://fetchnow:fetchnow@localhost:5432/fetchnow",
            MEDIA_INSPECTION_TIMEOUT_SECONDS=5,
            MEDIA_INSPECTION_SOCKET_TIMEOUT_SECONDS=10,
        )


def test_stdout_stderr_have_upper_bounds() -> None:
    with pytest.raises(ValidationError):
        Settings(
            APP_ENV="test",
            DATABASE_URL="postgresql+asyncpg://fetchnow:fetchnow@localhost:5432/fetchnow",
            MEDIA_INSPECTION_STDOUT_MAX_BYTES=50_000_000,
        )
    with pytest.raises(ValidationError):
        Settings(
            APP_ENV="test",
            DATABASE_URL="postgresql+asyncpg://fetchnow:fetchnow@localhost:5432/fetchnow",
            MEDIA_INSPECTION_STDERR_MAX_BYTES=50_000_000,
        )


def test_temp_root_must_be_absolute_when_set() -> None:
    with pytest.raises(ValidationError):
        Settings(
            APP_ENV="test",
            DATABASE_URL="postgresql+asyncpg://fetchnow:fetchnow@localhost:5432/fetchnow",
            MEDIA_INSPECTION_TEMP_ROOT="relative/path",
        )


def test_metadata_concurrency_not_in_settings() -> None:
    s = Settings(
        APP_ENV="test",
        DATABASE_URL="postgresql+asyncpg://fetchnow:fetchnow@localhost:5432/fetchnow",
        METADATA_CONCURRENCY=9,
    )
    assert not hasattr(s, "metadata_concurrency")


def test_format_entry_setting_512_accepted() -> None:
    s = Settings(
        APP_ENV="test",
        DATABASE_URL="postgresql+asyncpg://fetchnow:fetchnow@localhost:5432/fetchnow",
        MEDIA_INSPECTION_MAX_FORMAT_ENTRIES=512,
    )
    assert s.media_inspection_max_format_entries == 512


def test_format_entry_setting_513_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(
            APP_ENV="test",
            DATABASE_URL="postgresql+asyncpg://fetchnow:fetchnow@localhost:5432/fetchnow",
            MEDIA_INSPECTION_MAX_FORMAT_ENTRIES=513,
        )


def test_parser_and_settings_limits_coherent() -> None:
    import copy

    from fetchnow.media_inspection.errors import InspectionError
    from fetchnow.media_inspection.ytdlp_parse import (
        _DEFAULT_MAX_LIST_LEN,
        parse_ytdlp_json,
    )
    from media_inspection_fixtures import VK_FIXTURE, dumps_fixture

    assert _DEFAULT_MAX_LIST_LEN == 512
    s = Settings(
        APP_ENV="test",
        DATABASE_URL="postgresql+asyncpg://fetchnow:fetchnow@localhost:5432/fetchnow",
        MEDIA_INSPECTION_MAX_FORMAT_ENTRIES=512,
    )
    assert s.media_inspection_max_format_entries <= _DEFAULT_MAX_LIST_LEN
    assert s.media_inspection_max_format_entries == 512

    def _fmt(i: int) -> dict[str, object]:
        return {
            "format_id": f"f{i}",
            "ext": "mp4",
            "width": 640,
            "height": 360,
            "vcodec": "avc1",
            "acodec": "mp4a",
            "url": f"https://example.invalid/{i}",
        }

    base_kwargs = dict(
        expected_provider_id="vk",
        expected_canonical_url="https://vk.com/video-123_456239017",
        expected_media_id="-123_456239017",
        allowed_extractor_keys=frozenset({"vk"}),
        allowed_hostnames=frozenset(
            {"vk.com", "www.vk.com", "m.vk.com", "vkvideo.ru"}
        ),
        max_height=2160,
        max_width=3840,
        max_bytes=10**9,
        max_duration=3600,
        tool_version=None,
        max_format_entries=s.media_inspection_max_format_entries,
    )

    ok_payload = copy.deepcopy(VK_FIXTURE)
    ok_payload["formats"] = [_fmt(i) for i in range(512)]
    draft = parse_ytdlp_json(dumps_fixture(ok_payload), **base_kwargs)
    assert draft is not None

    bad_payload = copy.deepcopy(VK_FIXTURE)
    bad_payload["formats"] = [_fmt(i) for i in range(513)]
    with pytest.raises(InspectionError):
        parse_ytdlp_json(dumps_fixture(bad_payload), **base_kwargs)
