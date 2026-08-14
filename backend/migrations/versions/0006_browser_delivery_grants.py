"""PR14 browser-native delivery grants.

Revision ID: 0006_browser_delivery_grants
Revises: 0005_download_file_details
Create Date: 2026-08-14 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_browser_delivery_grants"
down_revision: str | None = "0005_download_file_details"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "media_delivery_grants",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("download_job_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("artifact_id", sa.UUID(), nullable=False),
        sa.Column("fence_token", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_media_delivery_grants_expires_after_created",
        ),
        sa.CheckConstraint(
            "octet_length(token_hash) = 32",
            name="ck_media_delivery_grants_token_hash_len",
        ),
        sa.CheckConstraint(
            "fence_token >= 0",
            name="ck_media_delivery_grants_fence_nonneg",
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="ck_media_delivery_grants_revoked_after_created",
        ),
        sa.ForeignKeyConstraint(
            ["download_job_id"],
            ["media_download_jobs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_media_delivery_grants_token_hash"),
    )
    op.create_index(
        "ix_media_delivery_grants_download_job_id",
        "media_delivery_grants",
        ["download_job_id"],
        unique=False,
    )
    op.create_index(
        "ix_media_delivery_grants_expires_at",
        "media_delivery_grants",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_media_delivery_grants_expires_at",
        table_name="media_delivery_grants",
    )
    op.drop_index(
        "ix_media_delivery_grants_download_job_id",
        table_name="media_delivery_grants",
    )
    op.drop_table("media_delivery_grants")
