"""PR12 download file details: suggested filename and byte progress.

Revision ID: 0005_download_file_details
Revises: 0004_download_observability
Create Date: 2026-08-14 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_download_file_details"
down_revision: str | None = "0004_download_observability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "media_download_jobs",
        sa.Column("suggested_filename", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "media_download_jobs",
        sa.Column("progress_percent", sa.SmallInteger(), nullable=True),
    )
    op.create_check_constraint(
        "ck_media_download_jobs_suggested_filename",
        "media_download_jobs",
        "("
        "suggested_filename IS NULL OR ("
        "char_length(suggested_filename) BETWEEN 1 AND 255 "
        "AND position('/' in suggested_filename) = 0 "
        "AND position(E'\\\\' in suggested_filename) = 0 "
        "AND position(CHR(10) in suggested_filename) = 0 "
        "AND position(CHR(13) in suggested_filename) = 0"
        "))",
    )
    op.create_check_constraint(
        "ck_media_download_jobs_progress_percent",
        "media_download_jobs",
        "("
        "progress_percent IS NULL"
        ") OR ("
        "progress_percent BETWEEN 0 AND 99 "
        "AND public_state = 'downloading' "
        "AND progress_stage IN ('downloading_video', 'downloading_audio')"
        ")",
    )
    # PR11 workers UPDATE progress_stage / public_state without writing
    # progress_percent. A leftover non-null percent from a PR12 writer would
    # fail ck_media_download_jobs_progress_percent on those stage changes.
    # PostgreSQL row triggers cannot tell an omitted column from an explicit
    # assignment of the same value: both leave NEW IS NOT DISTINCT FROM OLD.
    # This trigger therefore normalizes progress_percent to NULL whenever
    # progress_stage changes and the percent value did not change. That keeps
    # previous PR11 applications CHECK-compatible. An explicit write of a
    # *different* illegal percent still reaches the CHECK. An explicit write
    # of the *same* old percent on a disallowed stage is also cleared to NULL
    # (safe empty percent, not a persisted illegal pair).
    op.execute(
        sa.text(
            """
            CREATE FUNCTION fetchnow_media_download_jobs_progress_percent_compat()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $fn$
            BEGIN
              IF TG_OP = 'UPDATE'
                 AND NEW.progress_stage IS DISTINCT FROM OLD.progress_stage
                 AND NEW.progress_percent IS NOT DISTINCT FROM OLD.progress_percent
              THEN
                NEW.progress_percent := NULL;
              END IF;
              RETURN NEW;
            END;
            $fn$;
            """
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_media_download_jobs_progress_percent_compat "
            "BEFORE UPDATE ON media_download_jobs "
            "FOR EACH ROW EXECUTE FUNCTION "
            "fetchnow_media_download_jobs_progress_percent_compat()"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_media_download_jobs_progress_percent_compat "
            "ON media_download_jobs"
        )
    )
    op.execute(
        sa.text(
            "DROP FUNCTION IF EXISTS "
            "fetchnow_media_download_jobs_progress_percent_compat()"
        )
    )
    op.drop_constraint(
        "ck_media_download_jobs_progress_percent",
        "media_download_jobs",
        type_="check",
    )
    op.drop_constraint(
        "ck_media_download_jobs_suggested_filename",
        "media_download_jobs",
        type_="check",
    )
    op.drop_column("media_download_jobs", "progress_percent")
    op.drop_column("media_download_jobs", "suggested_filename")
