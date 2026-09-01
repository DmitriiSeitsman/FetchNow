"""SQLAlchemy model for Premium entitlements granted from paid orders."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from fetchnow.db.base import Base


class PremiumEntitlement(Base):
    __tablename__ = "premium_entitlements"
    __table_args__ = (
        CheckConstraint(
            "expires_at > starts_at",
            name="ck_premium_entitlements_window",
        ),
        UniqueConstraint("public_id", name="uq_premium_entitlements_public_id"),
        UniqueConstraint(
            "source_payment_order_id",
            name="uq_premium_entitlements_source_payment_order_id",
        ),
        Index(
            "ix_premium_entitlements_client_expires",
            "anonymous_client_id",
            "expires_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    public_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    anonymous_client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("anonymous_clients.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_payment_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payment_orders.id", ondelete="RESTRICT"),
        nullable=False,
    )
    product_code: Mapped[str] = mapped_column(String(32), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
