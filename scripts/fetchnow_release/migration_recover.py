"""Migration transaction recovery (PRD1C3B2B2)."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from .application_compatibility import (
    CompatibilityError,
    assert_application_compatible_with_database,
)
from .c2_constants import SOURCE_DIRNAME
from .c3b2b2_constants import (
    MIGRATION_RECOVERY_OUTCOME_ACCEPT_SOURCE,
    MIGRATION_RECOVERY_OUTCOME_ACCEPT_TARGET,
    MIGRATION_RECOVERY_OUTCOME_RELEASE_HOLD,
    MIGRATION_RECOVERY_PLAN_SCHEMA_VERSION,
    STATUS_MIGRATION_COMMITTED,
    STATUS_MIGRATION_FAILED,
    STATUS_MIGRATION_REQUIRES_RECOVERY,
    STATUS_MIGRATION_STATE_COMMITTED,
)
from .current_state import (
    CurrentStateError,
    assert_schema_release_graph_sha256,
    build_committed_current_state,
    current_path,
    load_and_resolve_current_state,
    next_database_state_after_migration,
    write_current_state,
)
from .db_heads import DbHeadsError, database_heads_via_postgres
from .deploy_root import release_dir, validate_deploy_root
from .env_file import load_env_file
from .environment import assert_real_environment_bundle, real_identity
from .health import HealthError, HealthInput, run_health
from .journal import find_unresolved_deployments, utc_now
from .migration_journal import (
    MigrationJournalError,
    MigrationRecoveryPlanDocument,
    MigrationRecoveryResultDocument,
    MigrationResultDocument,
    append_migration_recovery_event,
    find_unresolved_migrations,
    latest_migration_status,
    load_migration_plan,
    load_migration_resolution,
    load_migration_result,
    migration_dir,
    migration_recovery_dir,
    new_recovery_id,
    write_migration_recovery_plan,
    write_migration_recovery_result,
    write_migration_resolution,
    write_migration_result,
)
from .redact import redact
from .rollout_lock import RolloutLock, RolloutLockError
from .verify_release import ReleaseVerifyError, verify_prepared_release

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from fetchnow_pg_backup.checksums import sha256_bytes  # noqa: E402
from fetchnow_pg_backup.retention_hold import (  # noqa: E402
    RetentionHoldError,
    active_hold_for_migration,
    hold_is_active,
)
from fetchnow_pg_backup.session import BackupSessionError, open_backup_root_session  # noqa: E402


class MigrationRecoverError(RuntimeError):
    """Migration recovery failure."""


@dataclass(frozen=True)
class MigrationRecoverInput:
    project_name: str
    env_file: Path
    compose_files: tuple[Path, ...]
    repo_root: Path
    deploy_root: Path
    backup_root: Path | None
    migration_id: str
    action: str
    gateway_base_url: str = "http://127.0.0.1:8091"
    wait_lock: bool = False


@dataclass(frozen=True)
class MigrationRecoverResult:
    ok: bool
    messages: tuple[str, ...]
    outcome: str | None = None


def _validate_active_hold(
    *,
    backup_root: Path,
    migration_id: str,
    plan,
) -> str:
    acquired = active_hold_for_migration(backup_root, migration_id)
    if acquired is None:
        raise MigrationRecoverError(
            "active retention hold required for migration recovery"
        )
    if not hold_is_active(backup_root, acquired.hold_id):
        raise MigrationRecoverError("retention hold is not active")
    if tuple(plan.source_database_heads) != acquired.expected_alembic_heads:
        raise MigrationRecoverError("hold expected heads mismatch plan source heads")
    if acquired.migrations_graph_sha256 != plan.source_migrations_graph_sha256:
        raise MigrationRecoverError("hold graph SHA mismatch plan source graph")
    return acquired.hold_id


def recover_migration(inp: MigrationRecoverInput) -> MigrationRecoverResult:
    messages: list[str] = []
    if inp.action not in {
        MIGRATION_RECOVERY_OUTCOME_ACCEPT_SOURCE,
        MIGRATION_RECOVERY_OUTCOME_ACCEPT_TARGET,
        MIGRATION_RECOVERY_OUTCOME_RELEASE_HOLD,
    }:
        return MigrationRecoverResult(
            ok=False,
            messages=(f"FAIL: unsupported recovery action {inp.action!r}",),
        )
    try:
        deploy = validate_deploy_root(inp.deploy_root, repo_root=inp.repo_root)
        mig_dir = migration_dir(deploy, inp.migration_id)
        if not mig_dir.is_dir():
            raise MigrationRecoverError(f"migration journal not found: {inp.migration_id}")

        existing_resolution = load_migration_resolution(mig_dir)
        if existing_resolution is not None:
            messages.append(
                f"OK: migration already resolved action={existing_resolution['action']!r}"
            )
            return MigrationRecoverResult(
                ok=True,
                messages=tuple(messages),
                outcome=str(existing_resolution["action"]),
            )

        result = load_migration_result(mig_dir)
        plan = load_migration_plan(mig_dir)
        if plan.migration_id != inp.migration_id:
            raise MigrationRecoverError("plan migration_id mismatch")
        if plan.project_name != inp.project_name:
            raise MigrationRecoverError("plan project_name mismatch")

        if inp.action == MIGRATION_RECOVERY_OUTCOME_RELEASE_HOLD:
            if result is None or result.status != STATUS_MIGRATION_COMMITTED:
                raise MigrationRecoverError(
                    "release_hold requires a committed migration result"
                )
        elif result is None:
            latest = latest_migration_status(mig_dir)
            if latest != STATUS_MIGRATION_STATE_COMMITTED:
                raise MigrationRecoverError(
                    "migration journal is not in migration_requires_recovery state"
                )
        elif result.status != STATUS_MIGRATION_REQUIRES_RECOVERY:
            raise MigrationRecoverError(
                "migration journal is not in migration_requires_recovery state"
            )

        env = load_env_file(inp.env_file)
        assert_real_environment_bundle(
            project_name=inp.project_name,
            env=env,
            env_file=inp.env_file,
            compose_files=inp.compose_files,
            deploy_root=inp.deploy_root,
            cli_backup_root=inp.backup_root,
            require_cli_backup_root=real_identity(inp.project_name) is not None,
            require_deploy_root=real_identity(inp.project_name) is not None,
        )
        backup_root = Path(
            inp.backup_root or env["FETCHNOW_BACKUP_ROOT"]
        ).expanduser().resolve()

        with RolloutLock(deploy, wait=inp.wait_lock):
            if inp.action != MIGRATION_RECOVERY_OUTCOME_RELEASE_HOLD:
                if inp.migration_id not in find_unresolved_migrations(deploy):
                    raise MigrationRecoverError(
                        "migration journal is not unresolved under lock"
                    )
            unresolved_deployments = find_unresolved_deployments(deploy)
            if unresolved_deployments:
                raise MigrationRecoverError(
                    "unresolved deployment journal(s) block migration recovery: "
                    + ", ".join(unresolved_deployments)
                )

            current = load_and_resolve_current_state(deploy, repo_root=inp.repo_root)
            if current is None:
                raise MigrationRecoverError("current.json missing")

            current_release = release_dir(deploy, current.application.revision)
            cwd = current_release / SOURCE_DIRNAME
            live_heads = frozenset(
                database_heads_via_postgres(
                    project_name=inp.project_name,
                    env_file=inp.env_file,
                    compose_files=inp.compose_files,
                    cwd=cwd,
                )
            )
            source_heads = frozenset(plan.source_database_heads)
            target_heads = frozenset(plan.target_database_heads)
            current_sha = sha256_bytes(current_path(deploy).read_bytes())

            if inp.action == MIGRATION_RECOVERY_OUTCOME_RELEASE_HOLD:
                hold_id = _validate_active_hold(
                    backup_root=backup_root,
                    migration_id=inp.migration_id,
                    plan=plan,
                )
                with open_backup_root_session(
                    backup_root=backup_root,
                    repo_root=inp.repo_root,
                    wait_lock=False,
                ) as session:
                    session.release_retention_hold(
                        hold_id=hold_id,
                        reason="migration recovery release_hold",
                    )
                messages.append(f"OK: retention hold released ({hold_id})")
                messages.append("OK: committed migration hold release completed")
                return MigrationRecoverResult(
                    ok=True,
                    messages=tuple(messages),
                    outcome=inp.action,
                )

            if live_heads != source_heads and live_heads != target_heads:
                raise MigrationRecoverError(
                    "live database heads match neither planned source nor target"
                )

            hold_id = _validate_active_hold(
                backup_root=backup_root,
                migration_id=inp.migration_id,
                plan=plan,
            )

            health = run_health(
                HealthInput(
                    project_name=inp.project_name,
                    env_file=inp.env_file,
                    compose_files=inp.compose_files,
                    expected_revision=current.application.revision,
                    repo_root=cwd,
                    gateway_base_url=inp.gateway_base_url,
                    expected_image_ids=dict(current.application.image_ids),
                )
            )
            if not health.ok:
                raise MigrationRecoverError(health.messages[0])

            if inp.action == MIGRATION_RECOVERY_OUTCOME_ACCEPT_SOURCE:
                if live_heads != source_heads:
                    raise MigrationRecoverError(
                        "accept-source requires live heads to equal planned source heads"
                    )
                if current_sha != plan.current_state_sha256:
                    raise MigrationRecoverError(
                        "current.json no longer matches planned source state"
                    )
                terminal_outcome = STATUS_MIGRATION_FAILED
            else:
                if live_heads != target_heads:
                    raise MigrationRecoverError(
                        "accept-target requires live heads to equal planned target heads"
                    )
                target_release = release_dir(
                    deploy, plan.target_database_release_revision
                )
                verified = verify_prepared_release(
                    target_release,
                    expected_revision=plan.target_database_release_revision,
                    repo_root=inp.repo_root,
                )
                if not verified.ok:
                    raise MigrationRecoverError(verified.messages[0])
                assert_schema_release_graph_sha256(
                    deploy,
                    plan.target_database_release_revision,
                    plan.target_migrations_graph_sha256,
                    repo_root=inp.repo_root,
                )
                projected_database = next_database_state_after_migration(
                    previous=current.database,
                    target_revision=plan.target_database_release_revision,
                    target_heads=tuple(plan.target_database_heads),
                    migration_id=plan.migration_id,
                    compatibility_contract_sha256=plan.compatibility_contract_sha256,
                )
                assert_application_compatible_with_database(
                    target_revision=current.application.revision,
                    target_source_heads=target_heads,
                    database=projected_database,
                )
                if current_sha == plan.expected_post_migration_state_sha256:
                    messages.append("OK: current.json already at target database state")
                elif current_sha == plan.current_state_sha256:
                    next_database = projected_database
                    write_current_state(
                        deploy,
                        build_committed_current_state(
                            application=current.application,
                            database=next_database,
                            updated_at_utc=utc_now(),
                        ),
                    )
                    messages.append("OK: current.json updated to target database state")
                else:
                    raise MigrationRecoverError(
                        "current.json is neither planned source nor expected target state"
                    )
                terminal_outcome = STATUS_MIGRATION_COMMITTED

            recovery_id = new_recovery_id()
            rec_dir = migration_recovery_dir(mig_dir, recovery_id)
            write_migration_recovery_plan(
                rec_dir,
                MigrationRecoveryPlanDocument(
                    schema_version=MIGRATION_RECOVERY_PLAN_SCHEMA_VERSION,
                    recovery_id=recovery_id,
                    migration_id=inp.migration_id,
                    action=inp.action,
                    created_at_utc=utc_now(),
                ),
            )
            append_migration_recovery_event(
                rec_dir, STATUS_MIGRATION_REQUIRES_RECOVERY, f"recover {inp.action}"
            )
            if result is None and terminal_outcome == STATUS_MIGRATION_COMMITTED:
                write_migration_result(
                    mig_dir,
                    MigrationResultDocument(
                        schema_version=1,
                        migration_id=inp.migration_id,
                        status=STATUS_MIGRATION_COMMITTED,
                        failure_phase=STATUS_MIGRATION_STATE_COMMITTED,
                        result_sha256=plan.expected_post_migration_state_sha256,
                        recorded_at_utc=utc_now(),
                    ),
                )
                messages.append("OK: missing committed migration result published")
            write_migration_resolution(
                mig_dir,
                migration_id=inp.migration_id,
                action=inp.action,
                outcome=terminal_outcome,
                recovery_id=recovery_id,
            )
            write_migration_recovery_result(
                rec_dir,
                MigrationRecoveryResultDocument(
                    schema_version=1,
                    recovery_id=recovery_id,
                    migration_id=inp.migration_id,
                    outcome=terminal_outcome,
                    recorded_at_utc=utc_now(),
                ),
            )

            with open_backup_root_session(
                backup_root=backup_root,
                repo_root=inp.repo_root,
                wait_lock=False,
            ) as session:
                session.release_retention_hold(
                    hold_id=hold_id,
                    reason=f"migration recovery {inp.action}",
                )
            messages.append(f"OK: retention hold released ({hold_id})")
            messages.append(f"OK: migration recovery {inp.action} completed")
            return MigrationRecoverResult(
                ok=True,
                messages=tuple(messages),
                outcome=inp.action,
            )
    except (
        MigrationRecoverError,
        MigrationJournalError,
        RolloutLockError,
        RetentionHoldError,
        BackupSessionError,
        DbHeadsError,
        HealthError,
        ReleaseVerifyError,
        CompatibilityError,
        CurrentStateError,
        OSError,
        ValueError,
    ) as exc:
        return MigrationRecoverResult(
            ok=False,
            messages=(f"FAIL: {redact(str(exc))}",),
        )
