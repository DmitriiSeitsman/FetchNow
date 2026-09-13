"""SQLAlchemy models for anonymous identities and Free quota entries."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from fetchnow.db.base import Base


class AnonymousClient(Base):
    """Server-controlled opaque browser identity; raw token is never stored."""

    __tablename__ = "anonymous_clients"
    __table_args__ = (
        CheckConstraint(
            "octet_length(token_hash) = 32",
            name="ck_anonymous_clients_token_hash_len",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="ck_anonymous_clients_expires_after_created",
        ),
        UniqueConstraint("token_hash", name="uq_anonymous_clients_token_hash"),
        Index("ix_anonymous_clients_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class FreeDownloadQuotaEntry(Base):
    """One repairable reservation/usage event for a durable download job."""

    __tablename__ = "free_download_quota_entries"
    __table_args__ = (
        CheckConstraint(
            "state IN ('reserved', 'consumed', 'released', 'expired')",
            name="ck_free_download_quota_entries_state",
        ),
        CheckConstraint(
            "reservation_expires_at > reserved_at",
            name="ck_free_download_quota_entries_expiry",
        ),
        CheckConstraint(
            "(state = 'reserved' AND consumed_at IS NULL AND released_at IS NULL) "
            "OR (state = 'consumed' AND consumed_at IS NOT NULL "
            "AND released_at IS NULL AND consumed_at >= reserved_at) "
            "OR (state IN ('released', 'expired') AND consumed_at IS NULL "
            "AND released_at IS NOT NULL AND released_at >= reserved_at)",
            name="ck_free_download_quota_entries_lifecycle",
        ),
        UniqueConstraint(
            "download_job_id",
            name="uq_free_download_quota_entries_download_job",
        ),
        Index(
            "ix_free_quota_entries_client_consumed",
            "anonymous_client_id",
            "consumed_at",
            postgresql_where=text("state = 'consumed'"),
        ),
        Index(
            "ix_free_quota_entries_client_reserved",
            "anonymous_client_id",
            "reservation_expires_at",
            postgresql_where=text("state = 'reserved'"),
        ),
        Index(
            "ix_free_quota_entries_released_at",
            "released_at",
            postgresql_where=text("state IN ('released', 'expired')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    anonymous_client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("anonymous_clients.id", ondelete="RESTRICT"),
        nullable=False,
    )
    download_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("media_download_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    reserved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    reservation_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class FreeDownloadDeliveryRange(Base):
    """Bounded server-observed delivery prefix for one Free response."""

    __tablename__ = "free_download_delivery_ranges"
    __table_args__ = (
        CheckConstraint(
            "state IN ('active', 'closed')",
            name="ck_free_delivery_ranges_state",
        ),
        CheckConstraint(
            "artifact_bytes > 0",
            name="ck_free_delivery_ranges_artifact_bytes_positive",
        ),
        CheckConstraint(
            "fence_token >= 0",
            name="ck_free_delivery_ranges_fence_nonneg",
        ),
        CheckConstraint(
            "request_start >= 0 AND request_start < request_end_exclusive "
            "AND request_end_exclusive <= artifact_bytes",
            name="ck_free_delivery_ranges_request",
        ),
        CheckConstraint(
            "served_end_exclusive >= request_start "
            "AND served_end_exclusive <= request_end_exclusive",
            name="ck_free_delivery_ranges_served",
        ),
        CheckConstraint(
            "(state = 'active' AND lease_expires_at IS NOT NULL "
            "AND lease_expires_at > started_at AND closed_at IS NULL) OR "
            "(state = 'closed' AND lease_expires_at IS NULL "
            "AND closed_at IS NOT NULL AND closed_at >= started_at)",
            name="ck_free_delivery_ranges_lifecycle",
        ),
        Index(
            "ix_free_delivery_ranges_entry_state_start",
            "quota_entry_id",
            "state",
            "request_start",
        ),
        Index(
            "ix_free_delivery_ranges_active_lease",
            "quota_entry_id",
            "lease_expires_at",
            postgresql_where=text("state = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    quota_entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("free_download_quota_entries.id", ondelete="CASCADE"),
        nullable=False,
    )
    artifact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    fence_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    artifact_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    request_start: Mapped[int] = mapped_column(BigInteger, nullable=False)
    request_end_exclusive: Mapped[int] = mapped_column(BigInteger, nullable=False)
    served_end_exclusive: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
