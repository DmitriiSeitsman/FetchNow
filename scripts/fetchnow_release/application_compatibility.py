"""Application compatibility envelope and live/saved DB drift gates (PRD1C3B2A)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .current_state import CurrentStateError, DatabaseState, ResolvedCurrentState
from .db_heads import DbHeadsError, target_heads_from_release_source
from .deploy_root import release_dir
from .redact import redact
from .revision import validate_full_sha
from .verify_release import verify_prepared_release


class CompatibilityError(RuntimeError):
    """Application is not compatible with the current database state."""


class DatabaseDriftError(RuntimeError):
    """Live PostgreSQL heads differ from saved current.database.heads."""


@dataclass(frozen=True)
class CompatibilityDecision:
    allowed: bool
    reason: str
    via_equal_heads: bool
    via_recorded_envelope: bool


def assert_live_matches_saved_database_heads(
    *,
    live_heads: frozenset[str],
    saved_heads: frozenset[str] | tuple[str, ...],
) -> None:
    saved = frozenset(saved_heads)
    if live_heads != saved:
        raise DatabaseDriftError(
            "live database Alembic heads "
            f"{sorted(live_heads)} drift from saved current.database.heads "
            f"{sorted(saved)}"
        )


def application_compatible_with_database(
    *,
    target_revision: str,
    target_source_heads: frozenset[str],
    database: DatabaseState,
) -> CompatibilityDecision:
    """Return whether target application may run against the saved database state.

    Allowed if either:
    - target release source heads equal current.database.heads, or
    - target revision is listed in compatible_application_revisions.

    Transitive inference is forbidden.
    """
    target = validate_full_sha(target_revision)
    saved = frozenset(database.heads)
    equal_heads = target_source_heads == saved
    recorded = target in database.compatible_application_revisions
    if equal_heads:
        return CompatibilityDecision(
            allowed=True,
            reason="target release source heads equal database heads",
            via_equal_heads=True,
            via_recorded_envelope=recorded,
        )
    if recorded:
        return CompatibilityDecision(
            allowed=True,
            reason="target revision is recorded in compatibility envelope",
            via_equal_heads=False,
            via_recorded_envelope=True,
        )
    return CompatibilityDecision(
        allowed=False,
        reason=(
            "application revision is not compatible with current database state: "
            f"target={target} target_heads={sorted(target_source_heads)} "
            f"database_heads={sorted(saved)} "
            f"envelope={list(database.compatible_application_revisions)}"
        ),
        via_equal_heads=False,
        via_recorded_envelope=False,
    )


def assert_application_compatible_with_database(
    *,
    target_revision: str,
    target_source_heads: frozenset[str],
    database: DatabaseState,
) -> CompatibilityDecision:
    decision = application_compatible_with_database(
        target_revision=target_revision,
        target_source_heads=target_source_heads,
        database=database,
    )
    if not decision.allowed:
        raise CompatibilityError(redact(decision.reason))
    return decision


def verify_target_release_heads(
    *,
    deploy_root: Path,
    target_revision: str,
    repo_root: Path,
) -> frozenset[str]:
    rev = validate_full_sha(target_revision)
    release = release_dir(deploy_root, rev)
    verified = verify_prepared_release(
        release, expected_revision=rev, repo_root=repo_root
    )
    if not verified.ok:
        raise CompatibilityError(verified.messages[0])
    try:
        return target_heads_from_release_source(release)
    except DbHeadsError as exc:
        raise CompatibilityError(str(exc)) from exc


def assert_resolved_application_compatible(
    *,
    current: ResolvedCurrentState,
    target_revision: str,
    target_source_heads: frozenset[str],
    live_heads: frozenset[str],
) -> CompatibilityDecision:
    """Steady-state gate: live==saved, then compatibility predicate."""
    assert_live_matches_saved_database_heads(
        live_heads=live_heads,
        saved_heads=current.database.heads,
    )
    return assert_application_compatible_with_database(
        target_revision=target_revision,
        target_source_heads=target_source_heads,
        database=current.database,
    )


def assert_current_application_compatible(
    current: ResolvedCurrentState,
    *,
    application_source_heads: frozenset[str] | None = None,
) -> None:
    """Ensure the published application revision satisfies the envelope rules.

    When application source heads are supplied, direct equal-head compatibility
    is also accepted; otherwise only the recorded envelope is checked for the
    current application revision (split-state already-active path).
    """
    if current.application.revision not in current.database.compatible_application_revisions:
        raise CurrentStateError(
            "current application.revision is absent from compatibility envelope"
        )
    if application_source_heads is None:
        return
    decision = application_compatible_with_database(
        target_revision=current.application.revision,
        target_source_heads=application_source_heads,
        database=current.database,
    )
    if not decision.allowed:
        raise CompatibilityError(redact(decision.reason))
