"""SQLAlchemy model for short-lived browser delivery grants."""

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
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from fetchnow.db.base import Base


class MediaDeliveryGrant(Base):
    """Persisted browser-native delivery grant (hash only; no raw token)."""

    __tablename__ = "media_delivery_grants"
    __table_args__ = (
        CheckConstraint(
            "expires_at > created_at",
            name="ck_media_delivery_grants_expires_after_created",
        ),
        CheckConstraint(
            "octet_length(token_hash) = 32",
            name="ck_media_delivery_grants_token_hash_len",
        ),
        CheckConstraint(
            "fence_token >= 0",
            name="ck_media_delivery_grants_fence_nonneg",
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="ck_media_delivery_grants_revoked_after_created",
        ),
        Index("ix_media_delivery_grants_download_job_id", "download_job_id"),
        Index("ix_media_delivery_grants_expires_at", "expires_at"),
        UniqueConstraint("token_hash", name="uq_media_delivery_grants_token_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    download_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("media_download_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    fence_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    def __repr__(self) -> str:
        return (
            f"MediaDeliveryGrant(id={self.id!r}, "
            f"download_job_id={self.download_job_id!r})"
        )
