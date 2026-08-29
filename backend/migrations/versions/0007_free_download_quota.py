"""Add anonymous identities and Free download quota reservations.

Revision ID: 0007_free_download_quota
Revises: 0006_browser_delivery_grants
Create Date: 2026-08-29 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_free_download_quota"
down_revision: str | None = "0006_browser_delivery_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "anonymous_clients",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "octet_length(token_hash) = 32",
            name="ck_anonymous_clients_token_hash_len",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_anonymous_clients_expires_after_created",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_anonymous_clients_token_hash"),
    )
    op.create_index(
        "ix_anonymous_clients_expires_at",
        "anonymous_clients",
        ["expires_at"],
        unique=False,
    )

    op.create_table(
        "free_download_quota_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "anonymous_client_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "download_job_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "reservation_expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('reserved', 'consumed', 'released', 'expired')",
            name="ck_free_download_quota_entries_state",
        ),
        sa.CheckConstraint(
            "reservation_expires_at > reserved_at",
            name="ck_free_download_quota_entries_expiry",
        ),
        sa.CheckConstraint(
            "(state = 'reserved' AND consumed_at IS NULL AND released_at IS NULL) "
            "OR (state = 'consumed' AND consumed_at IS NOT NULL "
            "AND released_at IS NULL AND consumed_at >= reserved_at) "
            "OR (state IN ('released', 'expired') AND consumed_at IS NULL "
            "AND released_at IS NOT NULL AND released_at >= reserved_at)",
            name="ck_free_download_quota_entries_lifecycle",
        ),
        sa.ForeignKeyConstraint(
            ["anonymous_client_id"],
            ["anonymous_clients.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["download_job_id"],
            ["media_download_jobs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "download_job_id",
            name="uq_free_download_quota_entries_download_job",
        ),
    )
    op.create_index(
        "ix_free_quota_entries_client_consumed",
        "free_download_quota_entries",
        ["anonymous_client_id", "consumed_at"],
        unique=False,
        postgresql_where=sa.text("state = 'consumed'"),
    )
    op.create_index(
        "ix_free_quota_entries_client_reserved",
        "free_download_quota_entries",
        ["anonymous_client_id", "reservation_expires_at"],
        unique=False,
        postgresql_where=sa.text("state = 'reserved'"),
    )
    op.create_index(
        "ix_free_quota_entries_released_at",
        "free_download_quota_entries",
        ["released_at"],
        unique=False,
        postgresql_where=sa.text("state IN ('released', 'expired')"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_free_quota_entries_released_at",
        table_name="free_download_quota_entries",
        postgresql_where=sa.text("state IN ('released', 'expired')"),
    )
    op.drop_index(
        "ix_free_quota_entries_client_reserved",
        table_name="free_download_quota_entries",
        postgresql_where=sa.text("state = 'reserved'"),
    )
    op.drop_index(
        "ix_free_quota_entries_client_consumed",
        table_name="free_download_quota_entries",
        postgresql_where=sa.text("state = 'consumed'"),
    )
    op.drop_table("free_download_quota_entries")
    op.drop_index("ix_anonymous_clients_expires_at", table_name="anonymous_clients")
    op.drop_table("anonymous_clients")
