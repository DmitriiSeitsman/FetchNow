"""Media activation / previous-state rollback helpers for the release transaction."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from .activate import (
    ActivateError,
    activate_media_sidecars,
)
from .c2_constants import SOURCE_DIRNAME
from .egress_deny_coverage import EgressDenyCoverageError, assert_deny_coverage
from .env_file import load_env_file
from .manifest import ReleaseManifest
from .media_activation import (
    MediaActivationError,
    MediaActivationPlan,
    assert_executor_flags_off,
    assert_media_compose_config,
    build_activation_plan,
    build_source_only_plan,
    compose_files_for_media,
    compose_for_media_plan,
)
from .media_health import run_media_health
from .media_prerequisites import (
    MediaPrerequisiteError,
    assert_running_media_budget,
    check_media_prerequisites,
)
from .media_state import (
    MediaActivationState,
    MediaStateError,
    inactive_media_state,
    load_media_activation_state,
    write_media_activation_state,
)
from .release_mode import RELEASE_MODE_CANONICAL, RELEASE_MODE_MEDIA
from .resource_budget import ResourceBudgetError, parse_resource_budget


class MediaRolloutError(RuntimeError):
    """Media activation path failure inside rollout/recover."""


def resolve_media_plan_for_rollout(
    *,
    media_activation: bool,
    target_manifest: ReleaseManifest,
    env_file: Path,
    required_deny_cidrs: tuple[str, ...],
    required_deny_ips: tuple[str, ...],
    budget_raw: object | None,
    landlock_probe_command: list[str] | None = None,
    slice_unit: str = "fetchnow-media-executor.slice",
    denied_host: str = "",
) -> tuple[MediaActivationPlan, dict[str, Any] | None]:
    """Build activation or source-only plan; fail closed on prereq/deny gaps."""
    if not media_activation:
        # Source-only: refuse hidden enablement when an env file is present.
        if env_file.is_file() and not env_file.is_symlink():
            assert_executor_flags_off(load_env_file(env_file))
        plan = build_source_only_plan(
            release_mode=getattr(
                target_manifest, "release_mode", RELEASE_MODE_CANONICAL
            )
            or RELEASE_MODE_CANONICAL
        )
        return plan, None

    env = load_env_file(env_file)
    if budget_raw is None:
        raise MediaRolloutError(
            "media activation requires an explicit approved resource_budget"
        )
    try:
        report = check_media_prerequisites(
            budget_raw=budget_raw,
            slice_unit=slice_unit,
            landlock_probe_command=landlock_probe_command,
        )
    except (MediaPrerequisiteError, ResourceBudgetError) as exc:
        raise MediaRolloutError(str(exc)) from exc

    try:
        plan = build_activation_plan(
            manifest=target_manifest,
            required_deny_cidrs=required_deny_cidrs,
            required_deny_ips=required_deny_ips,
        )
    except MediaActivationError as exc:
        raise MediaRolloutError(str(exc)) from exc

    deny_cidrs = env.get("EGRESS_DENY_CIDRS", "")
    deny_ips = env.get("EGRESS_DENY_IPS", "")
    try:
        assert_deny_coverage(
            deny_cidrs=deny_cidrs,
            deny_ips=deny_ips,
            required_cidrs=plan.required_deny_cidrs,
            required_ips=plan.required_deny_ips,
        )
    except EgressDenyCoverageError as exc:
        raise MediaRolloutError(str(exc)) from exc

    # Muxing must not be forced off by this path.
    # We intentionally do not read or rewrite MEDIA_MUXING_ENABLED.
    if not denied_host:
        raise MediaRolloutError("media activation requires a denied_host proof target")
    plan = replace(
        plan,
        resource_budget=report.budget.to_dict(),
        slice_unit=slice_unit,
        denied_host=denied_host,
    )
    return plan, plan.to_dict()


def previous_media_for_journal(deploy_root: Path) -> dict[str, Any] | None:
    try:
        state = load_media_activation_state(deploy_root)
    except MediaStateError as exc:
        raise MediaRolloutError(str(exc)) from exc
    if state is None:
        return {
            "activation": False,
            "release_mode": RELEASE_MODE_CANONICAL,
            "media_image_ids": {},
            "executor_settings": {
                "MEDIA_EXECUTOR_ENABLED": "false",
                "MEDIA_NET_EXECUTOR_ENABLED": "false",
            },
            "overlays": [],
            "profiles": [],
        }
    return {
        "activation": state.activation,
        "release_mode": state.release_mode,
        "media_image_ids": dict(state.media_image_ids),
        "executor_settings": dict(state.executor_settings),
        "overlays": list(state.overlays),
        "profiles": list(state.profiles),
        "resource_budget": state.resource_budget,
        "slice_unit": state.slice_unit,
        "denied_host": state.denied_host,
    }


def journal_media_block(
    *,
    target: MediaActivationPlan,
    previous: dict[str, Any] | None,
) -> dict[str, Any]:
    block = target.to_dict()
    if previous is not None:
        block["previous_media_image_ids"] = dict(previous.get("media_image_ids") or {})
        block["previous_executor_settings"] = dict(
            previous.get("executor_settings") or {}
        )
        block["previous_activation"] = bool(previous.get("activation"))
        block["previous_release_mode"] = str(
            previous.get("release_mode") or RELEASE_MODE_CANONICAL
        )
        block["previous_overlays"] = list(previous.get("overlays") or [])
        block["previous_profiles"] = list(previous.get("profiles") or [])
        for key in ("resource_budget", "slice_unit", "denied_host"):
            block["previous_" + key] = previous.get(key)
    return block


def _compose_and_validate_media_plan(
    release_directory: Path,
    *,
    base_compose: tuple[Path, ...],
    plan: MediaActivationPlan,
    project_name: str,
    env_file: Path,
    cwd: Path,
    phase: str,
) -> tuple[Path, ...]:
    compose_files = compose_for_media_plan(
        release_directory, base_compose=base_compose, plan=plan
    )
    try:
        assert_media_compose_config(
            project_name=project_name,
            env_file=env_file,
            compose_files=compose_files,
            cwd=cwd,
            plan=plan,
            phase=phase,
        )
    except MediaActivationError as exc:
        raise MediaRolloutError(str(exc)) from exc
    return compose_files


def activate_media_before_worker(
    *,
    project_name: str,
    env_file: Path,
    release_directory: Path,
    base_compose: tuple[Path, ...],
    cwd: Path,
    plan: MediaActivationPlan,
    denied_host: str,
) -> tuple[Path, ...]:
    """Start sidecars and prove health before any worker recreate."""
    if not plan.activation:
        return base_compose
    compose_files = _compose_and_validate_media_plan(
        release_directory,
        base_compose=base_compose,
        plan=plan,
        project_name=project_name,
        env_file=env_file,
        cwd=cwd,
        phase="activating_media_compose_validate",
    )
    try:
        activate_media_sidecars(
            project_name=project_name,
            env_file=env_file,
            compose_files=compose_files,
            cwd=cwd,
            profiles=plan.profiles,
            services=("egress-proxy", "media-executor", "media-net-executor"),
        )
    except ActivateError as exc:
        raise MediaRolloutError(str(exc)) from exc

    assert_running_media_budget(
        project_name=project_name,
        env_file=env_file,
        compose_files=compose_files,
        cwd=cwd,
        budget=parse_resource_budget(plan.resource_budget),
        slice_unit=plan.slice_unit,
    )

    # Sidecar protocol/proxy proof before worker flags ON. Worker flags are
    # checked after app-tier activation (expect_flags_enabled=True then).
    health = run_media_health(
        project_name=project_name,
        env_file=env_file,
        compose_files=compose_files,
        cwd=cwd,
        executor_socket=plan.executor_settings["MEDIA_EXECUTOR_SOCKET"],
        net_socket=plan.executor_settings["MEDIA_NET_EXECUTOR_SOCKET"],
        expect_flags_enabled=False,
        denied_host=denied_host,
        media_image_ids=plan.media_image_ids,
        check_worker_flags=False,
    )
    if not health.ok:
        raise MediaRolloutError(health.messages[0])
    return compose_files


def commit_media_state(
    *,
    deploy_root: Path,
    revision: str,
    plan: MediaActivationPlan,
    updated_at_utc: str,
) -> None:
    if not plan.activation:
        write_media_activation_state(
            deploy_root,
            inactive_media_state(revision=revision, updated_at_utc=updated_at_utc),
        )
        return
    write_media_activation_state(
        deploy_root,
        MediaActivationState(
            schema_version=1,
            activation=True,
            release_mode=RELEASE_MODE_MEDIA,
            revision=revision,
            media_image_ids=dict(plan.media_image_ids),
            overlays=plan.overlays,
            profiles=plan.profiles,
            executor_settings=dict(plan.executor_settings),
            updated_at_utc=updated_at_utc,
            resource_budget=plan.resource_budget,
            slice_unit=plan.slice_unit,
            denied_host=plan.denied_host,
        ),
    )


def restore_previous_media(
    *,
    project_name: str,
    env_file: Path,
    previous_release: Path,
    base_compose: tuple[Path, ...],
    cwd: Path,
    plan_media: dict[str, Any] | None,
    deploy_root: Path,
    previous_revision: str,
    updated_at_utc: str,
    failed_release: Path | None = None,
    deployment_id: str | None = None,
) -> tuple[Path, ...]:
    """Restore previous media authority. Never deletes host slices.

    OFF→ON failure → previous OFF (stop sidecars we started).
    ON→ON failure → previous ON images/settings.
    """
    previous_activation = bool((plan_media or {}).get("previous_activation"))
    if plan_media is None:
        return base_compose
    if not previous_activation:
        if (plan_media or {}).get("activation"):
            if failed_release is None or deployment_id is None:
                raise MediaRolloutError(
                    "rollback requires failed media transaction ownership"
                )
            from .bootstrap_cleanup import remove_owned_bootstrap_containers

            # Target compose defines the newly introduced services even when
            # the previous release predates media overlays. Labels gate removal.
            from .environment import snapshot_compose_files_for_release
            from .manifest import load_manifest, manifest_path

            target_base = snapshot_compose_files_for_release(
                failed_release,
                project_name=project_name,
                manifest=load_manifest(manifest_path(failed_release)),
            )
            target_files = compose_files_for_media(
                failed_release, base_compose=target_base, activation=True
            )
            remove_owned_bootstrap_containers(
                project_name=project_name,
                env_file=env_file,
                compose_files=target_files,
                cwd=failed_release / SOURCE_DIRNAME,
                deployment_id=deployment_id,
                release_revision=failed_release.name,
                allowed_services=(
                    "media-executor",
                    "media-net-executor",
                    "egress-proxy",
                ),
            )
        previous = build_source_only_plan(release_mode=RELEASE_MODE_CANONICAL)
        return _compose_and_validate_media_plan(
            previous_release,
            base_compose=base_compose,
            plan=previous,
            project_name=project_name,
            env_file=env_file,
            cwd=cwd,
            phase="rollback_media_compose_validate",
        )

    prev_profiles = tuple((plan_media or {}).get("previous_profiles") or ())
    previous = media_plan_from_journal(plan_media or {}, previous=True)
    check_media_prerequisites(
        budget_raw=previous.resource_budget, slice_unit=previous.slice_unit
    )
    compose_files = _compose_and_validate_media_plan(
        previous_release,
        base_compose=base_compose,
        plan=previous,
        project_name=project_name,
        env_file=env_file,
        cwd=cwd,
        phase="rollback_media_compose_validate",
    )
    try:
        activate_media_sidecars(
            project_name=project_name,
            env_file=env_file,
            compose_files=compose_files,
            cwd=cwd,
            profiles=prev_profiles or ("media-executor", "media-net-executor"),
        )
    except ActivateError as exc:
        raise MediaRolloutError(str(exc)) from exc
    assert_running_media_budget(
        project_name=project_name,
        env_file=env_file,
        compose_files=compose_files,
        cwd=cwd,
        budget=parse_resource_budget(previous.resource_budget),
        slice_unit=previous.slice_unit,
    )
    return compose_files


def media_plan_from_journal(
    raw: dict[str, Any], *, previous: bool = False
) -> MediaActivationPlan:
    """Fail closed on incomplete active recovery authority; preserve legacy OFF."""
    prefix = "previous_" if previous else ""
    if not raw.get(prefix + "activation"):
        return build_source_only_plan(release_mode=RELEASE_MODE_CANONICAL)
    from .media_state import parse_media_activation_state

    state = parse_media_activation_state(
        {
            "schema_version": 1,
            "revision": "a" * 40,
            "updated_at_utc": "2026-01-01T00:00:00Z",
            **{
                key: raw.get(prefix + key)
                for key in (
                    "activation",
                    "release_mode",
                    "media_image_ids",
                    "executor_settings",
                    "overlays",
                    "profiles",
                    "resource_budget",
                    "slice_unit",
                    "denied_host",
                )
            },
        }
    )
    budget = parse_resource_budget(state.resource_budget)
    if not state.denied_host:
        raise MediaRolloutError("active recovery plan lacks proxy proof target")
    return MediaActivationPlan(
        activation=True,
        release_mode=state.release_mode,
        overlays=state.overlays,
        profiles=state.profiles,
        media_image_ids=state.media_image_ids,
        executor_settings=state.executor_settings,
        required_deny_cidrs=tuple(raw.get("required_deny_cidrs") or ()),
        required_deny_ips=tuple(raw.get("required_deny_ips") or ()),
        resource_budget=budget.to_dict(),
        slice_unit=state.slice_unit,
        denied_host=state.denied_host,
    )


def verify_media_plan(
    *,
    plan: MediaActivationPlan,
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
    cwd: Path,
) -> None:
    if not plan.activation:
        from .media_health import assert_worker_executor_flags

        assert_worker_executor_flags(
            project_name=project_name,
            env_file=env_file,
            compose_files=compose_files,
            cwd=cwd,
            expect_enabled=False,
        )
        return
    check_media_prerequisites(
        budget_raw=plan.resource_budget, slice_unit=plan.slice_unit
    )
    assert_running_media_budget(
        project_name=project_name,
        env_file=env_file,
        compose_files=compose_files,
        cwd=cwd,
        budget=parse_resource_budget(plan.resource_budget),
        slice_unit=plan.slice_unit,
    )
    env = load_env_file(env_file)
    assert_deny_coverage(
        deny_cidrs=env.get("EGRESS_DENY_CIDRS", ""),
        deny_ips=env.get("EGRESS_DENY_IPS", ""),
        required_cidrs=plan.required_deny_cidrs,
        required_ips=plan.required_deny_ips,
    )
    health = run_media_health(
        project_name=project_name,
        env_file=env_file,
        compose_files=compose_files,
        cwd=cwd,
        executor_socket=plan.executor_settings["MEDIA_EXECUTOR_SOCKET"],
        net_socket=plan.executor_settings["MEDIA_NET_EXECUTOR_SOCKET"],
        expect_flags_enabled=True,
        denied_host=plan.denied_host,
        media_image_ids=plan.media_image_ids,
    )
    if not health.ok:
        raise MediaRolloutError(health.messages[0])


def remove_failed_bootstrap_authority(
    deploy_root: Path, *, deployment_id: str, revision: str
) -> None:
    """After owned-container cleanup, retract only this bootstrap's publication."""
    from .current_state import CurrentStateV2, current_path, load_parsed_current_state
    from .media_state import media_state_path

    state = load_parsed_current_state(deploy_root)
    if state is None:
        return
    if (
        not isinstance(state, CurrentStateV2)
        or state.application.deployment_id != deployment_id
        or state.application.revision != revision
    ):
        raise MediaRolloutError("refusing to retract foreign bootstrap authority")
    media_state = load_media_activation_state(deploy_root)
    if media_state is not None:
        if media_state.revision != revision:
            raise MediaRolloutError("refusing to retract foreign bootstrap media state")
        media_state_path(deploy_root).unlink()
    current_path(deploy_root).unlink()
