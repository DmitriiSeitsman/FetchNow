"""Application configuration loaded exclusively from environment variables."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Annotated, Any

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _split_csv(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    raise TypeError(f"Expected CSV string or list, got {type(value)!r}")


def _split_csv_ports(value: Any) -> list[int]:
    parts = _split_csv(value)
    ports: list[int] = []
    for part in parts:
        port = int(part)
        if port < 1 or port > 65535:
            raise ValueError(f"Invalid port: {part}")
        ports.append(port)
    return ports


class Settings(BaseSettings):
    """Runtime settings for API and worker processes."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    app_name: str = Field(default="FetchNow", alias="APP_NAME")
    app_env: str = Field(default="production", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    api_host: str = Field(default="0.0.0.0", alias="API_HOST")
    api_port: int = Field(default=8000, alias="API_PORT")

    database_url: str = Field(
        default="postgresql+asyncpg://fetchnow:fetchnow@localhost:5432/fetchnow",
        alias="DATABASE_URL",
    )

    worker_poll_interval_seconds: float = Field(
        default=5.0,
        alias="WORKER_POLL_INTERVAL_SECONDS",
        gt=0,
    )

    api_concurrency_limit: int = Field(default=32, alias="API_CONCURRENCY_LIMIT")
    worker_concurrency: int = Field(
        default=2,
        alias="WORKER_CONCURRENCY",
        ge=1,
        le=32,
    )
    temp_storage_path: str = Field(
        default="/var/lib/fetchnow/tmp",
        alias="TEMP_STORAGE_PATH",
    )
    temp_storage_max_bytes: int = Field(
        default=10 * 1024 * 1024 * 1024,
        alias="TEMP_STORAGE_MAX_BYTES",
    )

    # URL validation (PR1)
    url_allowed_schemes: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http", "https"],
        alias="URL_ALLOWED_SCHEMES",
    )
    url_allowed_ports: Annotated[list[int], NoDecode] = Field(
        default_factory=lambda: [80, 443],
        alias="URL_ALLOWED_PORTS",
    )
    url_max_length: int = Field(default=4096, alias="URL_MAX_LENGTH", gt=0)
    url_max_redirects: int = Field(default=5, alias="URL_MAX_REDIRECTS", ge=0)
    dns_resolution_timeout_seconds: float = Field(
        default=3.0,
        alias="DNS_RESOLUTION_TIMEOUT_SECONDS",
        gt=0,
    )
    provider_vk_enabled: bool = Field(default=True, alias="PROVIDER_VK_ENABLED")
    provider_rutube_enabled: bool = Field(default=True, alias="PROVIDER_RUTUBE_ENABLED")

    # Safe outbound HTTP (PR2)
    outbound_connect_timeout_seconds: float = Field(
        default=5.0,
        alias="OUTBOUND_CONNECT_TIMEOUT_SECONDS",
        gt=0,
    )
    outbound_read_timeout_seconds: float = Field(
        default=10.0,
        alias="OUTBOUND_READ_TIMEOUT_SECONDS",
        gt=0,
    )
    outbound_total_timeout_seconds: float = Field(
        default=15.0,
        alias="OUTBOUND_TOTAL_TIMEOUT_SECONDS",
        gt=0,
    )
    outbound_max_response_bytes: int = Field(
        default=1_048_576,
        alias="OUTBOUND_MAX_RESPONSE_BYTES",
        gt=0,
    )
    outbound_probe_body_bytes: int = Field(
        default=131_072,
        alias="OUTBOUND_PROBE_BODY_BYTES",
        gt=0,
    )
    outbound_user_agent: str = Field(
        default="FetchNow/1.0",
        alias="OUTBOUND_USER_AGENT",
        min_length=1,
    )
    outbound_allowed_content_types: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["text/html", "application/json", "text/plain"],
        alias="OUTBOUND_ALLOWED_CONTENT_TYPES",
    )

    # Wrapper resolution foundation (PR3A)
    wrapper_resolution_max_depth: int = Field(
        default=3,
        alias="WRAPPER_RESOLUTION_MAX_DEPTH",
        ge=1,
        le=8,
    )

    # Media inspection foundation (PR4) — fail closed / disabled by default.
    # yt-dlp performs its own network I/O outside SafeHTTPClient; keep off until
    # operators explicitly configure a trusted executable path.
    media_inspection_enabled: bool = Field(
        default=False,
        alias="MEDIA_INSPECTION_ENABLED",
    )
    media_inspection_ytdlp_path: str = Field(
        default="",
        alias="MEDIA_INSPECTION_YTDLP_PATH",
    )
    media_inspection_timeout_seconds: float = Field(
        default=30.0,
        alias="MEDIA_INSPECTION_TIMEOUT_SECONDS",
        gt=0,
        le=120,
    )
    media_inspection_socket_timeout_seconds: float = Field(
        default=10.0,
        alias="MEDIA_INSPECTION_SOCKET_TIMEOUT_SECONDS",
        gt=0,
        le=60,
    )
    media_inspection_stdout_max_bytes: int = Field(
        default=1_048_576,
        alias="MEDIA_INSPECTION_STDOUT_MAX_BYTES",
        gt=0,
        le=8_388_608,
    )
    media_inspection_stderr_max_bytes: int = Field(
        default=262_144,
        alias="MEDIA_INSPECTION_STDERR_MAX_BYTES",
        gt=0,
        le=2_097_152,
    )
    media_inspection_max_formats: int = Field(
        default=32,
        alias="MEDIA_INSPECTION_MAX_FORMATS",
        ge=1,
        le=128,
    )
    media_inspection_max_title_length: int = Field(
        default=200,
        alias="MEDIA_INSPECTION_MAX_TITLE_LENGTH",
        ge=1,
        le=500,
    )
    media_inspection_max_height: int = Field(
        default=2160,
        alias="MEDIA_INSPECTION_MAX_HEIGHT",
        ge=144,
        le=7680,
    )
    media_inspection_max_width: int = Field(
        default=3840,
        alias="MEDIA_INSPECTION_MAX_WIDTH",
        ge=144,
        le=7680,
    )
    media_inspection_temp_root: str = Field(
        default="",
        alias="MEDIA_INSPECTION_TEMP_ROOT",
    )
    media_inspection_json_max_depth: int = Field(
        default=12,
        alias="MEDIA_INSPECTION_JSON_MAX_DEPTH",
        ge=2,
        le=32,
    )
    media_inspection_json_max_nodes: int = Field(
        default=8_192,
        alias="MEDIA_INSPECTION_JSON_MAX_NODES",
        ge=16,
        le=65_536,
    )
    media_inspection_max_format_entries: int = Field(
        default=256,
        alias="MEDIA_INSPECTION_MAX_FORMAT_ENTRIES",
        ge=1,
        le=512,
    )
    max_source_duration_seconds: int = Field(
        default=7200,
        alias="MAX_SOURCE_DURATION_SECONDS",
        gt=0,
    )
    max_source_file_bytes: int = Field(
        default=3_221_225_472,
        alias="MAX_SOURCE_FILE_BYTES",
        gt=0,
    )

    # Durable media-job orchestration (PR5) — fail closed / disabled by default.
    # Client-generated access tokens; no server secret required.
    media_jobs_enabled: bool = Field(
        default=False,
        alias="MEDIA_JOBS_ENABLED",
    )
    media_job_lease_seconds: float = Field(
        default=60.0,
        alias="MEDIA_JOB_LEASE_SECONDS",
        ge=5,
        le=600,
    )
    media_job_shutdown_grace_seconds: float = Field(
        default=15.0,
        alias="MEDIA_JOB_SHUTDOWN_GRACE_SECONDS",
        gt=0,
        le=300,
    )
    media_job_max_attempts: int = Field(
        default=3,
        alias="MEDIA_JOB_MAX_ATTEMPTS",
        ge=1,
        le=10,
    )
    media_job_retry_base_seconds: float = Field(
        default=2.0,
        alias="MEDIA_JOB_RETRY_BASE_SECONDS",
        gt=0,
        le=600,
    )
    media_job_retry_max_seconds: float = Field(
        default=60.0,
        alias="MEDIA_JOB_RETRY_MAX_SECONDS",
        gt=0,
        le=3600,
    )
    media_job_absolute_ttl_seconds: int = Field(
        default=86_400,
        alias="MEDIA_JOB_ABSOLUTE_TTL_SECONDS",
        ge=60,
        le=604_800,
    )
    media_job_result_max_bytes: int = Field(
        default=65_536,
        alias="MEDIA_JOB_RESULT_MAX_BYTES",
        gt=0,
        le=524_288,
    )

    # Durable download execution (PR6) — fail closed / disabled by default.
    # No public file delivery. yt-dlp path reused from MEDIA_INSPECTION_YTDLP_PATH.
    # MEDIA_DOWNLOAD_CONCURRENCY defaults to 1: multi-worker disk reservation is
    # not implemented; raising concurrency risks overcommit without coordinated
    # free-space accounting across workers.
    media_downloads_enabled: bool = Field(
        default=False,
        alias="MEDIA_DOWNLOADS_ENABLED",
    )
    media_download_max_bytes: int = Field(
        default=3_221_225_472,
        alias="MEDIA_DOWNLOAD_MAX_BYTES",
        gt=0,
    )
    media_download_timeout_seconds: float = Field(
        default=300.0,
        alias="MEDIA_DOWNLOAD_TIMEOUT_SECONDS",
        gt=0,
        le=600,
    )
    media_download_concurrency: int = Field(
        default=1,
        alias="MEDIA_DOWNLOAD_CONCURRENCY",
        ge=1,
        le=1,
    )
    media_download_artifact_ttl_seconds: int = Field(
        default=3600,
        alias="MEDIA_DOWNLOAD_ARTIFACT_TTL_SECONDS",
        ge=60,
        le=86_400,
    )
    media_download_min_free_bytes: int = Field(
        default=2 * 1024 * 1024 * 1024,
        alias="MEDIA_DOWNLOAD_MIN_FREE_BYTES",
        ge=0,
    )
    media_download_temp_root: str = Field(
        default="",
        alias="MEDIA_DOWNLOAD_TEMP_ROOT",
    )
    media_download_stdout_max_bytes: int = Field(
        default=1_048_576,
        alias="MEDIA_DOWNLOAD_STDOUT_MAX_BYTES",
        gt=0,
        le=8_388_608,
    )
    media_download_stderr_max_bytes: int = Field(
        default=262_144,
        alias="MEDIA_DOWNLOAD_STDERR_MAX_BYTES",
        gt=0,
        le=2_097_152,
    )
    media_download_max_attempts: int = Field(
        default=3,
        alias="MEDIA_DOWNLOAD_MAX_ATTEMPTS",
        ge=1,
        le=10,
    )
    media_download_retry_base_seconds: float = Field(
        default=2.0,
        alias="MEDIA_DOWNLOAD_RETRY_BASE_SECONDS",
        gt=0,
        le=600,
    )
    media_download_retry_max_seconds: float = Field(
        default=60.0,
        alias="MEDIA_DOWNLOAD_RETRY_MAX_SECONDS",
        gt=0,
        le=3600,
    )
    # Grace must stay well above the widest publish→ready-commit window: a zero
    # or tiny grace lets one worker reconcile away a publication another worker
    # created moments earlier but has not yet committed.
    media_download_orphan_grace_seconds: int = Field(
        default=300,
        alias="MEDIA_DOWNLOAD_ORPHAN_GRACE_SECONDS",
        ge=60,
        le=86_400,
    )
    media_download_lease_seconds: float = Field(
        default=120.0,
        alias="MEDIA_DOWNLOAD_LEASE_SECONDS",
        ge=5,
        le=600,
    )
    media_download_socket_timeout_seconds: float = Field(
        default=30.0,
        alias="MEDIA_DOWNLOAD_SOCKET_TIMEOUT_SECONDS",
        gt=0,
        le=120,
    )

    # Authenticated private artifact delivery (PR7) — fail closed / disabled by
    # default. Delivery process only; API never mounts the artifact root.
    media_delivery_enabled: bool = Field(
        default=False,
        alias="MEDIA_DELIVERY_ENABLED",
    )
    media_delivery_root: str = Field(
        default="",
        alias="MEDIA_DELIVERY_ROOT",
    )
    media_delivery_chunk_bytes: int = Field(
        default=65_536,
        alias="MEDIA_DELIVERY_CHUNK_BYTES",
        ge=4_096,
        le=1_048_576,
    )
    # Process-local concurrency only — not a cross-replica rate limit.
    media_delivery_concurrency: int = Field(
        default=8,
        alias="MEDIA_DELIVERY_CONCURRENCY",
        ge=1,
        le=64,
    )
    media_delivery_range_enabled: bool = Field(
        default=True,
        alias="MEDIA_DELIVERY_RANGE_ENABLED",
    )

    # Browser-native delivery grants (PR14) — fail closed / disabled by default.
    # API issues grants; delivery authenticates the exact-path cookie.
    media_browser_delivery_enabled: bool = Field(
        default=False,
        alias="MEDIA_BROWSER_DELIVERY_ENABLED",
    )
    media_browser_grant_ttl_seconds: int = Field(
        default=300,
        alias="MEDIA_BROWSER_GRANT_TTL_SECONDS",
        ge=30,
        le=3600,
    )
    media_browser_grant_max_active: int = Field(
        default=4,
        alias="MEDIA_BROWSER_GRANT_MAX_ACTIVE",
        ge=1,
        le=16,
    )

    # Bounded stream-copy muxing (PR9) — fail closed / disabled by default.
    # ffmpeg/ffprobe paths are worker-only; API and delivery must not receive them.
    media_muxing_enabled: bool = Field(
        default=False,
        alias="MEDIA_MUXING_ENABLED",
    )
    media_muxing_ffmpeg_path: str = Field(
        default="",
        alias="MEDIA_MUXING_FFMPEG_PATH",
    )
    media_muxing_ffprobe_path: str = Field(
        default="",
        alias="MEDIA_MUXING_FFPROBE_PATH",
    )
    media_muxing_timeout_seconds: float = Field(
        default=90.0,
        alias="MEDIA_MUXING_TIMEOUT_SECONDS",
        gt=0,
        le=120,
    )
    media_muxing_max_stdout_bytes: int = Field(
        default=262_144,
        alias="MEDIA_MUXING_MAX_STDOUT_BYTES",
        gt=0,
        le=1_048_576,
    )
    media_muxing_max_stderr_bytes: int = Field(
        default=262_144,
        alias="MEDIA_MUXING_MAX_STDERR_BYTES",
        gt=0,
        le=1_048_576,
    )
    media_muxing_duration_tolerance_seconds: float = Field(
        default=2.0,
        alias="MEDIA_MUXING_DURATION_TOLERANCE_SECONDS",
        ge=0,
        le=30,
    )
    media_muxing_max_video_candidates: int = Field(
        default=8,
        alias="MEDIA_MUXING_MAX_VIDEO_CANDIDATES",
        ge=1,
        le=32,
    )
    media_muxing_max_audio_candidates: int = Field(
        default=8,
        alias="MEDIA_MUXING_MAX_AUDIO_CANDIDATES",
        ge=1,
        le=32,
    )
    media_muxing_max_derived_options: int = Field(
        default=8,
        alias="MEDIA_MUXING_MAX_DERIVED_OPTIONS",
        ge=1,
        le=32,
    )

    @field_validator("url_allowed_schemes", mode="before")
    @classmethod
    def _parse_schemes(cls, value: Any) -> list[str]:
        return [item.lower() for item in _split_csv(value)]

    @field_validator("url_allowed_ports", mode="before")
    @classmethod
    def _parse_ports(cls, value: Any) -> list[int]:
        return _split_csv_ports(value)

    @field_validator("outbound_allowed_content_types", mode="before")
    @classmethod
    def _parse_content_types(cls, value: Any) -> list[str]:
        return [item.lower() for item in _split_csv(value)]

    @model_validator(mode="after")
    def _validate_url_policy(self) -> Settings:
        if not self.url_allowed_schemes:
            raise ValueError("URL_ALLOWED_SCHEMES must not be empty")
        if not self.url_allowed_ports:
            raise ValueError("URL_ALLOWED_PORTS must not be empty")
        for scheme in self.url_allowed_schemes:
            if scheme not in {"http", "https"}:
                raise ValueError(f"Unsupported configured scheme: {scheme}")
        if self.url_max_redirects > 20:
            raise ValueError("URL_MAX_REDIRECTS must be <= 20")
        if not self.outbound_allowed_content_types:
            raise ValueError("OUTBOUND_ALLOWED_CONTENT_TYPES must not be empty")
        if self.outbound_probe_body_bytes > self.outbound_max_response_bytes:
            raise ValueError(
                "OUTBOUND_PROBE_BODY_BYTES must be <= OUTBOUND_MAX_RESPONSE_BYTES"
            )
        if self.outbound_total_timeout_seconds < self.outbound_connect_timeout_seconds:
            raise ValueError(
                "OUTBOUND_TOTAL_TIMEOUT_SECONDS must be >= "
                "OUTBOUND_CONNECT_TIMEOUT_SECONDS"
            )
        if (
            self.wrapper_resolution_max_depth < 1
            or self.wrapper_resolution_max_depth > 8
        ):
            raise ValueError("WRAPPER_RESOLUTION_MAX_DEPTH must be between 1 and 8")
        if self.media_inspection_enabled and not (
            self.media_inspection_ytdlp_path.strip()
        ):
            raise ValueError(
                "MEDIA_INSPECTION_YTDLP_PATH is required when "
                "MEDIA_INSPECTION_ENABLED is true"
            )
        if self.media_inspection_socket_timeout_seconds > (
            self.media_inspection_timeout_seconds
        ):
            raise ValueError(
                "MEDIA_INSPECTION_SOCKET_TIMEOUT_SECONDS must be <= "
                "MEDIA_INSPECTION_TIMEOUT_SECONDS"
            )
        temp_root = self.media_inspection_temp_root.strip()
        if temp_root and not os.path.isabs(temp_root):
            raise ValueError("MEDIA_INSPECTION_TEMP_ROOT must be absolute when set")
        if self.media_job_retry_base_seconds > self.media_job_retry_max_seconds:
            raise ValueError(
                "MEDIA_JOB_RETRY_BASE_SECONDS must be <= MEDIA_JOB_RETRY_MAX_SECONDS"
            )
        # MEDIA_DOWNLOADS_ENABLED must not require MEDIA_INSPECTION_YTDLP_PATH here:
        # the API container enables job creation without receiving the worker
        # executable path. Worker constructs validate executable/root at runtime.
        download_temp = self.media_download_temp_root.strip()
        if download_temp and not os.path.isabs(download_temp):
            raise ValueError("MEDIA_DOWNLOAD_TEMP_ROOT must be absolute when set")
        if (
            self.media_download_retry_base_seconds
            > self.media_download_retry_max_seconds
        ):
            raise ValueError(
                "MEDIA_DOWNLOAD_RETRY_BASE_SECONDS must be <= "
                "MEDIA_DOWNLOAD_RETRY_MAX_SECONDS"
            )
        if self.media_download_socket_timeout_seconds > (
            self.media_download_timeout_seconds
        ):
            raise ValueError(
                "MEDIA_DOWNLOAD_SOCKET_TIMEOUT_SECONDS must be <= "
                "MEDIA_DOWNLOAD_TIMEOUT_SECONDS"
            )
        if self.media_download_max_bytes > self.max_source_file_bytes:
            raise ValueError(
                "MEDIA_DOWNLOAD_MAX_BYTES must be <= MAX_SOURCE_FILE_BYTES"
            )
        # Fail-closed: only concurrency=1 until multi-worker disk reservation exists.
        if self.media_download_concurrency != 1:
            raise ValueError(
                "MEDIA_DOWNLOAD_CONCURRENCY must be 1 until cross-worker "
                "disk reservation is implemented"
            )
        delivery_root = self.media_delivery_root.strip()
        if delivery_root and not os.path.isabs(delivery_root):
            raise ValueError("MEDIA_DELIVERY_ROOT must be absolute when set")
        if self.media_delivery_enabled and not delivery_root:
            raise ValueError(
                "MEDIA_DELIVERY_ROOT is required when MEDIA_DELIVERY_ENABLED is true"
            )
        ffmpeg_path = self.media_muxing_ffmpeg_path.strip()
        ffprobe_path = self.media_muxing_ffprobe_path.strip()
        if ffmpeg_path and not os.path.isabs(ffmpeg_path):
            raise ValueError("MEDIA_MUXING_FFMPEG_PATH must be absolute when set")
        if ffprobe_path and not os.path.isabs(ffprobe_path):
            raise ValueError("MEDIA_MUXING_FFPROBE_PATH must be absolute when set")
        # MEDIA_MUXING_ENABLED must not require ffmpeg/ffprobe paths here: the
        # API container never receives worker tool paths. Worker construction
        # validates executables at runtime when muxing is on.
        return self


@lru_cache
def get_settings() -> Settings:
    """Return cached settings instance."""
    return Settings()
