"""Download job cancellation, progress stages, and cancel request fencing.

Revision ID: 0004_download_observability
Revises: 0003_download_jobs
Create Date: 2026-08-13 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_download_observability"
down_revision: str | None = "0003_download_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROGRESS_DEFAULT = "queued"


def upgrade() -> None:
    op.add_column(
        "media_download_jobs",
        sa.Column(
            "progress_stage",
            sa.String(length=32),
            nullable=False,
            server_default=_PROGRESS_DEFAULT,
        ),
    )
    op.add_column(
        "media_download_jobs",
        sa.Column(
            "cancel_requested_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.execute(
        sa.text(
            "UPDATE media_download_jobs SET progress_stage = CASE public_state "
            "WHEN 'queued' THEN 'queued' "
            "WHEN 'downloading' THEN 'inspecting' "
            "WHEN 'ready' THEN 'ready' "
            "WHEN 'failed' THEN 'failed' "
            "WHEN 'expired' THEN 'expired' "
            "ELSE 'queued' END"
        )
    )
    # public_state and result_state CHECKs stay on the PR9 allowlist. User
    # cancellation is stored as expired + progress_stage=cancelled +
    # cancel_requested_at so a rolled-back PR9 application can still parse
    # the raw public_state column.
    op.create_check_constraint(
        "ck_media_download_jobs_progress_stage",
        "media_download_jobs",
        "progress_stage IN ("
        "'queued', 'inspecting', 'downloading_video', 'downloading_audio', "
        "'muxing', 'verifying', 'publishing', 'retrying', "
        "'ready', 'failed', 'cancelled', 'expired')",
    )
    op.create_check_constraint(
        "ck_media_download_jobs_progress_state",
        "media_download_jobs",
        "("
        "public_state = 'queued' AND progress_stage IN ('queued', 'retrying')"
        ") OR ("
        "public_state = 'downloading' AND progress_stage IN ("
        "'inspecting', 'downloading_video', 'downloading_audio', "
        "'muxing', 'verifying', 'publishing')"
        ") OR ("
        "public_state = 'ready' AND progress_stage = 'ready'"
        ") OR ("
        "public_state = 'failed' AND progress_stage = 'failed'"
        ") OR ("
        "public_state = 'expired' AND progress_stage IN ('expired', 'cancelled')"
        ")",
    )
    op.create_check_constraint(
        "ck_media_download_jobs_cancel_requested",
        "media_download_jobs",
        "("
        "cancel_requested_at IS NULL AND progress_stage <> 'cancelled'"
        ") OR ("
        "cancel_requested_at IS NOT NULL "
        "AND ("
        "public_state = 'downloading' "
        "OR (public_state = 'expired' AND progress_stage = 'cancelled')"
        ")"
        ")",
    )
    # PR9 workers UPDATE public_state without writing progress_stage.
    # Server default covers INSERT only. This BEFORE trigger maps an illegal
    # pair only when progress_stage was omitted (INSERT default 'queued') or
    # left unchanged (legacy UPDATE). Explicit illegal PR10 writes still hit
    # ck_media_download_jobs_progress_state.
    op.execute(
        sa.text(
            """
            CREATE FUNCTION fetchnow_media_download_jobs_progress_compat()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $fn$
            DECLARE
              legal boolean;
              mapped text;
              coerced boolean := false;
            BEGIN
              legal := (
                (NEW.public_state = 'queued'
                 AND NEW.progress_stage IN ('queued', 'retrying'))
                OR (NEW.public_state = 'downloading'
                    AND NEW.progress_stage IN (
                      'inspecting', 'downloading_video', 'downloading_audio',
                      'muxing', 'verifying', 'publishing'
                    ))
                OR (NEW.public_state = 'ready'
                    AND NEW.progress_stage = 'ready')
                OR (NEW.public_state = 'failed'
                    AND NEW.progress_stage = 'failed')
                OR (NEW.public_state = 'expired'
                    AND NEW.progress_stage IN ('expired', 'cancelled'))
              );
              IF legal THEN
                RETURN NEW;
              END IF;
              mapped := CASE NEW.public_state
                WHEN 'queued' THEN 'queued'
                WHEN 'downloading' THEN 'inspecting'
                WHEN 'ready' THEN 'ready'
                WHEN 'failed' THEN 'failed'
                WHEN 'expired' THEN 'expired'
                ELSE NEW.progress_stage
              END;
              IF TG_OP = 'INSERT' AND NEW.progress_stage = 'queued' THEN
                NEW.progress_stage := mapped;
                coerced := true;
              ELSIF TG_OP = 'UPDATE'
                    AND NEW.progress_stage IS NOT DISTINCT FROM OLD.progress_stage THEN
                NEW.progress_stage := mapped;
                coerced := true;
              END IF;
              -- Legacy PR9 writes omit progress_stage. Clearing the cancel
              -- marker only on coerce keeps reclaim/expiry CHECK-compatible
              -- without rewriting a legal PR10 cancellation encoding.
              IF coerced AND NOT (
                NEW.public_state = 'downloading'
                OR (NEW.public_state = 'expired'
                    AND NEW.progress_stage = 'cancelled')
              ) THEN
                NEW.cancel_requested_at := NULL;
              END IF;
              RETURN NEW;
            END;
            $fn$;
            """
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_media_download_jobs_progress_compat "
            "BEFORE INSERT OR UPDATE ON media_download_jobs "
            "FOR EACH ROW EXECUTE FUNCTION "
            "fetchnow_media_download_jobs_progress_compat()"
        )
    )


def downgrade() -> None:
    # Cancellation is stored as PR9 ``expired``; dropping the PR10 columns
    # needs no rewrite of a new public_state enum value.
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_media_download_jobs_progress_compat "
            "ON media_download_jobs"
        )
    )
    op.execute(
        sa.text(
            "DROP FUNCTION IF EXISTS fetchnow_media_download_jobs_progress_compat()"
        )
    )
    op.drop_constraint(
        "ck_media_download_jobs_cancel_requested",
        "media_download_jobs",
        type_="check",
    )
    op.drop_constraint(
        "ck_media_download_jobs_progress_state",
        "media_download_jobs",
        type_="check",
    )
    op.drop_constraint(
        "ck_media_download_jobs_progress_stage",
        "media_download_jobs",
        type_="check",
    )
    op.drop_column("media_download_jobs", "cancel_requested_at")
    op.drop_column("media_download_jobs", "progress_stage")
