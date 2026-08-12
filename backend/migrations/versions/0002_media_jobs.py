"""Create media_jobs table for durable inspection orchestration.

Revision ID: 0002_media_jobs
Revises: 0001_baseline
Create Date: 2026-08-12 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002_media_jobs"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "media_jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("result_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("public_state", sa.String(length=32), nullable=False),
        sa.Column("provider_id", sa.String(length=32), nullable=False),
        sa.Column("canonical_provider_url", sa.Text(), nullable=False),
        sa.Column("media_id", sa.String(length=128), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("scheme", sa.String(length=16), nullable=False),
        sa.Column("port", sa.Integer(), nullable=True),
        sa.Column("credential_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("request_fingerprint", sa.LargeBinary(length=32), nullable=False),
        sa.Column(
            "result_metadata",
            postgresql.JSONB(astext_type=sa.Text(), none_as_null=True),
            nullable=True,
        ),
        sa.Column("public_error_code", sa.String(length=64), nullable=True),
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
            "credential_hash", name="uq_media_jobs_credential_hash"
        ),
        sa.CheckConstraint(
            "public_state IN ("
            "'queued', 'inspecting', 'inspected', 'failed', 'expired')",
            name="ck_media_jobs_public_state",
        ),
        sa.CheckConstraint(
            "position('?' in canonical_provider_url) = 0 "
            "AND position('#' in canonical_provider_url) = 0",
            name="ck_media_jobs_canonical_no_query_fragment",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_media_jobs_attempt_count_nonneg",
        ),
        sa.CheckConstraint(
            "max_attempts >= 1",
            name="ck_media_jobs_max_attempts_positive",
        ),
        sa.CheckConstraint(
            "fence_token >= 0",
            name="ck_media_jobs_fence_token_nonneg",
        ),
        sa.CheckConstraint(
            "octet_length(credential_hash) = 32",
            name="ck_media_jobs_credential_hash_len",
        ),
        sa.CheckConstraint(
            "octet_length(request_fingerprint) = 32",
            name="ck_media_jobs_request_fingerprint_len",
        ),
        sa.CheckConstraint(
            "schema_version >= 1",
            name="ck_media_jobs_schema_version",
        ),
        sa.CheckConstraint(
            "result_version >= 0",
            name="ck_media_jobs_result_version",
        ),
        sa.CheckConstraint(
            "scheme = 'https' AND port IS NULL",
            name="ck_media_jobs_terminal_transport",
        ),
        sa.CheckConstraint(
            "(public_state = 'inspecting' AND lease_owner IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR "
            "(public_state <> 'inspecting' AND lease_owner IS NULL "
            "AND lease_expires_at IS NULL)",
            name="ck_media_jobs_lease_state",
        ),
        sa.CheckConstraint(
            "(public_state = 'inspected' AND result_metadata IS NOT NULL "
            "AND public_error_code IS NULL AND completed_at IS NOT NULL) OR "
            "(public_state = 'failed' AND result_metadata IS NULL "
            "AND public_error_code IS NOT NULL AND completed_at IS NOT NULL) OR "
            "(public_state NOT IN ('inspected', 'failed') "
            "AND result_metadata IS NULL AND public_error_code IS NULL)",
            name="ck_media_jobs_result_state",
        ),
    )
    op.create_index(
        "ix_media_jobs_claim",
        "media_jobs",
        ["available_at", "created_at"],
        unique=False,
        postgresql_where=sa.text("public_state = 'queued'"),
    )
    op.create_index(
        "ix_media_jobs_lease",
        "media_jobs",
        ["lease_expires_at"],
        unique=False,
        postgresql_where=sa.text("public_state = 'inspecting'"),
    )
    op.create_index(
        "ix_media_jobs_expires_at",
        "media_jobs",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_media_jobs_expires_at", table_name="media_jobs")
    op.drop_index(
        "ix_media_jobs_lease",
        table_name="media_jobs",
        postgresql_where=sa.text("public_state = 'inspecting'"),
    )
    op.drop_index(
        "ix_media_jobs_claim",
        table_name="media_jobs",
        postgresql_where=sa.text("public_state = 'queued'"),
    )
    op.drop_table("media_jobs")
