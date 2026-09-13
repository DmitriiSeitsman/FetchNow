"""Persist bounded server-observed Free delivery coverage.

Revision ID: 0010_successful_delivery_quota
Revises: 0009_premium_entitlements
Create Date: 2026-09-12 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_successful_delivery_quota"
down_revision: str | None = "0009_premium_entitlements"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "free_download_delivery_ranges",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quota_entry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("fence_token", sa.BigInteger(), nullable=False),
        sa.Column("artifact_bytes", sa.BigInteger(), nullable=False),
        sa.Column("request_start", sa.BigInteger(), nullable=False),
        sa.Column("request_end_exclusive", sa.BigInteger(), nullable=False),
        sa.Column("served_end_exclusive", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('active', 'closed')",
            name="ck_free_delivery_ranges_state",
        ),
        sa.CheckConstraint(
            "artifact_bytes > 0",
            name="ck_free_delivery_ranges_artifact_bytes_positive",
        ),
        sa.CheckConstraint(
            "fence_token >= 0",
            name="ck_free_delivery_ranges_fence_nonneg",
        ),
        sa.CheckConstraint(
            "request_start >= 0 AND request_start < request_end_exclusive "
            "AND request_end_exclusive <= artifact_bytes",
            name="ck_free_delivery_ranges_request",
        ),
        sa.CheckConstraint(
            "served_end_exclusive >= request_start "
            "AND served_end_exclusive <= request_end_exclusive",
            name="ck_free_delivery_ranges_served",
        ),
        sa.CheckConstraint(
            "(state = 'active' AND lease_expires_at IS NOT NULL "
            "AND lease_expires_at > started_at AND closed_at IS NULL) OR "
            "(state = 'closed' AND lease_expires_at IS NULL "
            "AND closed_at IS NOT NULL AND closed_at >= started_at)",
            name="ck_free_delivery_ranges_lifecycle",
        ),
        sa.ForeignKeyConstraint(
            ["quota_entry_id"],
            ["free_download_quota_entries.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_free_delivery_ranges_entry_state_start",
        "free_download_delivery_ranges",
        ["quota_entry_id", "state", "request_start"],
        unique=False,
    )
    op.create_index(
        "ix_free_delivery_ranges_active_lease",
        "free_download_delivery_ranges",
        ["quota_entry_id", "lease_expires_at"],
        unique=False,
        postgresql_where=sa.text("state = 'active'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_free_delivery_ranges_active_lease",
        table_name="free_download_delivery_ranges",
        postgresql_where=sa.text("state = 'active'"),
    )
    op.drop_index(
        "ix_free_delivery_ranges_entry_state_start",
        table_name="free_download_delivery_ranges",
    )
    op.drop_table("free_download_delivery_ranges")
