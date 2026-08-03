"""Application configuration loaded exclusively from environment variables."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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


@lru_cache
def get_settings() -> Settings:
    """Return cached settings instance."""
    return Settings()
