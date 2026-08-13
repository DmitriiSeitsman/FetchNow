"""Explicit recovery for unresolved PRD1C3A deployment journals."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .activate import activate_app_tier, activate_gateway
from .application_compatibility import (
    CompatibilityError,
    DatabaseDriftError,
    assert_application_compatible_with_database,
    assert_live_matches_saved_database_heads,
)
from .c2_constants import SOURCE_DIRNAME
from .c3_constants import (
    APP_SERVICES_BEFORE_GATEWAY,
    OVERRIDES_DIRNAME,
    RECOVERY_PLAN_SCHEMA_VERSION,
    RESOLUTION_OUTCOME_ACCEPT_TARGET,
    RESOLUTION_OUTCOME_ROLLBACK,
    RESOLUTION_SCHEMA_VERSION,
    STATUS_COMMITTED,
    STATUS_FAILED,
    STATUS_PLANNED,
    STATUS_ROLLBACK_FAILED,
    STATUS_ROLLED_BACK,
    STATUS_STABILIZING,
)
from .current_state import (
    ApplicationState,
    CurrentStateError,
    build_bootstrap_database_state,
    build_committed_current_state,
    load_and_resolve_current_state,
    next_database_state_after_app_commit,
)
from .db_heads import assert_heads_equal, database_heads_via_postgres, target_heads_from_release_source
from .deploy_root import release_dir, validate_deploy_root
from .health import HealthInput
from .image_identity import assert_release_images_present
from .journal import (
    JournalError,
    RecoveryPlanDocument,
    ResolutionDocument,
    append_event,
    append_recovery_event,
    deployment_dir,
    ensure_rollout_layout,
    latest_event_status,
    load_plan,
    load_resolution,
    load_result,
    new_recovery_id,
    parse_resolution,
    parse_result,
    recovery_dir,
    result_path,
    sha256_file,
    utc_now,
    validate_resolution,
    write_current_state,
    write_recovery_plan,
    write_recovery_result,
    write_resolution,
    write_result,
)
from .manifest import load_manifest, manifest_path
from .override import compose_files_include_delivery, write_images_override
from .redact import redact
from .rollout_lock import RolloutLock
from .stabilize import StabilizationPolicy, stabilize_full_health, wait_services_healthy
from .verify_release import verify_prepared_release


class RecoverError(RuntimeError):
    """Recovery command failure."""


@dataclass(frozen=True)
class RecoverInput:
    project_name: str
    env_file: Path
    deploy_root: Path
    repo_root: Path
    deployment_id: str
    action: str  # accept-target | rollback
    gateway_base_url: str = "http://127.0.0.1:8091"
    wait_lock: bool = False
    policy: StabilizationPolicy | None = None


@dataclass(frozen=True)
class RecoverResult:
    ok: bool
    messages: tuple[str, ...]
    recovery_id: str | None = None


def _release_compose_files(release_directory: Path) -> tuple[Path, ...]:
    source = release_directory / SOURCE_DIRNAME
    return (
        (source / "compose.yaml").resolve(),
        (source / "compose.staging.yaml").resolve(),
    )


def _compose_with_override(release_directory: Path, override: Path) -> tuple[Path, ...]:
    return (*_release_compose_files(release_directory), override.resolve())


def _idempotent_if_resolved(dep_dir: Path) -> RecoverResult | None:
    resolution_raw = load_resolution(dep_dir)
    if resolution_raw is None:
        return None
    try:
        resolution = parse_resolution(resolution_raw)
        validate_resolution(dep_dir, resolution)
    except JournalError:
        return None
    return RecoverResult(
        ok=True,
        messages=("OK: deployment already resolved by recovery",),
        recovery_id=resolution.recovery_id,
    )


def _execute_accept_target(
    *,
    inp: RecoverInput,
    deploy: Path,
    plan,
    dep_dir: Path,
    compose_override_dir: Path,
) -> str:
    release = release_dir(deploy, plan.target_revision)
    verified = verify_prepared_release(
        release, expected_revision=plan.target_revision, repo_root=inp.repo_root
    )
    if not verified.ok:
        raise RecoverError(verified.messages[0])
    man_hash = sha256_file(manifest_path(release))
    if man_hash != plan.release_manifest_sha256:
        raise RecoverError("target release manifest hash does not match plan.json")
    assert_release_images_present(load_manifest(manifest_path(release)))
    source = release / SOURCE_DIRNAME
    base_compose = (
        (source / "compose.yaml").resolve(),
        (source / "compose.staging.yaml").resolve(),
    )
    cwd = source
    db_heads = database_heads_via_postgres(
        project_name=inp.project_name,
        env_file=inp.env_file,
        compose_files=base_compose,
        cwd=cwd,
    )
    # Plan snapshot must still match live DB (C3A plan heads).
    assert_heads_equal(
        database_heads=db_heads,
        target_heads=frozenset(plan.database_heads),
    )
    target_heads = target_heads_from_release_source(release)
    current = load_and_resolve_current_state(deploy, repo_root=inp.repo_root)
    if current is None:
        if not plan.bootstrap:
            raise RecoverError(
                "accept-target requires current.json unless recovering a bootstrap plan"
            )
        assert_heads_equal(database_heads=db_heads, target_heads=target_heads)
        database = build_bootstrap_database_state(
            heads=db_heads,
            schema_release_revision=plan.target_revision,
        )
    else:
        assert_live_matches_saved_database_heads(
            live_heads=db_heads,
            saved_heads=current.database.heads,
        )
        assert_application_compatible_with_database(
            target_revision=plan.target_revision,
            target_source_heads=target_heads,
            database=current.database,
        )
        database = next_database_state_after_app_commit(
            previous=current.database,
            target_revision=plan.target_revision,
            target_source_heads=target_heads,
        )
    override = write_images_override(
        compose_override_dir,
        plan.target_image_ids,
        deployment_id=plan.deployment_id,
        release_revision=plan.target_revision,
        include_delivery=compose_files_include_delivery(
            _release_compose_files(release)
        ),
    )
    compose_files = _compose_with_override(release, override)
    health_inp = HealthInput(
        project_name=inp.project_name,
        env_file=inp.env_file,
        compose_files=compose_files,
        expected_revision=plan.target_revision,
        repo_root=cwd,
        gateway_base_url=inp.gateway_base_url,
        expected_image_ids=plan.target_image_ids,
    )
    stabilize_full_health(health_inp, policy=inp.policy)
    write_current_state(
        deploy,
        build_committed_current_state(
            application=ApplicationState(
                revision=plan.target_revision,
                release_manifest_sha256=plan.release_manifest_sha256,
                image_ids=dict(plan.target_image_ids),
                deployment_id=plan.deployment_id,
            ),
            database=database,
            updated_at_utc=utc_now(),
        ),
    )
    return plan.target_revision


def _execute_rollback(
    *,
    inp: RecoverInput,
    deploy: Path,
    plan,
    compose_override_dir: Path,
) -> str:
    if plan.bootstrap or not plan.previous_revision or not plan.previous_image_ids:
        raise RecoverError(
            "rollback recover requires a verified previous release "
            "(bootstrap has no previous application target)"
        )
    prev = plan.previous_revision
    prev_release = release_dir(deploy, prev)
    verified = verify_prepared_release(
        prev_release, expected_revision=prev, repo_root=inp.repo_root
    )
    if not verified.ok:
        raise RecoverError(verified.messages[0])
    assert_release_images_present(load_manifest(manifest_path(prev_release)))
    for svc, image_id in plan.previous_image_ids.items():
        from .image_build import image_exists

        if not image_exists(image_id):
            raise RecoverError(
                f"previous immutable image missing for {svc}: {image_id}"
            )
    # Compatibility gate before any container mutation.
    pre_heads = target_heads_from_release_source(prev_release)
    pre_compose = (
        (prev_release / SOURCE_DIRNAME / "compose.yaml").resolve(),
        (prev_release / SOURCE_DIRNAME / "compose.staging.yaml").resolve(),
    )
    pre_live = database_heads_via_postgres(
        project_name=inp.project_name,
        env_file=inp.env_file,
        compose_files=pre_compose,
        cwd=prev_release / SOURCE_DIRNAME,
    )
    pre_current = load_and_resolve_current_state(deploy, repo_root=inp.repo_root)
    if pre_current is None:
        raise RecoverError(
            "rollback recover requires resolved current.json with database state"
        )
    assert_live_matches_saved_database_heads(
        live_heads=pre_live,
        saved_heads=pre_current.database.heads,
    )
    assert_application_compatible_with_database(
        target_revision=prev,
        target_source_heads=pre_heads,
        database=pre_current.database,
    )
    override = write_images_override(
        compose_override_dir,
        plan.previous_image_ids,
        deployment_id=plan.deployment_id,
        release_revision=prev,
        include_delivery=compose_files_include_delivery(
            _release_compose_files(prev_release)
        ),
    )
    compose_files = _compose_with_override(prev_release, override)
    cwd = prev_release / SOURCE_DIRNAME
    activate_app_tier(
        project_name=inp.project_name,
        env_file=inp.env_file,
        compose_files=compose_files,
        cwd=cwd,
    )
    wait_services_healthy(
        project_name=inp.project_name,
        env_file=inp.env_file,
        compose_files=compose_files,
        cwd=cwd,
        services=APP_SERVICES_BEFORE_GATEWAY,
        policy=inp.policy or StabilizationPolicy(),
    )
    activate_gateway(
        project_name=inp.project_name,
        env_file=inp.env_file,
        compose_files=compose_files,
        cwd=cwd,
    )
    health_inp = HealthInput(
        project_name=inp.project_name,
        env_file=inp.env_file,
        compose_files=compose_files,
        expected_revision=prev,
        repo_root=cwd,
        gateway_base_url=inp.gateway_base_url,
        expected_image_ids=plan.previous_image_ids,
    )
    stabilize_full_health(health_inp, policy=inp.policy)
    prev_heads = target_heads_from_release_source(prev_release)
    base_compose = (
        (prev_release / SOURCE_DIRNAME / "compose.yaml").resolve(),
        (prev_release / SOURCE_DIRNAME / "compose.staging.yaml").resolve(),
    )
    live_heads = database_heads_via_postgres(
        project_name=inp.project_name,
        env_file=inp.env_file,
        compose_files=base_compose,
        cwd=prev_release / SOURCE_DIRNAME,
    )
    current = load_and_resolve_current_state(deploy, repo_root=inp.repo_root)
    if current is None:
        raise RecoverError(
            "rollback recover requires resolved current.json with database state"
        )
    assert_live_matches_saved_database_heads(
        live_heads=live_heads,
        saved_heads=current.database.heads,
    )
    assert_application_compatible_with_database(
        target_revision=prev,
        target_source_heads=prev_heads,
        database=current.database,
    )
    if current.revision != prev:
        write_current_state(
            deploy,
            build_committed_current_state(
                application=ApplicationState(
                    revision=prev,
                    release_manifest_sha256=sha256_file(manifest_path(prev_release)),
                    image_ids=dict(plan.previous_image_ids),
                    deployment_id=plan.deployment_id,
                ),
                database=current.database,
                updated_at_utc=utc_now(),
            ),
        )
    return prev


def recover_deployment(inp: RecoverInput) -> RecoverResult:
    messages: list[str] = []
    recovery_id: str | None = None
    try:
        if inp.action not in {"accept-target", "rollback"}:
            raise RecoverError("action must be accept-target or rollback")
        deploy = validate_deploy_root(inp.deploy_root, repo_root=inp.repo_root)
        ensure_rollout_layout(deploy)
        with RolloutLock(deploy, wait=inp.wait_lock):
            from .migration_journal import find_unresolved_migrations

            unresolved_migrations = find_unresolved_migrations(deploy)
            if unresolved_migrations:
                raise RecoverError(
                    "unresolved migration journal(s) block application recovery: "
                    + ", ".join(unresolved_migrations)
                    + "; use recover-migration …"
                )
            dep_dir = deployment_dir(deploy, inp.deployment_id)
            if not dep_dir.is_dir():
                raise RecoverError(f"deployment journal not found: {inp.deployment_id}")

            resolved = _idempotent_if_resolved(dep_dir)
            if resolved is not None:
                return resolved

            plan = load_plan(dep_dir)
            if plan.project_name != inp.project_name:
                raise RecoverError(
                    f"plan project_name {plan.project_name!r} != "
                    f"requested {inp.project_name!r}"
                )

            existing_raw = load_result(dep_dir)
            if existing_raw is not None:
                existing = parse_result(existing_raw)
                if existing.status == STATUS_COMMITTED:
                    raise RecoverError("deployment already committed")
                if existing.status == STATUS_ROLLED_BACK and inp.action == "rollback":
                    raise RecoverError(
                        "deployment already rolled_back; recovery not required"
                    )
                if existing.status == STATUS_FAILED:
                    raise RecoverError(
                        "deployment failed before rollback; recovery not applicable"
                    )
                if existing.status != STATUS_ROLLBACK_FAILED:
                    raise RecoverError(
                        f"deployment result {existing.status!r} is not recoverable"
                    )
                # rollback_failed: immutable original result; recovery sub-journal.
                recovery_id = new_recovery_id()
                rec_dir = recovery_dir(dep_dir, recovery_id)
                write_recovery_plan(
                    rec_dir,
                    RecoveryPlanDocument(
                        schema_version=RECOVERY_PLAN_SCHEMA_VERSION,
                        recovery_id=recovery_id,
                        deployment_id=plan.deployment_id,
                        action=inp.action,
                        created_at_utc=utc_now(),
                    ),
                )
                append_recovery_event(rec_dir, STATUS_PLANNED, f"recover {inp.action}")
                try:
                    append_recovery_event(rec_dir, STATUS_STABILIZING, f"recover {inp.action}")
                    final_rev = (
                        _execute_accept_target(
                            inp=inp,
                            deploy=deploy,
                            plan=plan,
                            dep_dir=dep_dir,
                            compose_override_dir=rec_dir / OVERRIDES_DIRNAME,
                        )
                        if inp.action == "accept-target"
                        else _execute_rollback(
                            inp=inp,
                            deploy=deploy,
                            plan=plan,
                            compose_override_dir=rec_dir / OVERRIDES_DIRNAME,
                        )
                    )
                    outcome = (
                        RESOLUTION_OUTCOME_ACCEPT_TARGET
                        if inp.action == "accept-target"
                        else RESOLUTION_OUTCOME_ROLLBACK
                    )
                    status = (
                        STATUS_COMMITTED
                        if inp.action == "accept-target"
                        else STATUS_ROLLED_BACK
                    )
                    append_recovery_event(rec_dir, status, f"recover {inp.action} succeeded")
                    write_recovery_result(
                        rec_dir,
                        recovery_id=recovery_id,
                        status=status,
                        messages=(f"OK: recover {inp.action} completed",),
                    )
                    dep_result_hash = sha256_file(result_path(dep_dir))
                    rec_result_hash = sha256_file(rec_dir / "result.json")
                    write_resolution(
                        dep_dir,
                        ResolutionDocument(
                            schema_version=RESOLUTION_SCHEMA_VERSION,
                            deployment_id=plan.deployment_id,
                            deployment_result_sha256=dep_result_hash,
                            recovery_id=recovery_id,
                            recovery_result_sha256=rec_result_hash,
                            final_active_revision=final_rev,
                            outcome=outcome,
                            resolved_at_utc=utc_now(),
                        ),
                    )
                    messages.append(f"OK: recover {inp.action} resolved deployment")
                    messages.append(f"recovery_id={recovery_id}")
                    return RecoverResult(
                        ok=True, messages=tuple(messages), recovery_id=recovery_id
                    )
                except Exception as exc:  # noqa: BLE001
                    detail = redact(str(exc))
                    try:
                        append_recovery_event(rec_dir, STATUS_FAILED, detail)
                        write_recovery_result(
                            rec_dir,
                            recovery_id=recovery_id,
                            status=STATUS_FAILED,
                            messages=(f"FAIL: recover {inp.action}: {detail}",),
                        )
                    except JournalError:
                        pass
                    raise RecoverError(detail) from exc

            phase = latest_event_status(dep_dir)
            if phase != STATUS_STABILIZING:
                try:
                    append_event(dep_dir, STATUS_STABILIZING, f"recover {inp.action}")
                except JournalError:
                    pass
            final_rev = (
                _execute_accept_target(
                    inp=inp,
                    deploy=deploy,
                    plan=plan,
                    dep_dir=dep_dir,
                    compose_override_dir=dep_dir / OVERRIDES_DIRNAME,
                )
                if inp.action == "accept-target"
                else _execute_rollback(
                    inp=inp,
                    deploy=deploy,
                    plan=plan,
                    compose_override_dir=dep_dir / OVERRIDES_DIRNAME,
                )
            )
            status = (
                STATUS_COMMITTED if inp.action == "accept-target" else STATUS_ROLLED_BACK
            )
            append_event(dep_dir, status, f"recover {inp.action} completed")
            write_result(
                dep_dir,
                deployment_id=plan.deployment_id,
                status=status,
                messages=(f"OK: recover {inp.action} completed",),
            )
            messages.append(f"OK: recover {inp.action} completed deployment journal")
            messages.append(f"revision={final_rev}")
            return RecoverResult(ok=True, messages=tuple(messages))
    except (
        RecoverError,
        CurrentStateError,
        CompatibilityError,
        DatabaseDriftError,
    ) as exc:
        return RecoverResult(
            ok=False,
            messages=(f"FAIL: {redact(str(exc))}",),
            recovery_id=recovery_id,
        )
    except Exception as exc:  # noqa: BLE001
        return RecoverResult(
            ok=False,
            messages=(f"FAIL: {redact(str(exc))}",),
            recovery_id=recovery_id,
        )
