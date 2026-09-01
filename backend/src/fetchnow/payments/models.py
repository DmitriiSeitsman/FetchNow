"""SQLAlchemy model for immutable payment-order snapshots."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Sequence,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from fetchnow.db.base import Base

provider_invoice_id_sequence = Sequence(
    "payment_provider_invoice_id_seq",
    start=1,
    minvalue=1,
    maxvalue=9_223_372_036_854_775_807,
)


class PaymentOrder(Base):
    __tablename__ = "payment_orders"
    __table_args__ = (
        CheckConstraint("provider = 'robokassa'", name="ck_payment_orders_provider"),
        CheckConstraint("provider_invoice_id > 0", name="ck_payment_orders_inv_id"),
        CheckConstraint("amount_minor > 0", name="ck_payment_orders_amount"),
        CheckConstraint("currency = 'RUB'", name="ck_payment_orders_currency"),
        CheckConstraint(
            "entitlement_duration_seconds = 86400",
            name="ck_payment_orders_duration",
        ),
        CheckConstraint(
            "status IN ('created', 'pending', 'paid', 'expired')",
            name="ck_payment_orders_status",
        ),
        CheckConstraint("is_test", name="ck_payment_orders_test_only"),
        CheckConstraint(
            "octet_length(creation_idempotency_hash) = 32",
            name="ck_payment_orders_idempotency_hash_len",
        ),
        CheckConstraint(
            "char_length(receipt_json) BETWEEN 2 AND 4096",
            name="ck_payment_orders_receipt_length",
        ),
        CheckConstraint("expires_at > created_at", name="ck_payment_orders_expiry"),
        CheckConstraint(
            "(status = 'created' AND pending_at IS NULL AND paid_at IS NULL "
            "AND provider_callback_at IS NULL) OR "
            "(status = 'pending' AND pending_at IS NOT NULL AND paid_at IS NULL "
            "AND provider_callback_at IS NULL) OR "
            "(status = 'paid' AND pending_at IS NOT NULL AND paid_at IS NOT NULL "
            "AND provider_callback_at IS NOT NULL AND paid_at >= pending_at "
            "AND provider_callback_at >= pending_at) OR "
            "(status = 'expired' AND paid_at IS NULL "
            "AND provider_callback_at IS NULL)",
            name="ck_payment_orders_lifecycle",
        ),
        UniqueConstraint("public_id", name="uq_payment_orders_public_id"),
        UniqueConstraint(
            "provider_invoice_id", name="uq_payment_orders_provider_invoice_id"
        ),
        UniqueConstraint(
            "anonymous_client_id",
            "creation_idempotency_hash",
            name="uq_payment_orders_client_idempotency",
        ),
        Index(
            "ix_payment_orders_unpaid_expiry",
            "expires_at",
            postgresql_where=text("status IN ('created', 'pending')"),
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
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    provider_invoice_id: Mapped[int] = mapped_column(
        BigInteger,
        provider_invoice_id_sequence,
        server_default=provider_invoice_id_sequence.next_value(),
        nullable=False,
    )
    product_code: Mapped[str] = mapped_column(String(32), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    entitlement_duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    is_test: Mapped[bool] = mapped_column(Boolean, nullable=False)
    receipt_json: Mapped[str] = mapped_column(Text, nullable=False)
    creation_idempotency_hash: Mapped[bytes] = mapped_column(
        LargeBinary(32), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    pending_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provider_callback_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    provider_operation_key: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
