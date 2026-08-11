"""Application configuration loaded exclusively from environment variables."""

from __future__ import annotations

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
    )

    api_concurrency_limit: int = Field(default=32, alias="API_CONCURRENCY_LIMIT")
    worker_concurrency: int = Field(default=2, alias="WORKER_CONCURRENCY")
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
        return self


@lru_cache
def get_settings() -> Settings:
    """Return cached settings instance."""
    return Settings()
