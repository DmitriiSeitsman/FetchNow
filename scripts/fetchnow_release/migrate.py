"""Verified migration transaction orchestrator (PRD1C3B2B2)."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import STAGING_PROJECT
from .alembic_migrate import AlembicMigrateError, run_alembic_upgrade
from .application_compatibility import (
    CompatibilityError,
    DatabaseDriftError,
    assert_application_compatible_with_database,
    assert_live_matches_saved_database_heads,
)
from .c2_constants import SOURCE_DIRNAME
from .c3_constants import OVERRIDES_DIRNAME
from .c3b2b2_constants import (
    MIGRATION_PLAN_NAME,
    PRE_MUTATION_PHASES,
    STATUS_MIGRATION_APPLICATION_STABILIZED,
    STATUS_MIGRATION_BACKUP_STARTED,
    STATUS_MIGRATION_BACKUP_VERIFIED,
    STATUS_MIGRATION_COMMITTED,
    STATUS_MIGRATION_COMMAND_COMPLETED,
    STATUS_MIGRATION_FAILED,
    STATUS_MIGRATION_HOLD_ACQUIRED,
    STATUS_MIGRATION_PLANNED,
    STATUS_MIGRATION_PRE_MIGRATION_REVALIDATED,
    STATUS_MIGRATION_REQUIRES_RECOVERY,
    STATUS_MIGRATION_STARTED,
    STATUS_MIGRATION_STATE_COMMITTED,
    STATUS_MIGRATION_TARGET_HEADS_VERIFIED,
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
from .db_heads import DbHeadsError, database_heads_via_postgres, target_heads_from_release_source
from .deploy_root import DeployRootError, release_dir, validate_deploy_root
from .health import HealthError, HealthInput, run_health
from .image_identity import ImageIdentityError, assert_release_images_present
from .journal import JournalError, find_unresolved_deployments, utc_now
from .manifest import ManifestError, load_manifest, manifest_path
from .migration_journal import (
    MigrationJournalError,
    MigrationResultDocument,
    append_migration_event,
    append_migration_evidence_event,
    ensure_migration_layout,
    find_unresolved_migrations,
    latest_migration_status,
    migration_dir,
    write_migration_plan,
    write_migration_result,
)
from .migration_plan import MigrationPlanError, MigrationPlanInput, compute_migration_plan
from .migration_project import MigrationProjectError, assert_migration_project
from .override import (
    OverrideError,
    compose_files_include_delivery,
    write_images_override,
)
from .redact import redact
from .revision import RevisionError, validate_full_sha
from .rollout_lock import RolloutLock, RolloutLockError
from .verify_release import ReleaseVerifyError, verify_prepared_release

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from fetchnow_pg_backup.checksums import sha256_bytes  # noqa: E402
from fetchnow_pg_backup.compose_target import ComposeTarget  # noqa: E402
from fetchnow_pg_backup.session import (  # noqa: E402
    BackupSessionError,
    open_backup_root_session,
)

_MONITORED_SERVICES = ("api", "worker", "delivery", "web", "gateway", "postgres")


class MigrationError(RuntimeError):
    """Migration transaction failure."""


@dataclass(frozen=True)
class MigrationInput:
    project_name: str
    env_file: Path
    compose_files: tuple[Path, ...]
    expected_revision: str
    repo_root: Path
    deploy_root: Path
    backup_root: Path | None = None
    gateway_base_url: str = "http://127.0.0.1:8091"
    wait_lock: bool = False
    wait_lock_seconds: float | None = 30.0


@dataclass(frozen=True)
class MigrationResult:
    ok: bool
    already_migrated: bool
    messages: tuple[str, ...]
    migration_id: str | None = None
    status: str | None = None


def _assert_allowed_project(name: str) -> None:
    try:
        assert_migration_project(name, allow_staging=True)
    except MigrationProjectError as exc:
        raise MigrationError(
            f"project name must be {STAGING_PROJECT!r} or a validated "
            f"fetchnow-migration-test-* name, got {name!r}"
        ) from exc


def _release_compose_files(release_directory: Path) -> tuple[Path, ...]:
    source = release_directory / SOURCE_DIRNAME
    return (
        (source / "compose.yaml").resolve(),
        (source / "compose.staging.yaml").resolve(),
    )


def _compose_argv(
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
    compose_args: tuple[str, ...],
) -> list[str]:
    argv = [
        "docker",
        "compose",
        "--env-file",
        str(env_file),
        "--project-name",
        project_name,
    ]
    for path in compose_files:
        argv.extend(["-f", str(path)])
    argv.extend(compose_args)
    return argv


def _snapshot_container_ids(
    *,
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
    cwd: Path,
) -> dict[str, str]:
    ids: dict[str, str] = {}
    for service in _MONITORED_SERVICES:
        proc = subprocess.run(
            _compose_argv(
                project_name,
                env_file,
                compose_files,
                ("ps", "-q", service),
            ),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise MigrationError(
                f"compose ps failed for {service}: "
                + redact(proc.stderr.strip() or proc.stdout.strip() or "unknown")
            )
        cid = proc.stdout.strip()
        if not cid:
            raise MigrationError(f"missing running container for {service}")
        ids[service] = cid
    return ids


def _assert_container_ids_unchanged(
    before: dict[str, str], after: dict[str, str]
) -> None:
    for service in _MONITORED_SERVICES:
        if before.get(service) != after.get(service):
            raise MigrationError(
                f"{service} container recreated during migration: "
                f"before={before.get(service)!r} after={after.get(service)!r}"
            )


def _verify_already_migrated(
    inp: MigrationInput,
    deploy: Path,
    messages: list[str],
) -> MigrationResult:
    current = load_and_resolve_current_state(deploy, repo_root=inp.repo_root)
    if current is None:
        raise MigrationError("current.json missing during already-migrated verify")

    current_release = release_dir(deploy, current.application.revision)
    verified = verify_prepared_release(
        current_release,
        expected_revision=current.application.revision,
        repo_root=inp.repo_root,
    )
    if not verified.ok:
        raise MigrationError(verified.messages[0])

    target_revision = validate_full_sha(inp.expected_revision)
    target_release = release_dir(deploy, target_revision)
    target_verified = verify_prepared_release(
        target_release,
        expected_revision=target_revision,
        repo_root=inp.repo_root,
    )
    if not target_verified.ok:
        raise MigrationError(target_verified.messages[0])

    cwd = current_release / SOURCE_DIRNAME
    live_heads = database_heads_via_postgres(
        project_name=inp.project_name,
        env_file=inp.env_file,
        compose_files=inp.compose_files,
        cwd=cwd,
    )
    assert_live_matches_saved_database_heads(
        live_heads=live_heads,
        saved_heads=current.database.heads,
    )
    target_heads = target_heads_from_release_source(target_release)
    assert_application_compatible_with_database(
        target_revision=current.application.revision,
        target_source_heads=target_heads,
        database=current.database,
    )
    messages.append("OK: live database heads match saved state")
    messages.append("OK: current application compatible with target schema")

    health_inp = HealthInput(
        project_name=inp.project_name,
        env_file=inp.env_file,
        compose_files=inp.compose_files,
        expected_revision=current.application.revision,
        repo_root=cwd,
        gateway_base_url=inp.gateway_base_url,
        expected_image_ids=dict(current.application.image_ids),
    )
    health = run_health(health_inp)
    if not health.ok:
        raise MigrationError(health.messages[0])
    messages.append("OK: application health verified (no mutation)")
    messages.append(f"target_schema_revision={target_revision}")
    return MigrationResult(
        ok=True,
        already_migrated=True,
        messages=tuple(messages),
        migration_id=None,
        status=STATUS_MIGRATION_COMMITTED,
    )


def _failure_phase(*, post_mutation: bool, latest_status: str | None) -> str | None:
    if post_mutation:
        return latest_status
    if latest_status in PRE_MUTATION_PHASES:
        return latest_status
    return STATUS_MIGRATION_PLANNED


def _release_hold_if_needed(
    *,
    backup_root: Path,
    repo_root: Path,
    hold_id: str | None,
    reason: str,
    messages: list[str],
) -> None:
    if hold_id is None:
        return
    try:
        with open_backup_root_session(
            backup_root=backup_root, repo_root=repo_root, wait_lock=False
        ) as session:
            session.release_retention_hold(hold_id=hold_id, reason=reason)
        messages.append(f"OK: retention hold released ({hold_id})")
    except Exception as exc:  # noqa: BLE001
        messages.append(f"WARN: failed to release retention hold: {redact(str(exc))}")


def _terminal_failure(
    *,
    mig_dir: Path | None,
    migration_id: str | None,
    hold_id: str | None,
    backup_root: Path | None,
    repo_root: Path,
    messages: list[str],
    detail: str,
    post_mutation: bool,
    latest_status: str | None,
) -> MigrationResult:
    status = (
        STATUS_MIGRATION_REQUIRES_RECOVERY
        if post_mutation
        else STATUS_MIGRATION_FAILED
    )
    if mig_dir is not None and migration_id is not None:
        try:
            if post_mutation:
                append_migration_event(
                    mig_dir, STATUS_MIGRATION_REQUIRES_RECOVERY, detail
                )
            else:
                append_migration_event(mig_dir, STATUS_MIGRATION_FAILED, detail)
            write_migration_result(
                mig_dir,
                MigrationResultDocument(
                    schema_version=1,
                    migration_id=migration_id,
                    status=status,
                    failure_phase=_failure_phase(
                        post_mutation=post_mutation, latest_status=latest_status
                    ),
                    result_sha256=None,
                    recorded_at_utc=utc_now(),
                ),
            )
        except Exception as journal_exc:  # noqa: BLE001
            messages.append(
                f"WARN: failed to publish terminal migration journal: "
                f"{redact(str(journal_exc))}"
            )
    if not post_mutation:
        if backup_root is not None:
            _release_hold_if_needed(
                backup_root=backup_root,
                repo_root=repo_root,
                hold_id=hold_id,
                reason=f"pre-mutation failure: {detail}",
                messages=messages,
            )
    messages.append(f"FAIL: {detail}")
    return MigrationResult(
        ok=False,
        already_migrated=False,
        messages=tuple(messages),
        migration_id=migration_id,
        status=status,
    )


def run_migration(inp: MigrationInput) -> MigrationResult:
    messages: list[str] = []
    migration_id: str | None = None
    mig_dir: Path | None = None
    hold_id: str | None = None
    latest_status: str | None = None
    post_mutation = False
    backup_root: Path | None = None

    try:
        _assert_allowed_project(inp.project_name)
        if not inp.compose_files:
            raise MigrationError("at least one Compose file is required")

        rev = validate_full_sha(inp.expected_revision)
        deploy = validate_deploy_root(inp.deploy_root, repo_root=inp.repo_root)
        ensure_migration_layout(deploy)

        with RolloutLock(
            deploy, wait=inp.wait_lock, wait_timeout_seconds=inp.wait_lock_seconds
        ):
            unresolved_deployments = find_unresolved_deployments(deploy)
            if unresolved_deployments:
                raise MigrationError(
                    "unresolved deployment journal(s) block migration: "
                    + ", ".join(unresolved_deployments)
                    + "; use recover --deployment-id …"
                )
            unresolved_migrations = find_unresolved_migrations(deploy)
            if unresolved_migrations:
                raise MigrationError(
                    "unresolved migration journal(s) block new migration: "
                    + ", ".join(unresolved_migrations)
                )

            plan_input = MigrationPlanInput(
                project_name=inp.project_name,
                env_file=inp.env_file,
                compose_files=inp.compose_files,
                expected_revision=rev,
                repo_root=inp.repo_root,
                deploy_root=deploy,
                backup_root=inp.backup_root,
            )
            computed = compute_migration_plan(plan_input)
            messages.extend(computed.messages)
            if not computed.ok:
                raise MigrationPlanError(computed.messages[-1].removeprefix("FAIL: "))

            if computed.already_migrated:
                return _verify_already_migrated(inp, deploy, messages)

            plan = computed.plan
            if plan is None:
                raise MigrationError("migration plan missing despite ok=True")

            migration_id = plan.migration_id
            mig_dir = migration_dir(deploy, migration_id)
            if mig_dir.exists():
                raise MigrationError(f"migration journal already exists: {migration_id}")
            mig_dir.mkdir(mode=0o700, parents=False)
            write_migration_plan(mig_dir / MIGRATION_PLAN_NAME, plan)
            append_migration_event(mig_dir, STATUS_MIGRATION_PLANNED, "plan published")
            latest_status = STATUS_MIGRATION_PLANNED
            messages.append(f"OK: migration journal created migration_id={migration_id}")

            from .env_file import load_env_file

            env = load_env_file(inp.env_file)
            backup_root = Path(inp.backup_root or env["FETCHNOW_BACKUP_ROOT"]).expanduser().resolve()

            current = load_and_resolve_current_state(deploy, repo_root=inp.repo_root)
            if current is None:
                raise MigrationError("current.json missing at migration start")
            current_release = release_dir(deploy, current.application.revision)
            cwd = current_release / SOURCE_DIRNAME

            schema_release = release_dir(deploy, plan.source_database_release_revision)
            source_versions_dir = (
                schema_release
                / SOURCE_DIRNAME
                / "backend"
                / "migrations"
                / "versions"
            )

            compose_target = ComposeTarget(
                project_name=inp.project_name,
                env_file=inp.env_file,
                compose_files=inp.compose_files,
                repo_root=inp.repo_root,
            )

            append_migration_event(
                mig_dir, STATUS_MIGRATION_BACKUP_STARTED, "create verified backup"
            )
            latest_status = STATUS_MIGRATION_BACKUP_STARTED
            with open_backup_root_session(
                backup_root=backup_root, repo_root=inp.repo_root, wait_lock=False
            ) as backup_session:
                create_result = backup_session.create_backup(target=compose_target)
                messages.append(f"OK: backup created backup_id={create_result.backup_id}")

                verified = backup_session.verify_backup(
                    target=compose_target,
                    backup_dir=create_result.backup_dir,
                    expected_alembic_heads=tuple(plan.source_database_heads),
                    migrations_versions_dir=source_versions_dir,
                )
                if not verified.passed:
                    reason = verified.failure_reason or "backup verification failed"
                    raise MigrationError(reason)
                append_migration_evidence_event(
                    mig_dir,
                    status=STATUS_MIGRATION_BACKUP_VERIFIED,
                    detail="backup verified with exact head proof",
                    evidence={
                        "backup_id": create_result.backup_id,
                        "backup_manifest_sha256": create_result.manifest.sha256,
                        "backup_dump_sha256": create_result.manifest.sha256,
                        "attestation_schema_version": verified.attestation_schema_version,
                        "attestation_sha256": verified.attestation_sha256,
                        "expected_alembic_heads": list(plan.source_database_heads),
                        "static_alembic_heads": list(verified.static_alembic_heads or ()),
                        "verified_restored_heads": list(
                            verified.verified_restored_heads or ()
                        ),
                        "migrations_graph_sha256": verified.migrations_graph_sha256,
                        "retention_hold_id": None,
                    },
                )
                latest_status = STATUS_MIGRATION_BACKUP_VERIFIED
                messages.append("OK: backup verified")

                hold = backup_session.acquire_retention_hold(
                    backup_dir=create_result.backup_dir,
                    migration_id=migration_id,
                    verified=verified,
                )
                hold_id = hold.hold_id
                append_migration_evidence_event(
                    mig_dir,
                    status=STATUS_MIGRATION_HOLD_ACQUIRED,
                    detail=f"retention hold acquired hold_id={hold_id}",
                    evidence={
                        "backup_id": create_result.backup_id,
                        "retention_hold_id": hold_id,
                        "attestation_sha256": verified.attestation_sha256,
                    },
                )
                latest_status = STATUS_MIGRATION_HOLD_ACQUIRED
                messages.append(f"OK: retention hold acquired hold_id={hold_id}")

            live_before = database_heads_via_postgres(
                project_name=inp.project_name,
                env_file=inp.env_file,
                compose_files=inp.compose_files,
                cwd=cwd,
            )
            if frozenset(live_before) != frozenset(plan.source_database_heads):
                raise MigrationError(
                    "live database heads drifted from plan source heads before Alembic"
                )
            append_migration_event(
                mig_dir,
                STATUS_MIGRATION_PRE_MIGRATION_REVALIDATED,
                "source heads revalidated",
            )
            latest_status = STATUS_MIGRATION_PRE_MIGRATION_REVALIDATED
            messages.append("OK: pre-migration source heads revalidated")

            container_ids_before = _snapshot_container_ids(
                project_name=inp.project_name,
                env_file=inp.env_file,
                compose_files=inp.compose_files,
                cwd=cwd,
            )

            target_release = release_dir(deploy, plan.target_database_release_revision)
            target_manifest = load_manifest(manifest_path(target_release))
            target_ids = assert_release_images_present(target_manifest)
            if target_ids["api"] != plan.target_api_image_id:
                raise MigrationError(
                    "target API image ID mismatch between plan and verified release"
                )
            override = write_images_override(
                mig_dir / OVERRIDES_DIRNAME,
                target_ids,
                include_delivery=compose_files_include_delivery(
                    _release_compose_files(target_release)
                ),
            )
            alembic_compose = (*_release_compose_files(target_release), override)
            alembic_cwd = target_release / SOURCE_DIRNAME

            append_migration_event(mig_dir, STATUS_MIGRATION_STARTED, "alembic upgrade")
            latest_status = STATUS_MIGRATION_STARTED
            post_mutation = True
            alembic_result = run_alembic_upgrade(
                project_name=inp.project_name,
                env_file=inp.env_file,
                compose_files=alembic_compose,
                cwd=alembic_cwd,
                target_heads=tuple(plan.target_database_heads),
                migration_id=migration_id,
                target_revision=plan.target_database_release_revision,
            )
            if alembic_result.exit_code != 0:
                raise MigrationError(
                    "alembic upgrade failed: "
                    + (
                        alembic_result.stderr_summary
                        or alembic_result.stdout_summary
                        or f"exit {alembic_result.exit_code}"
                    )
                )
            append_migration_event(
                mig_dir,
                STATUS_MIGRATION_COMMAND_COMPLETED,
                "alembic upgrade completed",
            )
            latest_status = STATUS_MIGRATION_COMMAND_COMPLETED
            messages.append("OK: alembic upgrade completed")

            live_after = database_heads_via_postgres(
                project_name=inp.project_name,
                env_file=inp.env_file,
                compose_files=inp.compose_files,
                cwd=cwd,
            )
            if frozenset(live_after) != frozenset(plan.target_database_heads):
                raise MigrationError(
                    "live database heads do not match target after Alembic upgrade"
                )
            append_migration_event(
                mig_dir,
                STATUS_MIGRATION_TARGET_HEADS_VERIFIED,
                "target heads verified",
            )
            latest_status = STATUS_MIGRATION_TARGET_HEADS_VERIFIED
            messages.append("OK: target database heads verified")

            health_inp = HealthInput(
                project_name=inp.project_name,
                env_file=inp.env_file,
                compose_files=inp.compose_files,
                expected_revision=current.application.revision,
                repo_root=cwd,
                gateway_base_url=inp.gateway_base_url,
                expected_image_ids=dict(current.application.image_ids),
            )
            health = run_health(health_inp)
            if not health.ok:
                raise MigrationError(health.messages[0])
            append_migration_event(
                mig_dir,
                STATUS_MIGRATION_APPLICATION_STABILIZED,
                "application health verified unchanged",
            )
            latest_status = STATUS_MIGRATION_APPLICATION_STABILIZED
            messages.append("OK: application health verified unchanged")

            container_ids_after = _snapshot_container_ids(
                project_name=inp.project_name,
                env_file=inp.env_file,
                compose_files=inp.compose_files,
                cwd=cwd,
            )
            _assert_container_ids_unchanged(container_ids_before, container_ids_after)
            messages.append("OK: application/postgres container IDs unchanged")

            current_bytes = current_path(deploy).read_bytes()
            if sha256_bytes(current_bytes) != plan.current_state_sha256:
                raise MigrationError(
                    "current.json CAS mismatch — concurrent writer detected"
                )
            assert_schema_release_graph_sha256(
                deploy,
                plan.target_database_release_revision,
                plan.target_migrations_graph_sha256,
                repo_root=inp.repo_root,
            )
            next_database = next_database_state_after_migration(
                previous=current.database,
                target_revision=plan.target_database_release_revision,
                target_heads=tuple(plan.target_database_heads),
                migration_id=migration_id,
                compatibility_contract_sha256=plan.compatibility_contract_sha256,
            )
            write_current_state(
                deploy,
                build_committed_current_state(
                    application=current.application,
                    database=next_database,
                    updated_at_utc=utc_now(),
                ),
            )
            append_migration_event(
                mig_dir, STATUS_MIGRATION_STATE_COMMITTED, "current.json published"
            )
            latest_status = STATUS_MIGRATION_STATE_COMMITTED
            messages.append("OK: current.json updated")

            append_migration_event(
                mig_dir, STATUS_MIGRATION_COMMITTED, "migration transaction committed"
            )
            latest_status = STATUS_MIGRATION_COMMITTED

            write_migration_result(
                mig_dir,
                MigrationResultDocument(
                    schema_version=1,
                    migration_id=migration_id,
                    status=STATUS_MIGRATION_COMMITTED,
                    failure_phase=None,
                    result_sha256=plan.expected_post_migration_state_sha256,
                    recorded_at_utc=utc_now(),
                ),
            )
            messages.append("OK: migration committed")

            if hold_id is not None:
                _release_hold_if_needed(
                    backup_root=backup_root,
                    repo_root=inp.repo_root,
                    hold_id=hold_id,
                    reason="migration committed",
                    messages=messages,
                )

            messages.append(
                f"target_schema_revision={plan.target_database_release_revision}"
            )
            return MigrationResult(
                ok=True,
                already_migrated=False,
                messages=tuple(messages),
                migration_id=migration_id,
                status=STATUS_MIGRATION_COMMITTED,
            )
    except (
        MigrationError,
        MigrationPlanError,
        JournalError,
        MigrationJournalError,
        ManifestError,
        OverrideError,
        DbHeadsError,
        ImageIdentityError,
        AlembicMigrateError,
        RolloutLockError,
        ReleaseVerifyError,
        RevisionError,
        CurrentStateError,
        CompatibilityError,
        DatabaseDriftError,
        BackupSessionError,
        HealthError,
        DeployRootError,
        OSError,
        ValueError,
    ) as exc:
        detail = redact(str(exc))
        if mig_dir is not None:
            latest_status = latest_migration_status(mig_dir) or latest_status
        if migration_id is not None and mig_dir is not None:
            return _terminal_failure(
                mig_dir=mig_dir,
                migration_id=migration_id,
                hold_id=hold_id,
                backup_root=backup_root,
                repo_root=inp.repo_root,
                messages=messages,
                detail=detail,
                post_mutation=post_mutation,
                latest_status=latest_status,
            )
        return MigrationResult(
            ok=False,
            already_migrated=False,
            messages=tuple(messages) + (f"FAIL: {detail}",),
            migration_id=migration_id,
            status=None,
        )
