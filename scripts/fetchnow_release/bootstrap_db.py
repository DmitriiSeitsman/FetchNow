"""Initial database bootstrap transaction (schema only; no application activation)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .alembic_migrate import AlembicMigrateError, run_alembic_upgrade
from .bootstrap_freshness import (
    CLASS_INITIALIZED,
    BootstrapFreshness,
    BootstrapFreshnessError,
    inspect_bootstrap_freshness,
)
from .bootstrap_journal import (
    BootstrapIdentity,
    BootstrapJournalError,
    BootstrapPlanDocument,
    BootstrapResultDocument,
    append_bootstrap_event,
    bootstrap_dir,
    bootstrap_plan_matches_identity,
    ensure_bootstrap_layout,
    find_matching_committed_bootstrap,
    find_unresolved_bootstraps,
    latest_bootstrap_status,
    load_bootstrap_plan,
    load_bootstrap_result,
    new_bootstrap_id,
    prepare_bootstrap_dir,
    utc_now,
    write_bootstrap_plan,
    write_bootstrap_result,
)
from .bootstrap_project import BootstrapProjectError, assert_bootstrap_project
from .c2_constants import SOURCE_DIRNAME
from .c3b3_constants import (
    BOOTSTRAP_OVERRIDES_DIRNAME,
    BOOTSTRAP_PLAN_NAME,
    BOOTSTRAP_PLAN_SCHEMA_VERSION,
    LABEL_BOOTSTRAP_ID,
    LABEL_BOOTSTRAP_REVISION,
    STATUS_BOOTSTRAP_COMMITTED,
    STATUS_BOOTSTRAP_FAILED,
    STATUS_BOOTSTRAP_FRESHNESS_VERIFIED,
    STATUS_BOOTSTRAP_PLANNED,
    STATUS_BOOTSTRAP_POSTGRES_STARTED,
    STATUS_BOOTSTRAP_REQUIRES_RECOVERY,
    STATUS_BOOTSTRAP_SCHEMA_COMPLETED,
    STATUS_BOOTSTRAP_SCHEMA_STARTED,
    STATUS_BOOTSTRAP_TARGET_HEADS_VERIFIED,
)
from .current_state import current_path, load_and_resolve_current_state
from .db_heads import DbHeadsError, probe_database_heads, target_heads_from_release_source
from .deploy_root import DeployRootError, release_dir, validate_deploy_root
from .env_file import EnvFileError, load_env_file, require_keys
from .environment import (
    PRODUCTION,
    STAGING,
    assert_real_environment_bundle,
    real_identity,
    snapshot_compose_files_for_release,
)
from .image_identity import ImageIdentityError, assert_release_images_present
from .journal import JournalError, find_unresolved_deployments, sha256_file
from .manifest import ManifestError, load_manifest, manifest_path
from .migration_journal import find_unresolved_migrations
from .override import (
    OverrideError,
    compose_files_include_delivery,
    compose_files_include_storage_init,
    write_images_override,
)
from .password import PasswordError, validate_staging_password
from .postgres_bootstrap import PostgresBootstrapError, start_postgres_only
from .preflight import REQUIRED_ENV
from .redact import redact
from .revision import RevisionError, validate_full_sha
from .rollout_lock import RolloutLock, RolloutLockError
from .stabilize import StabilizationPolicy, StabilizeError, wait_services_healthy
from .verify_release import ReleaseVerifyError, verify_prepared_release


class BootstrapDbError(RuntimeError):
    """Initial database bootstrap failure."""


@dataclass(frozen=True)
class BootstrapDbInput:
    project_name: str
    env_file: Path
    compose_files: tuple[Path, ...]
    expected_revision: str
    repo_root: Path
    deploy_root: Path
    wait_lock: bool = False
    wait_lock_seconds: float | None = 30.0
    policy: StabilizationPolicy | None = None


@dataclass(frozen=True)
class BootstrapDbResult:
    ok: bool
    already_initialized: bool
    messages: tuple[str, ...]
    bootstrap_id: str | None = None
    status: str | None = None


def _assert_allowed_project(name: str) -> None:
    try:
        assert_bootstrap_project(name, allow_real=True)
    except BootstrapProjectError as exc:
        raise BootstrapDbError(
            f"project name must be {STAGING.project!r}, {PRODUCTION.project!r}, "
            f"or a validated fetchnow-bootstrap-test-* name, got {name!r}"
        ) from exc


def _require_current_absent(deploy: Path, *, repo_root: Path) -> None:
    if load_and_resolve_current_state(deploy, repo_root=repo_root) is not None:
        raise BootstrapDbError(
            "current.json already exists — initial DB bootstrap is refused"
        )
    if current_path(deploy).exists():
        raise BootstrapDbError("current.json path exists — refusing bootstrap")


def _require_no_unresolved(deploy: Path) -> None:
    unresolved_deployments = find_unresolved_deployments(deploy)
    if unresolved_deployments:
        raise BootstrapDbError(
            "unresolved deployment journal(s) block initial DB bootstrap: "
            + ", ".join(unresolved_deployments)
        )
    unresolved_migrations = find_unresolved_migrations(deploy)
    if unresolved_migrations:
        raise BootstrapDbError(
            "unresolved migration journal(s) block initial DB bootstrap: "
            + ", ".join(unresolved_migrations)
        )


def _write_terminal(
    *,
    boot_dir: Path,
    bootstrap_id: str,
    target_revision: str,
    status: str,
    already_initialized: bool,
    verified_heads: tuple[str, ...],
    messages: list[str],
) -> None:
    if load_bootstrap_result(boot_dir) is not None:
        return
    write_bootstrap_result(
        boot_dir,
        BootstrapResultDocument(
            schema_version=1,
            bootstrap_id=bootstrap_id,
            status=status,
            already_initialized=already_initialized,
            target_revision=target_revision,
            verified_db_heads=list(verified_heads),
            current_json_written=False,
            messages=list(messages),
            finished_at_utc=utc_now(),
        ),
    )


def _assert_no_current_json(deploy: Path) -> None:
    if current_path(deploy).exists():
        raise BootstrapDbError("bootstrap must not publish current.json")


def _heads_match_target(
    freshness: BootstrapFreshness, target_heads: frozenset[str]
) -> bool:
    live = freshness.initialized_heads
    return live is not None and live == target_heads


def run_bootstrap_db(inp: BootstrapDbInput) -> BootstrapDbResult:
    messages: list[str] = []
    boot_dir: Path | None = None
    bootstrap_id: str | None = None
    schema_started = False
    journal_mutable = False
    target_heads: frozenset[str] = frozenset()
    rev = ""
    try:
        _assert_allowed_project(inp.project_name)
        if not inp.compose_files:
            raise BootstrapDbError("at least one Compose file is required")

        env = load_env_file(inp.env_file)
        require_keys(env, REQUIRED_ENV)
        if env.get("COMPOSE_PROJECT_NAME") != inp.project_name:
            raise BootstrapDbError(
                "COMPOSE_PROJECT_NAME must match --project-name "
                f"({inp.project_name!r})"
            )
        rev = validate_full_sha(inp.expected_revision)
        env_rev = validate_full_sha(env["FETCHNOW_RELEASE_REVISION"])
        if rev != env_rev:
            raise BootstrapDbError(
                "expected revision does not match FETCHNOW_RELEASE_REVISION in env file"
            )
        validate_staging_password(env["POSTGRES_PASSWORD"])
        assert_real_environment_bundle(
            project_name=inp.project_name,
            env=env,
            env_file=inp.env_file,
            compose_files=inp.compose_files,
            deploy_root=inp.deploy_root,
            require_cli_backup_root=False,
            require_deploy_root=real_identity(inp.project_name) is not None,
        )

        deploy = validate_deploy_root(
            inp.deploy_root.expanduser().resolve(), repo_root=inp.repo_root
        )
        ensure_bootstrap_layout(deploy)
        _require_current_absent(deploy, repo_root=inp.repo_root)

        target_release = release_dir(deploy, rev)
        verified = verify_prepared_release(
            target_release, expected_revision=rev, repo_root=inp.repo_root
        )
        if not verified.ok:
            raise BootstrapDbError(verified.messages[0])
        messages.append("OK: target release verified")

        target_manifest = load_manifest(manifest_path(target_release))
        target_ids = assert_release_images_present(target_manifest)
        man_hash = sha256_file(manifest_path(target_release))
        snapshot_files = snapshot_compose_files_for_release(
            target_release, project_name=inp.project_name, manifest=target_manifest
        )
        cwd = target_release / SOURCE_DIRNAME
        overlay = snapshot_files[-1].name
        target_heads = target_heads_from_release_source(target_release)
        if not target_heads:
            raise BootstrapDbError("target release has no Alembic heads")
        target_heads_sorted = tuple(sorted(target_heads))
        contract_version = getattr(
            target_manifest, "source_contract_version", None
        )
        if type(contract_version) is not int:
            raise BootstrapDbError(
                "target release source_contract_version is required"
            )
        identity = BootstrapIdentity(
            project_name=inp.project_name,
            target_revision=rev,
            target_db_heads=target_heads_sorted,
            release_manifest_sha256=man_hash,
            target_api_image_id=target_ids["api"],
            compose_overlay=overlay,
            source_contract_version=contract_version,
        )

        with RolloutLock(
            deploy, wait=inp.wait_lock, wait_timeout_seconds=inp.wait_lock_seconds
        ):
            _require_current_absent(deploy, repo_root=inp.repo_root)
            _require_no_unresolved(deploy)

            freshness = inspect_bootstrap_freshness(
                project_name=inp.project_name,
                env_file=inp.env_file,
                compose_files=snapshot_files,
                cwd=cwd,
            )
            if freshness.application_services:
                raise BootstrapDbError(freshness.reason)

            unresolved = find_unresolved_bootstraps(deploy)
            committed = find_matching_committed_bootstrap(deploy, identity)
            resume_id: str | None = None

            if unresolved:
                if len(unresolved) != 1:
                    raise BootstrapDbError(
                        "unresolved bootstrap journal(s) block new bootstrap: "
                        + ", ".join(unresolved)
                    )
                resume_id = unresolved[0]
                existing_plan = load_bootstrap_plan(bootstrap_dir(deploy, resume_id))
                if not bootstrap_plan_matches_identity(existing_plan, identity):
                    raise BootstrapDbError(
                        "unresolved bootstrap journal "
                        f"{resume_id} does not match this project, revision, "
                        "prepared release identity, and target heads"
                    )
                bootstrap_id = resume_id
                boot_dir = bootstrap_dir(deploy, resume_id)
                journal_mutable = True
                messages.append(f"OK: resuming bootstrap journal {resume_id}")
            elif committed is not None:
                bootstrap_id = committed.bootstrap_id
                boot_dir = bootstrap_dir(deploy, committed.bootstrap_id)
                journal_mutable = False
                messages.append(
                    f"OK: found committed bootstrap journal {committed.bootstrap_id}"
                )
            else:
                if freshness.classification == CLASS_INITIALIZED:
                    raise BootstrapDbError(
                        "database already has Alembic heads "
                        f"{sorted(freshness.initialized_heads or ())} but no "
                        "committed bootstrap journal bound to this project, "
                        "revision, prepared release, and target heads; "
                        "refusing to adopt or overwrite an unknown database"
                    )
                if not freshness.uninitialized:
                    raise BootstrapDbError(
                        "initial DB bootstrap refused: " + freshness.reason
                    )
                bootstrap_id = new_bootstrap_id()
                boot_dir = prepare_bootstrap_dir(deploy, bootstrap_id)
                journal_mutable = True
                plan = BootstrapPlanDocument(
                    schema_version=BOOTSTRAP_PLAN_SCHEMA_VERSION,
                    bootstrap_id=bootstrap_id,
                    project_name=inp.project_name,
                    target_revision=rev,
                    target_db_heads=list(target_heads_sorted),
                    release_manifest_sha256=man_hash,
                    target_api_image_id=target_ids["api"],
                    compose_overlay=overlay,
                    source_contract_version=contract_version,
                    pgdata_volume_existed_before=freshness.pgdata_volume_present,
                    created_at_utc=utc_now(),
                )
                write_bootstrap_plan(boot_dir / BOOTSTRAP_PLAN_NAME, plan)
                append_bootstrap_event(
                    boot_dir, STATUS_BOOTSTRAP_PLANNED, "initial DB bootstrap plan"
                )
                messages.append(
                    f"OK: bootstrap journal created bootstrap_id={bootstrap_id}"
                )

            assert boot_dir is not None and bootstrap_id is not None
            latest = latest_bootstrap_status(boot_dir)

            if not freshness.postgres_healthy:
                start_postgres_only(
                    project_name=inp.project_name,
                    env_file=inp.env_file,
                    compose_files=snapshot_files,
                    cwd=cwd,
                    bootstrap_id=bootstrap_id,
                    target_revision=rev,
                )
                if journal_mutable and latest in {None, STATUS_BOOTSTRAP_PLANNED}:
                    append_bootstrap_event(
                        boot_dir,
                        STATUS_BOOTSTRAP_POSTGRES_STARTED,
                        "postgres started from immutable snapshot",
                    )
                    latest = STATUS_BOOTSTRAP_POSTGRES_STARTED
                messages.append("OK: postgres started (only)")
                wait_services_healthy(
                    project_name=inp.project_name,
                    env_file=inp.env_file,
                    compose_files=snapshot_files,
                    cwd=cwd,
                    services=("postgres",),
                    policy=inp.policy or StabilizationPolicy(),
                )
                messages.append("OK: postgres healthy")
                freshness = inspect_bootstrap_freshness(
                    project_name=inp.project_name,
                    env_file=inp.env_file,
                    compose_files=snapshot_files,
                    cwd=cwd,
                )
                if freshness.application_services:
                    raise BootstrapDbError(freshness.reason)

            if _heads_match_target(freshness, target_heads):
                if committed is None and resume_id is None:
                    raise BootstrapDbError(
                        "database already initialized without a bootstrap journal"
                    )
                messages.append(
                    "OK: database already initialized for this exact release"
                )
                _assert_no_current_json(deploy)
                if journal_mutable:
                    if latest in {
                        STATUS_BOOTSTRAP_PLANNED,
                        STATUS_BOOTSTRAP_POSTGRES_STARTED,
                    }:
                        append_bootstrap_event(
                            boot_dir,
                            STATUS_BOOTSTRAP_FRESHNESS_VERIFIED,
                            "already initialized for this exact release",
                        )
                        latest = STATUS_BOOTSTRAP_FRESHNESS_VERIFIED
                    if latest == STATUS_BOOTSTRAP_FRESHNESS_VERIFIED:
                        append_bootstrap_event(
                            boot_dir,
                            STATUS_BOOTSTRAP_COMMITTED,
                            "already initialized for this exact release",
                        )
                    _write_terminal(
                        boot_dir=boot_dir,
                        bootstrap_id=bootstrap_id,
                        target_revision=rev,
                        status=STATUS_BOOTSTRAP_COMMITTED,
                        already_initialized=True,
                        verified_heads=target_heads_sorted,
                        messages=messages,
                    )
                return BootstrapDbResult(
                    ok=True,
                    already_initialized=True,
                    messages=tuple(messages)
                    + (
                        f"bootstrap_id={bootstrap_id}",
                        f"verified_db_heads={list(target_heads_sorted)}",
                    ),
                    bootstrap_id=bootstrap_id,
                    status=STATUS_BOOTSTRAP_COMMITTED,
                )

            if committed is not None:
                raise BootstrapDbError(
                    "committed bootstrap journal exists for this identity but live "
                    "database heads do not match the target release"
                )

            if not freshness.uninitialized:
                raise BootstrapDbError(
                    "database is not a proven-empty bootstrap resource: "
                    + freshness.reason
                )

            if latest in {STATUS_BOOTSTRAP_PLANNED, STATUS_BOOTSTRAP_POSTGRES_STARTED}:
                append_bootstrap_event(
                    boot_dir,
                    STATUS_BOOTSTRAP_FRESHNESS_VERIFIED,
                    freshness.reason,
                )
                latest = STATUS_BOOTSTRAP_FRESHNESS_VERIFIED
                messages.append("OK: database freshness verified")

            override = write_images_override(
                boot_dir / BOOTSTRAP_OVERRIDES_DIRNAME,
                target_ids,
                include_delivery=compose_files_include_delivery(snapshot_files),
                include_storage_init=compose_files_include_storage_init(
                    snapshot_files
                ),
            )
            alembic_compose = (*snapshot_files, override)
            if latest == STATUS_BOOTSTRAP_FRESHNESS_VERIFIED:
                append_bootstrap_event(
                    boot_dir,
                    STATUS_BOOTSTRAP_SCHEMA_STARTED,
                    "alembic upgrade from immutable release snapshot",
                )
                latest = STATUS_BOOTSTRAP_SCHEMA_STARTED
            schema_started = True
            alembic_result = run_alembic_upgrade(
                project_name=inp.project_name,
                env_file=inp.env_file,
                compose_files=alembic_compose,
                cwd=cwd,
                target_heads=target_heads,
                migration_id=bootstrap_id,
                target_revision=rev,
                extra_env={
                    f"COMPOSE_LABEL_{LABEL_BOOTSTRAP_ID}": bootstrap_id,
                    f"COMPOSE_LABEL_{LABEL_BOOTSTRAP_REVISION}": rev,
                },
            )
            if alembic_result.exit_code != 0:
                raise BootstrapDbError(
                    "alembic upgrade failed: "
                    + (
                        alembic_result.stderr_summary
                        or alembic_result.stdout_summary
                        or f"exit {alembic_result.exit_code}"
                    )
                )
            if latest == STATUS_BOOTSTRAP_SCHEMA_STARTED:
                append_bootstrap_event(
                    boot_dir,
                    STATUS_BOOTSTRAP_SCHEMA_COMPLETED,
                    "alembic upgrade completed",
                )
            messages.append("OK: alembic upgrade completed")

            probe = probe_database_heads(
                project_name=inp.project_name,
                env_file=inp.env_file,
                compose_files=snapshot_files,
                cwd=cwd,
            )
            if not probe.has_heads or probe.heads != target_heads:
                raise BootstrapDbError(
                    "live database heads "
                    f"{sorted(probe.heads)} do not equal target release heads "
                    f"{list(target_heads_sorted)}"
                )
            append_bootstrap_event(
                boot_dir,
                STATUS_BOOTSTRAP_TARGET_HEADS_VERIFIED,
                "live Alembic heads equal target release heads",
            )
            messages.append(
                f"OK: live Alembic heads equal target {list(target_heads_sorted)}"
            )
            append_bootstrap_event(
                boot_dir,
                STATUS_BOOTSTRAP_COMMITTED,
                "initial schema committed; current.json not published",
            )
            _assert_no_current_json(deploy)
            messages.append("OK: current.json remains absent")
            messages.append("OK: application services were not started")
            _write_terminal(
                boot_dir=boot_dir,
                bootstrap_id=bootstrap_id,
                target_revision=rev,
                status=STATUS_BOOTSTRAP_COMMITTED,
                already_initialized=False,
                verified_heads=target_heads_sorted,
                messages=messages,
            )
            return BootstrapDbResult(
                ok=True,
                already_initialized=False,
                messages=tuple(messages),
                bootstrap_id=bootstrap_id,
                status=STATUS_BOOTSTRAP_COMMITTED,
            )
    except (
        BootstrapDbError,
        BootstrapJournalError,
        BootstrapProjectError,
        BootstrapFreshnessError,
        EnvFileError,
        RevisionError,
        PasswordError,
        DeployRootError,
        JournalError,
        ManifestError,
        ImageIdentityError,
        OverrideError,
        DbHeadsError,
        AlembicMigrateError,
        PostgresBootstrapError,
        StabilizeError,
        RolloutLockError,
        ReleaseVerifyError,
        OSError,
        ValueError,
    ) as exc:
        detail = redact(str(exc))
        messages.append(f"FAIL: {detail}")
        if boot_dir is not None and bootstrap_id is not None and journal_mutable:
            latest = latest_bootstrap_status(boot_dir)
            terminal = (
                STATUS_BOOTSTRAP_REQUIRES_RECOVERY
                if schema_started
                else STATUS_BOOTSTRAP_FAILED
            )
            if latest is not None and terminal != latest:
                try:
                    append_bootstrap_event(boot_dir, terminal, detail)
                except BootstrapJournalError:
                    messages.append("FAIL: could not append terminal bootstrap event")
            try:
                _write_terminal(
                    boot_dir=boot_dir,
                    bootstrap_id=bootstrap_id,
                    target_revision=rev or inp.expected_revision,
                    status=terminal,
                    already_initialized=False,
                    verified_heads=tuple(sorted(target_heads)),
                    messages=messages,
                )
            except BootstrapJournalError as journal_exc:
                messages.append(
                    "FAIL: could not publish bootstrap result: "
                    + redact(str(journal_exc))
                )
                return BootstrapDbResult(
                    ok=False,
                    already_initialized=False,
                    messages=tuple(messages),
                    bootstrap_id=bootstrap_id,
                    status=None,
                )
            return BootstrapDbResult(
                ok=False,
                already_initialized=False,
                messages=tuple(messages),
                bootstrap_id=bootstrap_id,
                status=terminal,
            )
        return BootstrapDbResult(
            ok=False,
            already_initialized=False,
            messages=tuple(messages),
            bootstrap_id=bootstrap_id,
            status=None,
        )
