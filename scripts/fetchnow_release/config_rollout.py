"""Transactional config-only rollout for an already-active immutable release."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .activate import ActivateError, activate_services
from .application_compatibility import (
    CompatibilityError,
    DatabaseDriftError,
    assert_live_matches_saved_database_heads,
    assert_resolved_application_compatible,
)
from .bootstrap_journal import find_unresolved_bootstraps
from .c2_constants import SOURCE_DIRNAME
from .c3_constants import OVERRIDES_DIRNAME, RUNTIME_APPLICATION_SERVICES
from .config_contract import (
    BUILD_TIME_CONFIG,
    RUNTIME_CONFIG_ALLOWLIST,
    ConfigContractError,
    affected_services,
    build_config_fingerprint,
    build_values_from_env,
    changed_runtime_keys,
    runtime_config_fingerprint,
    runtime_values_from_compose,
    service_environment,
)
from .config_journal import (
    STATUS_ACTIVATING,
    STATUS_COMMITTED,
    STATUS_FAILED,
    STATUS_PLANNED,
    STATUS_ROLLBACK_FAILED,
    STATUS_ROLLBACK_STARTED,
    STATUS_ROLLED_BACK,
    STATUS_STABILIZING,
    ConfigJournalError,
    ConfigPlan,
    append_config_event,
    config_rollout_dir,
    find_unresolved_config_rollouts,
    new_config_rollout_id,
    write_config_plan,
    write_config_result,
)
from .config_state import (
    RUNTIME_CONFIG_STATE_SCHEMA,
    ConfigStateError,
    RuntimeConfigState,
    build_runtime_config_state,
    load_runtime_config_state,
    write_runtime_config_state,
)
from .current_state import CurrentStateError, load_and_resolve_current_state
from .db_heads import (
    DbHeadsError,
    database_heads_via_postgres,
    target_heads_from_release_source,
)
from .deploy_root import release_dir, validate_deploy_root
from .docker_checks import DockerCheckError, compose_config_json
from .env_file import EnvFileError, load_env_file
from .environment import (
    assert_real_environment_bundle,
    real_identity,
    snapshot_compose_files_for_release,
)
from .health import HealthInput
from .image_identity import ImageIdentityError, assert_release_images_present
from .journal import find_unresolved_deployments, sha256_file, utc_now
from .journal_io import atomic_write_text
from .manifest import ManifestError, load_manifest, manifest_path
from .migration_journal import find_unresolved_migrations
from .override import (
    OverrideError,
    compose_files_include_delivery,
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


class ConfigRolloutError(RuntimeError):
    """Config-only rollout gate or transaction failure."""


@dataclass(frozen=True)
class ConfigRolloutInput:
    project_name: str
    env_file: Path
    expected_revision: str
    repo_root: Path
    deploy_root: Path
    gateway_base_url: str = "http://127.0.0.1:8091"
    wait_lock: bool = False
    wait_lock_seconds: float | None = 30.0
    policy: StabilizationPolicy | None = None
    initialize_active_config: bool = False


@dataclass(frozen=True)
class ConfigRolloutResult:
    ok: bool
    already_active_config: bool
    messages: tuple[str, ...]
    config_rollout_id: str | None = None
    status: str | None = None


def _compose_argv(
    *,
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
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
    return argv


def _container_id(
    *,
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
    cwd: Path,
    service: str,
) -> str:
    proc = subprocess.run(
        _compose_argv(
            project_name=project_name,
            env_file=env_file,
            compose_files=compose_files,
        )
        + ["ps", "-q", service],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    container_id = proc.stdout.strip()
    if proc.returncode != 0 or not re.fullmatch(r"[0-9a-f]{12,64}", container_id):
        raise ConfigRolloutError(f"cannot resolve live container for {service}")
    return container_id


def _inspect_container_env(container_id: str) -> dict[str, str]:
    proc = subprocess.run(
        ["docker", "inspect", container_id, "--format", "{{json .Config.Env}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise ConfigRolloutError("docker inspect failed for live service config")
    try:
        raw = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ConfigRolloutError(
            "live service config inspect returned invalid JSON"
        ) from exc
    if not isinstance(raw, list):
        raise ConfigRolloutError("live service config environment is malformed")
    out: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, str) or "=" not in item:
            raise ConfigRolloutError("live service config environment is malformed")
        key, value = item.split("=", 1)
        if key in out:
            raise ConfigRolloutError("live service has duplicate environment key")
        out[key] = value
    return out


def inspect_live_service_state(
    *,
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
    cwd: Path,
) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    ids: dict[str, str] = {}
    environments: dict[str, dict[str, str]] = {}
    for service in RUNTIME_APPLICATION_SERVICES:
        container_id = _container_id(
            project_name=project_name,
            env_file=env_file,
            compose_files=compose_files,
            cwd=cwd,
            service=service,
        )
        ids[service] = container_id
        environments[service] = _inspect_container_env(container_id)
    return ids, environments


def _active_runtime_values(live_env: dict[str, dict[str, str]]) -> dict[str, str]:
    values: dict[str, str] = {}
    for key, spec in RUNTIME_CONFIG_ALLOWLIST.items():
        observed: set[str] = set()
        for service in spec.services:
            value = live_env.get(service, {}).get(key)
            if value is None:
                raise ConfigRolloutError(
                    f"live service {service} is missing allowlisted key {key}"
                )
            observed.add(value)
        if len(observed) != 1:
            raise ConfigRolloutError(f"live allowlisted config diverges for {key}")
        values[key] = observed.pop()
    # Normalization and validation happen in the fingerprint function.
    runtime_config_fingerprint(values)
    return values


def _environment_deltas(
    *,
    rendered: dict,
    live_env: dict[str, dict[str, str]],
) -> dict[str, tuple[str, ...]]:
    deltas: dict[str, tuple[str, ...]] = {}
    for service in RUNTIME_APPLICATION_SERVICES:
        target = service_environment(rendered, service)
        live = live_env.get(service)
        if live is None:
            raise ConfigRolloutError(f"live config missing service {service}")
        changed = tuple(key for key in sorted(target) if live.get(key) != target[key])
        if changed:
            deltas[service] = changed
    return deltas


def _assert_only_allowlisted_deltas(
    deltas: dict[str, tuple[str, ...]], changed_keys: tuple[str, ...]
) -> None:
    expected = {
        (service, key)
        for key in changed_keys
        for service in RUNTIME_CONFIG_ALLOWLIST[key].services
    }
    observed = {(service, key) for service, keys in deltas.items() for key in keys}
    unsafe = sorted(observed - expected)
    missing = sorted(expected - observed)
    if unsafe:
        summary = ", ".join(f"{service}:{key}" for service, key in unsafe)
        raise ConfigRolloutError(
            "config-only rollout rejects non-allowlisted runtime delta(s): " + summary
        )
    if missing:
        raise ConfigRolloutError("allowlisted runtime delta receiver mismatch")


def _health_input(
    *,
    inp: ConfigRolloutInput,
    env_file: Path,
    compose_files: tuple[Path, ...],
    cwd: Path,
    image_ids: dict[str, str],
) -> HealthInput:
    return HealthInput(
        project_name=inp.project_name,
        env_file=env_file,
        compose_files=compose_files,
        expected_revision=inp.expected_revision,
        repo_root=cwd,
        gateway_base_url=inp.gateway_base_url,
        expected_image_ids=image_ids,
    )


def _rollback_env_file(
    *, env_file: Path, deploy_root: Path, rollout_id: str, previous: dict[str, str]
) -> Path:
    # Preserve all unrelated bytes and replace only reviewed non-secret keys.
    text = env_file.read_text(encoding="utf-8")
    for key, value in previous.items():
        pattern = re.compile(rf"(?m)^{re.escape(key)}=.*$")
        matches = pattern.findall(text)
        if len(matches) > 1:
            raise ConfigRolloutError(f"duplicate env key blocks rollback: {key}")
        replacement = f"{key}={value}"
        if matches:
            text = pattern.sub(replacement, text, count=1)
        else:
            if text and not text.endswith("\n"):
                text += "\n"
            text += replacement + "\n"
    path = deploy_root / f".config-rollback-{rollout_id}.env"
    atomic_write_text(path, text, mode=0o600)
    return path


def _assert_container_recreation(
    *,
    before: dict[str, str],
    after: dict[str, str],
    affected: tuple[str, ...],
) -> None:
    for service in RUNTIME_APPLICATION_SERVICES:
        if service in affected:
            if before.get(service) == after.get(service):
                raise ConfigRolloutError(
                    f"affected service {service} was not recreated"
                )
        elif before.get(service) != after.get(service):
            raise ConfigRolloutError(
                f"unaffected service {service} was unexpectedly recreated"
            )


def _initialize_state(
    *,
    inp: ConfigRolloutInput,
    current,
    active_runtime: dict[str, str],
    build_values: dict[str, str],
) -> RuntimeConfigState:
    state = build_runtime_config_state(
        revision=inp.expected_revision,
        deployment_id=current.deployment_id,
        latest_config_rollout_id=None,
        runtime_values=active_runtime,
        build_values=build_values,
        updated_at_utc=utc_now(),
    )
    write_runtime_config_state(inp.deploy_root, state)
    return state


def _assert_release_state_unchanged(
    *,
    inp: ConfigRolloutInput,
    deploy: Path,
    compose_files: tuple[Path, ...],
    cwd: Path,
    expected_heads: frozenset[str],
    env_file: Path | None = None,
) -> None:
    current = load_and_resolve_current_state(deploy, repo_root=inp.repo_root)
    if current is None or current.revision != inp.expected_revision:
        raise ConfigRolloutError("current revision changed during config rollout")
    live_heads = database_heads_via_postgres(
        project_name=inp.project_name,
        env_file=env_file or inp.env_file,
        compose_files=compose_files,
        cwd=cwd,
    )
    if frozenset(live_heads) != expected_heads:
        raise ConfigRolloutError("database heads changed during config rollout")


def run_config_rollout(inp: ConfigRolloutInput) -> ConfigRolloutResult:
    rollout_id: str | None = None
    directory: Path | None = None
    try:
        revision = validate_full_sha(inp.expected_revision)
        deploy = validate_deploy_root(inp.deploy_root, repo_root=inp.repo_root)
        env = load_env_file(inp.env_file)
        if real_identity(inp.project_name) is not None:
            assert_real_environment_bundle(
                project_name=inp.project_name,
                env=env,
                env_file=inp.env_file,
                deploy_root=deploy,
                require_cli_backup_root=False,
                require_deploy_root=True,
            )
        current = load_and_resolve_current_state(deploy, repo_root=inp.repo_root)
        if current is None:
            raise ConfigRolloutError("config rollout requires current.json")
        if current.revision != revision:
            raise ConfigRolloutError("active revision does not match EXPECTED_REVISION")

        release = release_dir(deploy, revision)
        verified = verify_prepared_release(
            release, expected_revision=revision, repo_root=inp.repo_root
        )
        if not verified.ok:
            raise ConfigRolloutError(verified.messages[0])
        manifest = load_manifest(manifest_path(release))
        if sha256_file(manifest_path(release)) != current.release_manifest_sha256:
            raise ConfigRolloutError("current.json manifest hash mismatch")
        image_ids = assert_release_images_present(manifest)
        if image_ids != current.image_ids:
            raise ConfigRolloutError("accepted image identity mismatch")
        base_compose = snapshot_compose_files_for_release(
            release, project_name=inp.project_name, manifest=manifest
        )
        cwd = release / SOURCE_DIRNAME
        live_heads = database_heads_via_postgres(
            project_name=inp.project_name,
            env_file=inp.env_file,
            compose_files=base_compose,
            cwd=cwd,
        )
        assert_live_matches_saved_database_heads(
            live_heads=live_heads, saved_heads=current.database.heads
        )
        assert_resolved_application_compatible(
            current=current,
            target_revision=revision,
            target_source_heads=target_heads_from_release_source(release),
            live_heads=live_heads,
        )

        rendered = compose_config_json(
            project_name=inp.project_name,
            env_file=inp.env_file,
            compose_files=base_compose,
            repo_root=cwd,
        )
        target_runtime = runtime_values_from_compose(rendered)
        target_build = build_values_from_env(env)

        with RolloutLock(
            deploy,
            wait=inp.wait_lock,
            wait_timeout_seconds=inp.wait_lock_seconds,
        ):
            blockers = (
                tuple(find_unresolved_deployments(deploy))
                + tuple(find_unresolved_migrations(deploy))
                + tuple(find_unresolved_bootstraps(deploy))
                + tuple(find_unresolved_config_rollouts(deploy))
            )
            if blockers:
                raise ConfigRolloutError(
                    "unresolved release journal(s) block config rollout: "
                    + ", ".join(blockers)
                )

            # Re-read both authorities while holding the shared rollout lock.
            current = load_and_resolve_current_state(deploy, repo_root=inp.repo_root)
            if current is None or current.revision != revision:
                raise ConfigRolloutError("active revision changed under config lock")
            before_ids, live_env = inspect_live_service_state(
                project_name=inp.project_name,
                env_file=inp.env_file,
                compose_files=base_compose,
                cwd=cwd,
            )
            active_runtime = _active_runtime_values(live_env)
            changed = changed_runtime_keys(active_runtime, target_runtime)
            deltas = _environment_deltas(rendered=rendered, live_env=live_env)
            _assert_only_allowlisted_deltas(deltas, changed)

            state = load_runtime_config_state(deploy)
            if state is None or (
                state.revision != revision
                or state.deployment_id != current.deployment_id
                or state.schema_version != RUNTIME_CONFIG_STATE_SCHEMA
            ):
                if not inp.initialize_active_config:
                    raise ConfigRolloutError(
                        "runtime config state is not initialized for the active "
                        "deployment; rerun with --initialize-active-config before "
                        "editing the production env"
                    )
                if changed:
                    raise ConfigRolloutError(
                        "refusing active-config initialization after runtime env edit"
                    )
                stabilize_full_health(
                    _health_input(
                        inp=inp,
                        env_file=inp.env_file,
                        compose_files=base_compose,
                        cwd=cwd,
                        image_ids=image_ids,
                    ),
                    policy=inp.policy,
                )
                _initialize_state(
                    inp=inp,
                    current=current,
                    active_runtime=active_runtime,
                    build_values=target_build,
                )
                return ConfigRolloutResult(
                    ok=True,
                    already_active_config=True,
                    messages=(
                        "OK: active runtime config initialized",
                        "OK: already-active-config (no recreate)",
                        f"revision={revision}",
                    ),
                    status=STATUS_COMMITTED,
                )

            assert state is not None
            if state.runtime_config_fingerprint != runtime_config_fingerprint(
                active_runtime
            ):
                raise ConfigRolloutError(
                    "live runtime config drift from persisted state"
                )
            if state.build_config_fingerprint != build_config_fingerprint(target_build):
                changed_build = sorted(
                    key
                    for key in BUILD_TIME_CONFIG
                    if state.build_values.get(key) != target_build.get(key)
                )
                raise ConfigRolloutError(
                    "config-only rollout rejects build-time delta(s): "
                    + ", ".join(changed_build)
                )
            if not changed:
                stabilize_full_health(
                    _health_input(
                        inp=inp,
                        env_file=inp.env_file,
                        compose_files=base_compose,
                        cwd=cwd,
                        image_ids=image_ids,
                    ),
                    policy=inp.policy,
                )
                return ConfigRolloutResult(
                    ok=True,
                    already_active_config=True,
                    messages=(
                        "OK: already-active-config (no recreate)",
                        f"runtime_config_fingerprint={state.runtime_config_fingerprint}",
                        f"revision={revision}",
                    ),
                    status=STATUS_COMMITTED,
                )

            affected = affected_services(changed)
            rollout_id = new_config_rollout_id()
            directory = config_rollout_dir(deploy, rollout_id)
            plan = ConfigPlan(
                config_rollout_id=rollout_id,
                expected_revision=revision,
                previous_runtime_config_fingerprint=state.runtime_config_fingerprint,
                target_runtime_config_fingerprint=runtime_config_fingerprint(
                    target_runtime
                ),
                changed_keys=changed,
                affected_services=affected,
                previous_deployment_id=current.deployment_id,
                previous_config_rollout_id=state.latest_config_rollout_id,
                created_at_utc=utc_now(),
            )
            write_config_plan(directory, plan)
            append_config_event(directory, STATUS_PLANNED, "plan published")
            override = write_images_override(
                directory / OVERRIDES_DIRNAME,
                current.image_ids,
                deployment_id=current.deployment_id,
                release_revision=revision,
                include_delivery=compose_files_include_delivery(base_compose),
            )
            compose_files = (*base_compose, override)
            rollback_env: Path | None = None

            try:
                append_config_event(
                    directory, STATUS_ACTIVATING, "recreate affected services"
                )
                activate_services(
                    project_name=inp.project_name,
                    env_file=inp.env_file,
                    compose_files=compose_files,
                    cwd=cwd,
                    services=affected,
                )
                wait_services_healthy(
                    project_name=inp.project_name,
                    env_file=inp.env_file,
                    compose_files=compose_files,
                    cwd=cwd,
                    services=affected,
                    policy=inp.policy or StabilizationPolicy(),
                )
                after_ids, after_env = inspect_live_service_state(
                    project_name=inp.project_name,
                    env_file=inp.env_file,
                    compose_files=compose_files,
                    cwd=cwd,
                )
                _assert_container_recreation(
                    before=before_ids, after=after_ids, affected=affected
                )
                if runtime_config_fingerprint(
                    _active_runtime_values(after_env)
                ) != runtime_config_fingerprint(target_runtime):
                    raise ConfigRolloutError("target runtime config was not applied")
                append_config_event(directory, STATUS_STABILIZING, "global health")
                stabilize_full_health(
                    _health_input(
                        inp=inp,
                        env_file=inp.env_file,
                        compose_files=compose_files,
                        cwd=cwd,
                        image_ids=image_ids,
                    ),
                    policy=inp.policy,
                )
                _assert_release_state_unchanged(
                    inp=inp,
                    deploy=deploy,
                    compose_files=compose_files,
                    cwd=cwd,
                    expected_heads=frozenset(live_heads),
                )
            except Exception as target_exc:  # noqa: BLE001
                append_config_event(
                    directory,
                    STATUS_ROLLBACK_STARTED,
                    "target activation failed; restoring previous config",
                )
                try:
                    rollback_env = _rollback_env_file(
                        env_file=inp.env_file,
                        deploy_root=deploy,
                        rollout_id=rollout_id,
                        previous=state.runtime_values,
                    )
                    activate_services(
                        project_name=inp.project_name,
                        env_file=rollback_env,
                        compose_files=compose_files,
                        cwd=cwd,
                        services=affected,
                    )
                    wait_services_healthy(
                        project_name=inp.project_name,
                        env_file=rollback_env,
                        compose_files=compose_files,
                        cwd=cwd,
                        services=affected,
                        policy=inp.policy or StabilizationPolicy(),
                    )
                    _rollback_ids, rollback_live_env = inspect_live_service_state(
                        project_name=inp.project_name,
                        env_file=rollback_env,
                        compose_files=compose_files,
                        cwd=cwd,
                    )
                    if (
                        runtime_config_fingerprint(
                            _active_runtime_values(rollback_live_env)
                        )
                        != state.runtime_config_fingerprint
                    ):
                        raise ConfigRolloutError("rollback runtime config mismatch")
                    stabilize_full_health(
                        _health_input(
                            inp=inp,
                            env_file=rollback_env,
                            compose_files=compose_files,
                            cwd=cwd,
                            image_ids=image_ids,
                        ),
                        policy=inp.policy,
                    )
                    _assert_release_state_unchanged(
                        inp=inp,
                        deploy=deploy,
                        compose_files=compose_files,
                        cwd=cwd,
                        expected_heads=frozenset(live_heads),
                        env_file=rollback_env,
                    )
                    append_config_event(
                        directory, STATUS_ROLLED_BACK, "previous config healthy"
                    )
                    write_config_result(
                        directory,
                        rollout_id=rollout_id,
                        status=STATUS_ROLLED_BACK,
                        health_result="failed",
                        rollback_status="passed",
                    )
                    return ConfigRolloutResult(
                        ok=False,
                        already_active_config=False,
                        messages=(
                            "OK: previous runtime config restored",
                            "FAIL: requested config rollout did not commit",
                            f"failure_class={type(target_exc).__name__}",
                        ),
                        config_rollout_id=rollout_id,
                        status=STATUS_ROLLED_BACK,
                    )
                except Exception as rollback_exc:  # noqa: BLE001
                    append_config_event(
                        directory,
                        STATUS_ROLLBACK_FAILED,
                        "automatic config rollback failed",
                    )
                    write_config_result(
                        directory,
                        rollout_id=rollout_id,
                        status=STATUS_ROLLBACK_FAILED,
                        health_result="failed",
                        rollback_status="failed",
                    )
                    return ConfigRolloutResult(
                        ok=False,
                        already_active_config=False,
                        messages=(
                            "FAIL: config rollout rollback_failed",
                            f"target_failure_class={type(target_exc).__name__}",
                            f"rollback_failure_class={type(rollback_exc).__name__}",
                        ),
                        config_rollout_id=rollout_id,
                        status=STATUS_ROLLBACK_FAILED,
                    )
                finally:
                    if rollback_env is not None:
                        try:
                            rollback_env.unlink(missing_ok=True)
                        except OSError:
                            pass

            if directory is None or rollout_id is None:
                raise ConfigRolloutError("config journal identity missing at commit")
            append_config_event(directory, STATUS_COMMITTED, "health passed")
            write_config_result(
                directory,
                rollout_id=rollout_id,
                status=STATUS_COMMITTED,
                health_result="passed",
                rollback_status=None,
            )
            write_runtime_config_state(
                deploy,
                build_runtime_config_state(
                    revision=revision,
                    deployment_id=current.deployment_id,
                    latest_config_rollout_id=rollout_id,
                    runtime_values=target_runtime,
                    build_values=target_build,
                    updated_at_utc=utc_now(),
                ),
            )
            return ConfigRolloutResult(
                ok=True,
                already_active_config=False,
                messages=(
                    "OK: config rollout committed",
                    f"config_rollout_id={rollout_id}",
                    f"affected_services={','.join(affected)}",
                    f"runtime_config_fingerprint={plan.target_runtime_config_fingerprint}",
                    f"revision={revision}",
                ),
                config_rollout_id=rollout_id,
                status=STATUS_COMMITTED,
            )
    except (
        ConfigRolloutError,
        ConfigContractError,
        ConfigStateError,
        ConfigJournalError,
        CurrentStateError,
        ManifestError,
        ImageIdentityError,
        ReleaseVerifyError,
        RevisionError,
        DbHeadsError,
        CompatibilityError,
        DatabaseDriftError,
        DockerCheckError,
        EnvFileError,
        OverrideError,
        ActivateError,
        StabilizeError,
        RolloutLockError,
        OSError,
        ValueError,
    ) as exc:
        if directory is not None and rollout_id is not None:
            try:
                append_config_event(directory, STATUS_FAILED, "pre-activation failure")
                write_config_result(
                    directory,
                    rollout_id=rollout_id,
                    status=STATUS_FAILED,
                    health_result="not_run",
                    rollback_status=None,
                )
            except Exception:  # noqa: BLE001
                pass
        return ConfigRolloutResult(
            ok=False,
            already_active_config=False,
            messages=(f"FAIL: {redact(str(exc))}",),
            config_rollout_id=rollout_id,
            status=None,
        )
