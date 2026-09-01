"""Add durable Robokassa test payment orders.

Revision ID: 0008_payment_orders
Revises: 0007_free_download_quota
Create Date: 2026-09-01 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_payment_orders"
down_revision: str | None = "0007_free_download_quota"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE SEQUENCE payment_provider_invoice_id_seq "
        "AS BIGINT MINVALUE 1 MAXVALUE 9223372036854775807 START WITH 1"
    )
    op.create_table(
        "payment_orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("public_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("anonymous_client_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column(
            "provider_invoice_id",
            sa.BigInteger(),
            server_default=sa.text("nextval('payment_provider_invoice_id_seq')"),
            nullable=False,
        ),
        sa.Column("product_code", sa.String(length=32), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("entitlement_duration_seconds", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("is_test", sa.Boolean(), nullable=False),
        sa.Column("receipt_json", sa.Text(), nullable=False),
        sa.Column(
            "creation_idempotency_hash", sa.LargeBinary(length=32), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("pending_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_callback_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_operation_key", sa.String(length=128), nullable=True),
        sa.CheckConstraint("provider = 'robokassa'", name="ck_payment_orders_provider"),
        sa.CheckConstraint("provider_invoice_id > 0", name="ck_payment_orders_inv_id"),
        sa.CheckConstraint("amount_minor > 0", name="ck_payment_orders_amount"),
        sa.CheckConstraint("currency = 'RUB'", name="ck_payment_orders_currency"),
        sa.CheckConstraint(
            "entitlement_duration_seconds = 86400",
            name="ck_payment_orders_duration",
        ),
        sa.CheckConstraint(
            "status IN ('created', 'pending', 'paid', 'expired')",
            name="ck_payment_orders_status",
        ),
        sa.CheckConstraint("is_test", name="ck_payment_orders_test_only"),
        sa.CheckConstraint(
            "octet_length(creation_idempotency_hash) = 32",
            name="ck_payment_orders_idempotency_hash_len",
        ),
        sa.CheckConstraint(
            "char_length(receipt_json) BETWEEN 2 AND 4096",
            name="ck_payment_orders_receipt_length",
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_payment_orders_expiry"),
        sa.CheckConstraint(
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
        sa.ForeignKeyConstraint(
            ["anonymous_client_id"], ["anonymous_clients.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_payment_orders_public_id"),
        sa.UniqueConstraint(
            "provider_invoice_id", name="uq_payment_orders_provider_invoice_id"
        ),
        sa.UniqueConstraint(
            "anonymous_client_id",
            "creation_idempotency_hash",
            name="uq_payment_orders_client_idempotency",
        ),
    )
    op.create_index(
        "ix_payment_orders_unpaid_expiry",
        "payment_orders",
        ["expires_at"],
        unique=False,
        postgresql_where=sa.text("status IN ('created', 'pending')"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_payment_orders_unpaid_expiry",
        table_name="payment_orders",
        postgresql_where=sa.text("status IN ('created', 'pending')"),
    )
    op.drop_table("payment_orders")
    op.execute("DROP SEQUENCE payment_provider_invoice_id_seq")
