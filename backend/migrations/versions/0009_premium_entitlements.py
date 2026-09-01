"""Add Premium entitlements tied to paid payment orders.

Revision ID: 0009_premium_entitlements
Revises: 0008_payment_orders
Create Date: 2026-09-02 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_premium_entitlements"
down_revision: str | None = "0008_payment_orders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "premium_entitlements",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("public_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("anonymous_client_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "source_payment_order_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("product_code", sa.String(length=32), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("expires_at > starts_at", name="ck_premium_entitlements_window"),
        sa.ForeignKeyConstraint(
            ["anonymous_client_id"], ["anonymous_clients.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_payment_order_id"], ["payment_orders.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_premium_entitlements_public_id"),
        sa.UniqueConstraint(
            "source_payment_order_id",
            name="uq_premium_entitlements_source_payment_order_id",
        ),
    )
    op.create_index(
        "ix_premium_entitlements_client_expires",
        "premium_entitlements",
        ["anonymous_client_id", "expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_premium_entitlements_client_expires", table_name="premium_entitlements"
    )
    op.drop_table("premium_entitlements")
