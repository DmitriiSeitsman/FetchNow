"""Settings tests for durable media-job orchestration."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fetchnow.core.config import Settings

_DB = "postgresql+asyncpg://fetchnow:fetchnow@localhost:5432/fetchnow"


def test_media_jobs_disabled_by_default() -> None:
    s = Settings(APP_ENV="test", DATABASE_URL=_DB)
    assert s.media_jobs_enabled is False


def test_lease_bounds() -> None:
    ok = Settings(
        APP_ENV="test",
        DATABASE_URL=_DB,
        MEDIA_JOB_LEASE_SECONDS=5,
    )
    assert ok.media_job_lease_seconds == 5.0
    ok_max = Settings(
        APP_ENV="test",
        DATABASE_URL=_DB,
        MEDIA_JOB_LEASE_SECONDS=600,
    )
    assert ok_max.media_job_lease_seconds == 600.0
    with pytest.raises(ValidationError):
        Settings(APP_ENV="test", DATABASE_URL=_DB, MEDIA_JOB_LEASE_SECONDS=4)
    with pytest.raises(ValidationError):
        Settings(APP_ENV="test", DATABASE_URL=_DB, MEDIA_JOB_LEASE_SECONDS=601)


def test_max_attempts_bounds() -> None:
    assert (
        Settings(
            APP_ENV="test", DATABASE_URL=_DB, MEDIA_JOB_MAX_ATTEMPTS=1
        ).media_job_max_attempts
        == 1
    )
    assert (
        Settings(
            APP_ENV="test", DATABASE_URL=_DB, MEDIA_JOB_MAX_ATTEMPTS=10
        ).media_job_max_attempts
        == 10
    )
    with pytest.raises(ValidationError):
        Settings(APP_ENV="test", DATABASE_URL=_DB, MEDIA_JOB_MAX_ATTEMPTS=0)
    with pytest.raises(ValidationError):
        Settings(APP_ENV="test", DATABASE_URL=_DB, MEDIA_JOB_MAX_ATTEMPTS=11)


def test_ttl_bounds() -> None:
    assert (
        Settings(
            APP_ENV="test", DATABASE_URL=_DB, MEDIA_JOB_ABSOLUTE_TTL_SECONDS=60
        ).media_job_absolute_ttl_seconds
        == 60
    )
    assert (
        Settings(
            APP_ENV="test",
            DATABASE_URL=_DB,
            MEDIA_JOB_ABSOLUTE_TTL_SECONDS=604_800,
        ).media_job_absolute_ttl_seconds
        == 604_800
    )
    with pytest.raises(ValidationError):
        Settings(
            APP_ENV="test", DATABASE_URL=_DB, MEDIA_JOB_ABSOLUTE_TTL_SECONDS=59
        )
    with pytest.raises(ValidationError):
        Settings(
            APP_ENV="test",
            DATABASE_URL=_DB,
            MEDIA_JOB_ABSOLUTE_TTL_SECONDS=604_801,
        )


def test_result_max_bytes_bounds() -> None:
    assert (
        Settings(
            APP_ENV="test", DATABASE_URL=_DB, MEDIA_JOB_RESULT_MAX_BYTES=1
        ).media_job_result_max_bytes
        == 1
    )
    assert (
        Settings(
            APP_ENV="test",
            DATABASE_URL=_DB,
            MEDIA_JOB_RESULT_MAX_BYTES=524_288,
        ).media_job_result_max_bytes
        == 524_288
    )
    with pytest.raises(ValidationError):
        Settings(
            APP_ENV="test", DATABASE_URL=_DB, MEDIA_JOB_RESULT_MAX_BYTES=0
        )
    with pytest.raises(ValidationError):
        Settings(
            APP_ENV="test",
            DATABASE_URL=_DB,
            MEDIA_JOB_RESULT_MAX_BYTES=524_289,
        )


def test_retry_base_greater_than_retry_max_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(
            APP_ENV="test",
            DATABASE_URL=_DB,
            MEDIA_JOB_RETRY_BASE_SECONDS=30,
            MEDIA_JOB_RETRY_MAX_SECONDS=10,
        )
