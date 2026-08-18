"""Application-only rollout transaction (PRD1C3A)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .activate import (
    ActivateError,
    activate_app_tier,
    activate_gateway,
    run_storage_init,
)
from .bootstrap_cleanup import BootstrapCleanupError, remove_owned_bootstrap_containers
from .c2_constants import SOURCE_DIRNAME
from .application_compatibility import (
    CompatibilityError,
    DatabaseDriftError,
    assert_application_compatible_with_database,
    assert_live_matches_saved_database_heads,
    assert_resolved_application_compatible,
)
from .c3_constants import (
    APP_SERVICES_BEFORE_GATEWAY,
    RUNTIME_APPLICATION_SERVICES,
    OVERRIDES_DIRNAME,
    PLAN_SCHEMA_VERSION,
    STATUS_ACTIVATING_APP,
    STATUS_ACTIVATING_GATEWAY,
    STATUS_APP_HEALTHY,
    STATUS_COMMITTED,
    STATUS_FAILED,
    STATUS_INITIALIZING_STORAGE,
    STATUS_PLANNED,
    STATUS_ROLLBACK_FAILED,
    STATUS_ROLLBACK_STARTED,
    STATUS_ROLLED_BACK,
    STATUS_STABILIZING,
)
from .current_state import (
    ApplicationState,
    CurrentStateError,
    ResolvedCurrentState,
    build_bootstrap_database_state,
    build_committed_current_state,
    load_and_resolve_current_state,
    next_database_state_after_app_commit,
)
from .db_heads import (
    DbHeadsError,
    assert_heads_equal,
    database_heads_via_postgres,
    target_heads_from_release_source,
)
from .deploy_root import release_dir, validate_deploy_root
from .health import HealthInput
from .env_file import load_env_file
from .environment import (
    PRODUCTION,
    STAGING,
    assert_real_environment_bundle,
    real_identity,
    snapshot_compose_files_for_release,
)
from .image_identity import ImageIdentityError, assert_release_images_present
from .journal import (
    JournalError,
    PlanDocument,
    append_event,
    begin_bootstrap_cleanup_event,
    deployment_dir,
    ensure_rollout_layout,
    find_unresolved_deployments,
    new_deployment_id,
    publish_bootstrap_failed_after_cleanup,
    sha256_file,
    utc_now,
    write_current_state,
    write_plan,
    write_result,
)
from .manifest import ManifestError, load_manifest, manifest_path
from .override import (
    OverrideError,
    compose_files_include_delivery,
    compose_files_include_storage_init,
    write_images_override,
)
from .redact import redact
from .revision import RevisionError, validate_full_sha
from .rollout_lock import RolloutLock, RolloutLockError
from .stabilize import (
    StabilizationPolicy,
    StabilizeError,
    stabilize_full_health,
    wait_services_healthy,
)
from .verify_release import ReleaseVerifyError, verify_prepared_release


class RolloutError(RuntimeError):
    """Rollout transaction failure."""


@dataclass(frozen=True)
class RolloutInput:
    project_name: str
    env_file: Path
    expected_revision: str
    repo_root: Path
    deploy_root: Path
    gateway_base_url: str = "http://127.0.0.1:8091"
    bootstrap: bool = False
    wait_lock: bool = False
    wait_lock_seconds: float | None = 30.0
    policy: StabilizationPolicy | None = None


@dataclass(frozen=True)
class RolloutResult:
    ok: bool
    already_active: bool
    messages: tuple[str, ...]
    deployment_id: str | None = None
    status: str | None = None


def _release_compose_files(
    release_directory: Path, *, project_name: str
) -> tuple[Path, ...]:
    manifest = load_manifest(manifest_path(release_directory))
    return snapshot_compose_files_for_release(
        release_directory, project_name=project_name, manifest=manifest
    )


def _compose_with_override(
    release_directory: Path, override_path: Path, *, project_name: str
) -> tuple[Path, ...]:
    return (
        *_release_compose_files(release_directory, project_name=project_name),
        override_path.resolve(),
    )


def _app_containers_present(
    *,
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
    cwd: Path,
) -> bool:
    from .stabilize import _compose_ps

    rows = _compose_ps(
        project_name=project_name,
        env_file=env_file,
        compose_files=compose_files,
        cwd=cwd,
    )
    app = set(RUNTIME_APPLICATION_SERVICES)
    for row in rows:
        svc = str(row.get("Service") or row.get("service") or "")
        state = str(row.get("State") or "").lower()
        if svc in app and state not in {"", "exited", "dead"}:
            # Treat created/running as present.
            if state in {"running", "created", "restarting", "paused"}:
                return True
    return False


def _postgres_healthy(
    *,
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
    cwd: Path,
) -> bool:
    from .stabilize import _compose_ps

    rows = _compose_ps(
        project_name=project_name,
        env_file=env_file,
        compose_files=compose_files,
        cwd=cwd,
    )
    for row in rows:
        svc = str(row.get("Service") or row.get("service") or "")
        if svc != "postgres":
            continue
        state = str(row.get("State") or "").lower()
        health = str(row.get("Health") or "").lower()
        return state == "running" and health in {"healthy", ""}
    return False


def _perform_rollback(
    *,
    inp: RolloutInput,
    dep_dir: Path,
    deployment_id: str,
    previous_revision: str,
    previous_ids: dict[str, str],
    messages: list[str],
    mutation_began: bool,
) -> RolloutResult:
    append_event(dep_dir, STATUS_ROLLBACK_STARTED, "automatic application rollback")
    messages.append("OK: rollback_started")
    try:
        prev_release = release_dir(inp.deploy_root, previous_revision)
        verified = verify_prepared_release(
            prev_release,
            expected_revision=previous_revision,
            repo_root=inp.repo_root,
        )
        if not verified.ok:
            raise RolloutError(
                "previous release failed verification during rollback: "
                + verified.messages[0]
            )
        prev_manifest = load_manifest(manifest_path(prev_release))
        assert_release_images_present(prev_manifest)

        # Authority + compatibility before any rollback container mutation.
        current = load_and_resolve_current_state(
            inp.deploy_root, repo_root=inp.repo_root
        )
        if current is None:
            raise RolloutError(
                "automatic rollback requires resolved current.json with database state"
            )
        base_compose = _release_compose_files(prev_release, project_name=inp.project_name)
        live_heads = database_heads_via_postgres(
            project_name=inp.project_name,
            env_file=inp.env_file,
            compose_files=base_compose,
            cwd=prev_release / SOURCE_DIRNAME,
        )
        assert_live_matches_saved_database_heads(
            live_heads=live_heads,
            saved_heads=current.database.heads,
        )
        prev_heads = target_heads_from_release_source(prev_release)
        assert_application_compatible_with_database(
            target_revision=previous_revision,
            target_source_heads=prev_heads,
            database=current.database,
        )
        messages.append("OK: rollback previous release compatible with database state")

        override = write_images_override(
            dep_dir / OVERRIDES_DIRNAME,
            previous_ids,
            deployment_id=deployment_id,
            release_revision=previous_revision,
            include_delivery=compose_files_include_delivery(
                _release_compose_files(prev_release, project_name=inp.project_name)
            ),
            include_storage_init=compose_files_include_storage_init(
                _release_compose_files(prev_release, project_name=inp.project_name)
            ),
        )
        compose_files = _compose_with_override(prev_release, override, project_name=inp.project_name)
        cwd = prev_release / SOURCE_DIRNAME
        if compose_files_include_storage_init(_release_compose_files(prev_release, project_name=inp.project_name)):
            run_storage_init(
                project_name=inp.project_name,
                env_file=inp.env_file,
                compose_files=compose_files,
                cwd=cwd,
            )
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
            expected_revision=previous_revision,
            repo_root=cwd,
            gateway_base_url=inp.gateway_base_url,
            expected_image_ids=previous_ids,
        )
        stabilize_full_health(health_inp, policy=inp.policy)
        append_event(dep_dir, STATUS_ROLLED_BACK, "previous release restored")
        write_result(
            dep_dir,
            deployment_id=deployment_id,
            status=STATUS_ROLLED_BACK,
            messages=tuple(messages) + ("OK: rolled_back",),
        )
        # current.json must remain on previous — do not rewrite.
        return RolloutResult(
            ok=False,
            already_active=False,
            messages=tuple(messages)
            + (
                "OK: rolled_back to previous release",
                f"previous_revision={previous_revision}",
                "FAIL: requested rollout did not commit",
            ),
            deployment_id=deployment_id,
            status=STATUS_ROLLED_BACK,
        )
    except Exception as exc:  # noqa: BLE001 — convert to rollback_failed
        detail = redact(str(exc))
        messages.append(f"FAIL: rollback failed: {detail}")
        try:
            append_event(dep_dir, STATUS_ROLLBACK_FAILED, detail)
            write_result(
                dep_dir,
                deployment_id=deployment_id,
                status=STATUS_ROLLBACK_FAILED,
                messages=tuple(messages),
            )
        except Exception:  # noqa: BLE001
            pass
        return RolloutResult(
            ok=False,
            already_active=False,
            messages=tuple(messages)
            + (
                "FAIL: rollback_failed — run: "
                f"fetchnow_release recover --deployment-id {deployment_id} "
                "--action rollback|accept-target",
            ),
            deployment_id=deployment_id,
            status=STATUS_ROLLBACK_FAILED,
        )


def _verify_already_active(
    inp: RolloutInput,
    deploy: Path,
    rev: str,
    current: ResolvedCurrentState,
) -> RolloutResult:
    """Idempotent path: dual-state drift + compatibility + health."""
    target_release = release_dir(deploy, rev)
    man_path = manifest_path(target_release)
    man_hash = sha256_file(man_path)
    if man_hash != current.release_manifest_sha256:
        raise RolloutError(
            "current.json manifest hash does not match prepared release.json"
        )
    verified = verify_prepared_release(
        target_release, expected_revision=rev, repo_root=inp.repo_root
    )
    if not verified.ok:
        raise RolloutError(verified.messages[0])
    base_compose = _release_compose_files(target_release, project_name=inp.project_name)
    cwd = target_release / SOURCE_DIRNAME
    live_heads = database_heads_via_postgres(
        project_name=inp.project_name,
        env_file=inp.env_file,
        compose_files=base_compose,
        cwd=cwd,
    )
    assert_live_matches_saved_database_heads(
        live_heads=live_heads,
        saved_heads=current.database.heads,
    )
    app_heads = target_heads_from_release_source(target_release)
    assert_resolved_application_compatible(
        current=current,
        target_revision=rev,
        target_source_heads=app_heads,
        live_heads=live_heads,
    )
    override_ids = current.image_ids
    health_dep = deployment_dir(deploy, current.deployment_id)
    override = health_dep / OVERRIDES_DIRNAME / "images.yaml"
    if not override.is_file():
        write_images_override(
            health_dep / OVERRIDES_DIRNAME,
            override_ids,
            include_delivery=compose_files_include_delivery(
                _release_compose_files(target_release, project_name=inp.project_name)
            ),
            include_storage_init=compose_files_include_storage_init(
                _release_compose_files(target_release, project_name=inp.project_name)
            ),
        )
    compose_files = _compose_with_override(target_release, override, project_name=inp.project_name)
    health_inp = HealthInput(
        project_name=inp.project_name,
        env_file=inp.env_file,
        compose_files=compose_files,
        expected_revision=rev,
        repo_root=cwd,
        gateway_base_url=inp.gateway_base_url,
        expected_image_ids=override_ids,
    )
    result = stabilize_full_health(health_inp, policy=inp.policy)
    if not result.ok:
        raise RolloutError(result.messages[0])
    return RolloutResult(
        ok=True,
        already_active=True,
        messages=(
            "OK: release already active",
            f"revision={rev}",
            "OK: live database heads match saved state",
            "OK: application compatible with database state",
            "OK: health verified (no recreate)",
        ),
        deployment_id=current.deployment_id,
        status=STATUS_COMMITTED,
    )


def rollout_release(inp: RolloutInput) -> RolloutResult:
    messages: list[str] = []
    deployment_id: str | None = None
    dep_dir: Path | None = None
    mutation_began = False
    try:
        if real_identity(inp.project_name) is None:
            from .deploy_plan_project import (
                DeployPlanProjectError,
                assert_deploy_plan_project,
            )
            from .migration_project import MigrationProjectError, assert_migration_project
            from .rollout_project import RolloutProjectError, assert_rollout_project

            for checker in (
                assert_rollout_project,
                assert_deploy_plan_project,
                assert_migration_project,
            ):
                try:
                    checker(inp.project_name)
                    break
                except (
                    RolloutProjectError,
                    DeployPlanProjectError,
                    MigrationProjectError,
                ):
                    continue
            else:
                raise RolloutError(
                    f"project name must be {STAGING.project!r}, "
                    f"{PRODUCTION.project!r}, or a validated "
                    f"fetchnow-rollout-test-* / fetchnow-deploy-plan-test-* / "
                    f"fetchnow-migration-test-* name, got {inp.project_name!r}"
                )
        else:
            env = load_env_file(inp.env_file)
            assert_real_environment_bundle(
                project_name=inp.project_name,
                env=env,
                env_file=inp.env_file,
                deploy_root=inp.deploy_root,
                require_cli_backup_root=False,
                require_deploy_root=True,
            )

        rev = validate_full_sha(inp.expected_revision)
        deploy = validate_deploy_root(inp.deploy_root, repo_root=inp.repo_root)
        ensure_rollout_layout(deploy)

        target_release = release_dir(deploy, rev)
        verified = verify_prepared_release(
            target_release, expected_revision=rev, repo_root=inp.repo_root
        )
        if not verified.ok:
            raise RolloutError(verified.messages[0])
        messages.append("OK: target release verified")

        current = load_and_resolve_current_state(deploy, repo_root=inp.repo_root)
        if current is None and not inp.bootstrap:
            raise RolloutError(
                "no current.json — pass --bootstrap for first application rollout"
            )

        if current is not None:
            prev_release = release_dir(deploy, current.revision)
            prev_ok = verify_prepared_release(
                prev_release,
                expected_revision=current.revision,
                repo_root=inp.repo_root,
            )
            if not prev_ok.ok:
                raise RolloutError(
                    "current release failed verification: " + prev_ok.messages[0]
                )
            messages.append("OK: current release verified")

        with RolloutLock(
            deploy, wait=inp.wait_lock, wait_timeout_seconds=inp.wait_lock_seconds
        ):
            unresolved = find_unresolved_deployments(deploy)
            if unresolved:
                raise RolloutError(
                    "unresolved deployment journal(s) block new rollout: "
                    + ", ".join(unresolved)
                    + "; use recover --deployment-id …"
                )
            from .migration_journal import find_unresolved_migrations

            unresolved_migrations = find_unresolved_migrations(deploy)
            if unresolved_migrations:
                raise RolloutError(
                    "unresolved migration journal(s) block application rollout: "
                    + ", ".join(unresolved_migrations)
                    + "; use recover-migration …"
                )

            # Re-check current under lock for races.
            current = load_and_resolve_current_state(deploy, repo_root=inp.repo_root)
            if current is not None and current.revision == rev:
                return _verify_already_active(inp, deploy, rev, current)

            target_manifest = load_manifest(manifest_path(target_release))
            target_ids = assert_release_images_present(target_manifest)
            man_hash = sha256_file(manifest_path(target_release))

            base_compose = _release_compose_files(target_release, project_name=inp.project_name)
            cwd = target_release / SOURCE_DIRNAME

            if inp.bootstrap:
                if current is not None:
                    raise RolloutError("--bootstrap refused: current.json already exists")
                if _app_containers_present(
                    project_name=inp.project_name,
                    env_file=inp.env_file,
                    compose_files=base_compose,
                    cwd=cwd,
                ):
                    raise RolloutError(
                        "bootstrap refused: application containers already exist"
                    )
                if not _postgres_healthy(
                    project_name=inp.project_name,
                    env_file=inp.env_file,
                    compose_files=base_compose,
                    cwd=cwd,
                ):
                    raise RolloutError("bootstrap requires healthy postgres")

            target_heads = target_heads_from_release_source(target_release)
            db_heads = database_heads_via_postgres(
                project_name=inp.project_name,
                env_file=inp.env_file,
                compose_files=base_compose,
                cwd=cwd,
            )
            if inp.bootstrap or current is None:
                assert_heads_equal(database_heads=db_heads, target_heads=target_heads)
                messages.append(
                    f"OK: bootstrap database heads equal target {sorted(target_heads)}"
                )
            else:
                assert_live_matches_saved_database_heads(
                    live_heads=db_heads,
                    saved_heads=current.database.heads,
                )
                decision = assert_application_compatible_with_database(
                    target_revision=rev,
                    target_source_heads=target_heads,
                    database=current.database,
                )
                messages.append("OK: live database heads match saved state")
                messages.append(f"OK: application compatibility ({decision.reason})")

            previous_ids = None if current is None else dict(current.image_ids)
            if current is not None:
                assert_release_images_present(
                    load_manifest(
                        manifest_path(release_dir(deploy, current.revision))
                    )
                )

            deployment_id = new_deployment_id()
            dep_dir = deployment_dir(deploy, deployment_id)
            plan = PlanDocument(
                schema_version=PLAN_SCHEMA_VERSION,
                deployment_id=deployment_id,
                target_revision=rev,
                previous_revision=None if current is None else current.revision,
                bootstrap=bool(inp.bootstrap and current is None),
                target_image_ids=target_ids,
                previous_image_ids=previous_ids,
                release_manifest_sha256=man_hash,
                database_heads=tuple(sorted(db_heads)),
                created_at_utc=utc_now(),
                project_name=inp.project_name,
            )
            write_plan(dep_dir, plan)
            append_event(dep_dir, STATUS_PLANNED, "plan published")
            override = write_images_override(
                dep_dir / OVERRIDES_DIRNAME,
                target_ids,
                deployment_id=deployment_id,
                release_revision=rev,
                include_delivery=compose_files_include_delivery(
                    _release_compose_files(target_release, project_name=inp.project_name)
                ),
                include_storage_init=compose_files_include_storage_init(
                    _release_compose_files(target_release, project_name=inp.project_name)
                ),
            )
            compose_files = _compose_with_override(target_release, override, project_name=inp.project_name)
            messages.append(f"OK: plan published deployment_id={deployment_id}")

            try:
                if compose_files_include_storage_init(
                    _release_compose_files(target_release, project_name=inp.project_name)
                ):
                    append_event(
                        dep_dir,
                        STATUS_INITIALIZING_STORAGE,
                        "storage initializer",
                    )
                    run_storage_init(
                        project_name=inp.project_name,
                        env_file=inp.env_file,
                        compose_files=compose_files,
                        cwd=cwd,
                    )
                    messages.append("OK: storage-init completed")
                append_event(
                    dep_dir,
                    STATUS_ACTIVATING_APP,
                    "activate api/worker/delivery/web",
                )
                mutation_began = True
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
                append_event(dep_dir, STATUS_APP_HEALTHY, "app tier healthy")
                append_event(
                    dep_dir, STATUS_ACTIVATING_GATEWAY, "activate gateway"
                )
                activate_gateway(
                    project_name=inp.project_name,
                    env_file=inp.env_file,
                    compose_files=compose_files,
                    cwd=cwd,
                )
                append_event(dep_dir, STATUS_STABILIZING, "full health gate")
                health_inp = HealthInput(
                    project_name=inp.project_name,
                    env_file=inp.env_file,
                    compose_files=compose_files,
                    expected_revision=rev,
                    repo_root=cwd,
                    gateway_base_url=inp.gateway_base_url,
                    expected_image_ids=target_ids,
                )
                stabilize_full_health(health_inp, policy=inp.policy)

                # Atomic current.json (schema v2) only after stabilized success.
                application = ApplicationState(
                    revision=rev,
                    release_manifest_sha256=man_hash,
                    image_ids=dict(target_ids),
                    deployment_id=deployment_id,
                )
                if current is None:
                    database = build_bootstrap_database_state(
                        heads=db_heads,
                        schema_release_revision=rev,
                    )
                else:
                    database = next_database_state_after_app_commit(
                        previous=current.database,
                        target_revision=rev,
                        target_source_heads=target_heads,
                    )
                write_current_state(
                    deploy,
                    build_committed_current_state(
                        application=application,
                        database=database,
                        updated_at_utc=utc_now(),
                    ),
                )
                append_event(dep_dir, STATUS_COMMITTED, "current.json published")
                write_result(
                    dep_dir,
                    deployment_id=deployment_id,
                    status=STATUS_COMMITTED,
                    messages=tuple(messages) + ("OK: rollout committed",),
                )
                messages.append("OK: rollout committed")
                messages.append(f"revision={rev}")
                return RolloutResult(
                    ok=True,
                    already_active=False,
                    messages=tuple(messages),
                    deployment_id=deployment_id,
                    status=STATUS_COMMITTED,
                )
            except Exception as exc:  # noqa: BLE001
                detail = redact(str(exc))
                messages.append(f"FAIL: target rollout error: {detail}")
                if inp.bootstrap and mutation_began:
                    from .bootstrap_cleanup import discover_owned_bootstrap_containers

                    try:
                        begin_bootstrap_cleanup_event(dep_dir)
                        owned = discover_owned_bootstrap_containers(
                            project_name=inp.project_name,
                            env_file=inp.env_file,
                            compose_files=compose_files,
                            cwd=cwd,
                            deployment_id=deployment_id,
                            release_revision=rev,
                        )
                        removed = remove_owned_bootstrap_containers(
                            project_name=inp.project_name,
                            env_file=inp.env_file,
                            compose_files=compose_files,
                            cwd=cwd,
                            deployment_id=deployment_id,
                            release_revision=rev,
                            expected_container_ids=[
                                item.container_id for item in owned
                            ],
                        )
                        if not removed:
                            raise BootstrapCleanupError(
                                "bootstrap cleanup found no owned containers to remove"
                            )
                        audit_payload = [
                            {
                                "service": item.service,
                                "container_id": item.container_id,
                                "compose_project": item.labels.get(
                                    "com.docker.compose.project"
                                ),
                                "compose_service": item.labels.get(
                                    "com.docker.compose.service"
                                ),
                                "deployment_id": item.labels.get(
                                    "com.fetchnow.deployment-id"
                                ),
                                "release_revision": item.labels.get(
                                    "com.fetchnow.release-revision"
                                ),
                            }
                            for item in removed
                        ]
                        messages.append(
                            "OK: bootstrap cleanup audit="
                            + json.dumps(audit_payload, sort_keys=True)
                        )
                        publish_bootstrap_failed_after_cleanup(
                            dep_dir,
                            plan=plan,
                            deployment_id=deployment_id,
                            messages=tuple(messages),
                            detail=detail,
                        )
                    except BootstrapCleanupError as cleanup_exc:
                        messages.append(
                            "FAIL: bootstrap cleanup: " + redact(str(cleanup_exc))
                        )
                        return RolloutResult(
                            ok=False,
                            already_active=False,
                            messages=tuple(messages)
                            + (
                                "FAIL: bootstrap cleanup left unresolved journal "
                                "(no result.json)",
                            ),
                            deployment_id=deployment_id,
                            status=None,
                        )
                    return RolloutResult(
                        ok=False,
                        already_active=False,
                        messages=tuple(messages),
                        deployment_id=deployment_id,
                        status=STATUS_FAILED,
                    )
                if mutation_began and plan.previous_revision and previous_ids:
                    return _perform_rollback(
                        inp=inp,
                        dep_dir=dep_dir,
                        deployment_id=deployment_id,
                        previous_revision=plan.previous_revision,
                        previous_ids=previous_ids,
                        messages=messages,
                        mutation_began=mutation_began,
                    )
                if mutation_began:
                    raise RolloutError(
                        "post-mutation failure must enter rollback or bootstrap "
                        "cleanup; refusing to publish terminal failed"
                    )
                append_event(dep_dir, STATUS_FAILED, detail)
                write_result(
                    dep_dir,
                    deployment_id=deployment_id,
                    status=STATUS_FAILED,
                    messages=tuple(messages),
                )
                return RolloutResult(
                    ok=False,
                    already_active=False,
                    messages=tuple(messages),
                    deployment_id=deployment_id,
                    status=STATUS_FAILED,
                )
    except (
        RolloutError,
        JournalError,
        ManifestError,
        OverrideError,
        DbHeadsError,
        ImageIdentityError,
        ActivateError,
        BootstrapCleanupError,
        StabilizeError,
        RolloutLockError,
        ReleaseVerifyError,
        RevisionError,
        CurrentStateError,
        CompatibilityError,
        DatabaseDriftError,
        OSError,
        ValueError,
    ) as exc:
        return RolloutResult(
            ok=False,
            already_active=False,
            messages=(f"FAIL: {redact(str(exc))}",),
            deployment_id=deployment_id,
            status=None,
        )
