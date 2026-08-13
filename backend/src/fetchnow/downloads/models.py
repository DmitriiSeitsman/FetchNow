"""SQLAlchemy model for durable media-download jobs."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from fetchnow.db.base import Base


class MediaDownloadJob(Base):
    """Persisted media-download job bound to an inspected parent MediaJob."""

    __tablename__ = "media_download_jobs"
    __table_args__ = (
        UniqueConstraint(
            "media_job_id",
            "format_option_id",
            name="uq_media_download_jobs_parent_format",
        ),
        CheckConstraint(
            "public_state IN ('queued', 'downloading', 'ready', 'failed', 'expired')",
            name="ck_media_download_jobs_public_state",
        ),
        CheckConstraint(
            "position('?' in canonical_provider_url) = 0 "
            "AND position('#' in canonical_provider_url) = 0",
            name="ck_media_download_jobs_canonical_no_query_fragment",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_media_download_jobs_attempt_count_nonneg",
        ),
        CheckConstraint(
            "max_attempts >= 1",
            name="ck_media_download_jobs_max_attempts_positive",
        ),
        CheckConstraint(
            "fence_token >= 0",
            name="ck_media_download_jobs_fence_token_nonneg",
        ),
        CheckConstraint(
            "schema_version >= 1",
            name="ck_media_download_jobs_schema_version",
        ),
        CheckConstraint(
            "scheme = 'https' AND port IS NULL",
            name="ck_media_download_jobs_terminal_transport",
        ),
        CheckConstraint(
            "(public_state = 'downloading' AND lease_owner IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR "
            "(public_state <> 'downloading' AND lease_owner IS NULL "
            "AND lease_expires_at IS NULL)",
            name="ck_media_download_jobs_lease_state",
        ),
        CheckConstraint(
            "("
            "public_state = 'ready' AND artifact_id IS NOT NULL "
            "AND artifact_bytes IS NOT NULL AND artifact_bytes > 0 "
            "AND artifact_container IS NOT NULL "
            "AND artifact_content_type IS NOT NULL "
            "AND public_error_code IS NULL AND completed_at IS NOT NULL"
            ") OR ("
            "public_state = 'failed' "
            "AND artifact_id IS NULL AND artifact_bytes IS NULL "
            "AND artifact_container IS NULL AND artifact_content_type IS NULL "
            "AND public_error_code IS NOT NULL AND completed_at IS NOT NULL "
            "AND char_length(public_error_code) BETWEEN 1 AND 64"
            ") OR ("
            "public_state IN ('queued', 'downloading', 'expired') "
            "AND artifact_id IS NULL AND artifact_bytes IS NULL "
            "AND artifact_container IS NULL AND artifact_content_type IS NULL "
            "AND public_error_code IS NULL"
            ")",
            name="ck_media_download_jobs_result_state",
        ),
        CheckConstraint(
            "artifact_bytes IS NULL OR artifact_bytes > 0",
            name="ck_media_download_jobs_artifact_bytes_positive",
        ),
        CheckConstraint(
            "progress_stage IN ("
            "'queued', 'inspecting', 'downloading_video', 'downloading_audio', "
            "'muxing', 'verifying', 'publishing', 'retrying', "
            "'ready', 'failed', 'cancelled', 'expired')",
            name="ck_media_download_jobs_progress_stage",
        ),
        CheckConstraint(
            "("
            "public_state = 'queued' AND progress_stage IN ('queued', 'retrying')"
            ") OR ("
            "public_state = 'downloading' AND progress_stage IN ("
            "'inspecting', 'downloading_video', 'downloading_audio', "
            "'muxing', 'verifying', 'publishing')"
            ") OR ("
            "public_state = 'ready' AND progress_stage = 'ready'"
            ") OR ("
            "public_state = 'failed' AND progress_stage = 'failed'"
            ") OR ("
            "public_state = 'expired' AND progress_stage IN ('expired', 'cancelled')"
            ")",
            name="ck_media_download_jobs_progress_state",
        ),
        CheckConstraint(
            "("
            "cancel_requested_at IS NULL AND progress_stage <> 'cancelled'"
            ") OR ("
            "cancel_requested_at IS NOT NULL "
            "AND ("
            "public_state = 'downloading' "
            "OR (public_state = 'expired' AND progress_stage = 'cancelled')"
            ")"
            ")",
            name="ck_media_download_jobs_cancel_requested",
        ),
        Index(
            "ix_media_download_jobs_claim",
            "available_at",
            "created_at",
            postgresql_where=text("public_state = 'queued'"),
        ),
        Index(
            "ix_media_download_jobs_lease",
            "lease_expires_at",
            postgresql_where=text("public_state = 'downloading'"),
        ),
        Index("ix_media_download_jobs_expires_at", "expires_at"),
        Index("ix_media_download_jobs_media_job_id", "media_job_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    media_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("media_jobs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    public_state: Mapped[str] = mapped_column(String(32), nullable=False)
    format_option_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_id: Mapped[str] = mapped_column(String(32), nullable=False)
    canonical_provider_url: Mapped[str] = mapped_column(Text, nullable=False)
    media_id: Mapped[str] = mapped_column(String(128), nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    scheme: Mapped[str] = mapped_column(String(16), nullable=False)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    selected_format_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    fence_token: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    artifact_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    artifact_content_type: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    artifact_container: Mapped[str | None] = mapped_column(String(16), nullable=True)
    public_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    progress_stage: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued"
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"MediaDownloadJob(id={self.id!r}, media_job_id={self.media_job_id!r}, "
            f"public_state={self.public_state!r}, "
            f"format_option_id={self.format_option_id!r}, "
            f"provider_id={self.provider_id!r})"
        )
