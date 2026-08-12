"""SQLAlchemy model for durable media-inspection jobs."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from fetchnow.db.base import Base


class MediaJob(Base):
    """Persisted media-inspection job (trusted terminal target only)."""

    __tablename__ = "media_jobs"
    __table_args__ = (
        UniqueConstraint("credential_hash", name="uq_media_jobs_credential_hash"),
        CheckConstraint(
            "public_state IN ("
            "'queued', 'inspecting', 'inspected', 'failed', 'expired')",
            name="ck_media_jobs_public_state",
        ),
        CheckConstraint(
            "position('?' in canonical_provider_url) = 0 "
            "AND position('#' in canonical_provider_url) = 0",
            name="ck_media_jobs_canonical_no_query_fragment",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_media_jobs_attempt_count_nonneg",
        ),
        CheckConstraint(
            "max_attempts >= 1",
            name="ck_media_jobs_max_attempts_positive",
        ),
        CheckConstraint(
            "fence_token >= 0",
            name="ck_media_jobs_fence_token_nonneg",
        ),
        CheckConstraint(
            "octet_length(credential_hash) = 32",
            name="ck_media_jobs_credential_hash_len",
        ),
        CheckConstraint(
            "octet_length(request_fingerprint) = 32",
            name="ck_media_jobs_request_fingerprint_len",
        ),
        CheckConstraint(
            "schema_version >= 1",
            name="ck_media_jobs_schema_version",
        ),
        CheckConstraint(
            "result_version >= 0",
            name="ck_media_jobs_result_version",
        ),
        CheckConstraint(
            "scheme = 'https' AND port IS NULL",
            name="ck_media_jobs_terminal_transport",
        ),
        CheckConstraint(
            "(public_state = 'inspecting' AND lease_owner IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR "
            "(public_state <> 'inspecting' AND lease_owner IS NULL "
            "AND lease_expires_at IS NULL)",
            name="ck_media_jobs_lease_state",
        ),
        CheckConstraint(
            "(public_state = 'inspected' AND result_metadata IS NOT NULL "
            "AND public_error_code IS NULL AND completed_at IS NOT NULL) OR "
            "(public_state = 'failed' AND result_metadata IS NULL "
            "AND public_error_code IS NOT NULL AND completed_at IS NOT NULL) OR "
            "(public_state NOT IN ('inspected', 'failed') "
            "AND result_metadata IS NULL AND public_error_code IS NULL)",
            name="ck_media_jobs_result_state",
        ),
        Index(
            "ix_media_jobs_claim",
            "available_at",
            "created_at",
            postgresql_where=text("public_state = 'queued'"),
        ),
        Index(
            "ix_media_jobs_lease",
            "lease_expires_at",
            postgresql_where=text("public_state = 'inspecting'"),
        ),
        Index("ix_media_jobs_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    result_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    public_state: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_id: Mapped[str] = mapped_column(String(32), nullable=False)
    canonical_provider_url: Mapped[str] = mapped_column(Text, nullable=False)
    media_id: Mapped[str] = mapped_column(String(128), nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    scheme: Mapped[str] = mapped_column(String(16), nullable=False)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    credential_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    request_fingerprint: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    result_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    public_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
            f"MediaJob(id={self.id!r}, public_state={self.public_state!r}, "
            f"provider_id={self.provider_id!r}, media_id={self.media_id!r})"
        )
