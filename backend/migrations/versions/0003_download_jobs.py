"""Create media_download_jobs for durable download execution (PR6).

Revision ID: 0003_download_jobs
Revises: 0002_media_jobs
Create Date: 2026-08-12 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_download_jobs"
down_revision: str | None = "0002_media_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "media_download_jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "media_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("media_jobs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("public_state", sa.String(length=32), nullable=False),
        sa.Column("format_option_id", sa.String(length=64), nullable=False),
        sa.Column("provider_id", sa.String(length=32), nullable=False),
        sa.Column("canonical_provider_url", sa.Text(), nullable=False),
        sa.Column("media_id", sa.String(length=128), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("scheme", sa.String(length=16), nullable=False),
        sa.Column("port", sa.Integer(), nullable=True),
        sa.Column(
            "selected_format_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "fence_token",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "artifact_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("artifact_bytes", sa.BigInteger(), nullable=True),
        sa.Column("artifact_content_type", sa.String(length=128), nullable=True),
        sa.Column("artifact_container", sa.String(length=16), nullable=True),
        sa.Column("public_error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "media_job_id",
            "format_option_id",
            name="uq_media_download_jobs_parent_format",
        ),
        sa.CheckConstraint(
            "public_state IN ("
            "'queued', 'downloading', 'ready', 'failed', 'expired')",
            name="ck_media_download_jobs_public_state",
        ),
        sa.CheckConstraint(
            "position('?' in canonical_provider_url) = 0 "
            "AND position('#' in canonical_provider_url) = 0",
            name="ck_media_download_jobs_canonical_no_query_fragment",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_media_download_jobs_attempt_count_nonneg",
        ),
        sa.CheckConstraint(
            "max_attempts >= 1",
            name="ck_media_download_jobs_max_attempts_positive",
        ),
        sa.CheckConstraint(
            "fence_token >= 0",
            name="ck_media_download_jobs_fence_token_nonneg",
        ),
        sa.CheckConstraint(
            "schema_version >= 1",
            name="ck_media_download_jobs_schema_version",
        ),
        sa.CheckConstraint(
            "scheme = 'https' AND port IS NULL",
            name="ck_media_download_jobs_terminal_transport",
        ),
        sa.CheckConstraint(
            "(public_state = 'downloading' AND lease_owner IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR "
            "(public_state <> 'downloading' AND lease_owner IS NULL "
            "AND lease_expires_at IS NULL)",
            name="ck_media_download_jobs_lease_state",
        ),
        sa.CheckConstraint(
            "("
            "public_state = 'ready' AND artifact_id IS NOT NULL "
            "AND artifact_bytes IS NOT NULL AND artifact_bytes > 0 "
            "AND artifact_container IS NOT NULL "
            "AND artifact_content_type IS NOT NULL "
            "AND public_error_code IS NULL AND completed_at IS NOT NULL"
            ") OR ("
            "public_state = 'failed' "
            "AND artifact_id IS NULL AND artifact_bytes IS NULL "
            "AND artifact_container IS NULL AND artifact_content_type IS NULL "
            "AND public_error_code IS NOT NULL AND completed_at IS NOT NULL "
            "AND char_length(public_error_code) BETWEEN 1 AND 64"
            ") OR ("
            "public_state IN ('queued', 'downloading', 'expired') "
            "AND artifact_id IS NULL AND artifact_bytes IS NULL "
            "AND artifact_container IS NULL AND artifact_content_type IS NULL "
            "AND public_error_code IS NULL"
            ")",
            name="ck_media_download_jobs_result_state",
        ),
        sa.CheckConstraint(
            "artifact_bytes IS NULL OR artifact_bytes > 0",
            name="ck_media_download_jobs_artifact_bytes_positive",
        ),
    )
    op.create_index(
        "ix_media_download_jobs_claim",
        "media_download_jobs",
        ["available_at", "created_at"],
        unique=False,
        postgresql_where=sa.text("public_state = 'queued'"),
    )
    op.create_index(
        "ix_media_download_jobs_lease",
        "media_download_jobs",
        ["lease_expires_at"],
        unique=False,
        postgresql_where=sa.text("public_state = 'downloading'"),
    )
    op.create_index(
        "ix_media_download_jobs_expires_at",
        "media_download_jobs",
        ["expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_media_download_jobs_media_job_id",
        "media_download_jobs",
        ["media_job_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_media_download_jobs_media_job_id", table_name="media_download_jobs"
    )
    op.drop_index(
        "ix_media_download_jobs_expires_at", table_name="media_download_jobs"
    )
    op.drop_index(
        "ix_media_download_jobs_lease",
        table_name="media_download_jobs",
        postgresql_where=sa.text("public_state = 'downloading'"),
    )
    op.drop_index(
        "ix_media_download_jobs_claim",
        table_name="media_download_jobs",
        postgresql_where=sa.text("public_state = 'queued'"),
    )
    op.drop_table("media_download_jobs")
