#!/usr/bin/env python3
"""Real disposable media release matrix, not a security-audit PASS.

Explicit native execution only: Linux AMD64/root, clean committed source,
Docker >=28/systemd/cgroup2 and Landlock >=6. One temporary /run slice and
one test project. NOT_RUN exits 2. No production unit, prune or retries.

Failure evidence is written before scoped cleanup. Diagnostics never replace
the primary rollout failure, and unreached matrix cases are recorded NOT_RUN.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import secrets
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
if str(_ROOT / "tests" / "release") not in sys.path:
    sys.path.insert(0, str(_ROOT / "tests" / "release"))

from fetchnow_release.activate import ActivateError  # noqa: E402
from fetchnow_release.bounded_subprocess import run_bounded  # noqa: E402
from fetchnow_release.bootstrap_cleanup import BootstrapCleanupError  # noqa: E402
from fetchnow_release.bootstrap_journal import find_unresolved_bootstraps  # noqa: E402
from fetchnow_release.c2_constants import SOURCE_DIRNAME  # noqa: E402
from fetchnow_release.c3_constants import (  # noqa: E402
    BOOTSTRAP_CLEANUP_AUDIT_PREFIX,
    OVERRIDES_DIRNAME,
    STATUS_ACTIVATING_MEDIA,
    STATUS_BOOTSTRAP_CLEANED,
    STATUS_BOOTSTRAP_CLEANUP_STARTED,
    STATUS_FAILED,
    STATUS_ROLLED_BACK,
)
from fetchnow_release.config_journal import find_unresolved_config_rollouts  # noqa: E402
from fetchnow_release.config_rollout import (  # noqa: E402
    ConfigRolloutInput,
    run_config_rollout,
)
from fetchnow_release.current_state import current_path  # noqa: E402
from fetchnow_release.deploy_root import release_dir  # noqa: E402
from fetchnow_release.diagnostics import format_rollout_failure_diagnostics  # noqa: E402
from fetchnow_release.journal import (  # noqa: E402
    deployment_blocks_rollout,
    find_unresolved_deployments,
    load_plan,
    new_deployment_id,
    utc_now,
    write_plan,
)
from fetchnow_release.journal_io import read_json  # noqa: E402
from fetchnow_release.env_file import load_env_file, replace_env_file_key  # noqa: E402
from fetchnow_release.health import HealthInput, run_health  # noqa: E402
from fetchnow_release.media_activation import compose_for_media_plan  # noqa: E402
from fetchnow_release.migration_journal import find_unresolved_migrations  # noqa: E402
from fetchnow_release.media_release_evidence import (  # noqa: E402
    MATRIX_CASE_NAMES,
    bounded_container_rows,
    evaluate_source_only_off,
    mark_remaining_cases_not_run,
    merge_primary_and_diagnostic_errors,
    prepare_result_snapshot,
    rollout_result_snapshot,
    safe_load_current_authority,
    safe_load_media_authority,
    sanitized_deployment_journal,
    write_json_evidence,
)
from fetchnow_release.media_rollout import (  # noqa: E402
    MediaRolloutError,
    media_plan_from_journal,
)
from fetchnow_release.media_state import (  # noqa: E402
    MediaActivationState,
    load_media_activation_state,
    media_state_path,
)
from fetchnow_release.prepare import PrepareInput, prepare_release  # noqa: E402
from fetchnow_release.recover import RecoverInput, _execute_accept_target  # noqa: E402
from fetchnow_release.redact import redact  # noqa: E402
from fetchnow_release.resource_budget import parse_resource_budget  # noqa: E402
from fetchnow_release.rollout import (  # noqa: E402
    RolloutInput,
    RolloutResult,
    _compose_with_override,
    rollout_release,
)
from fetchnow_release.rollout_project import make_rollout_project_name  # noqa: E402
from fetchnow_release.stabilize import StabilizationPolicy  # noqa: E402
from release_media_disk_native_probe import run_disk_probe  # noqa: E402
from release_rollout_integration_test import (  # noqa: E402
    assert_current_state_v2,
    commit,
    compose_argv,
    ensure_rollout_fixture_compatibility_policy,
    run,
    wait_for_postgres,
    write_env,
)

ROOT = Path(__file__).resolve().parents[1]


def checked(argv: list[str], cwd: Path) -> str:
    result = run_bounded(argv, cwd=cwd, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"{argv[0]} failed ({result.returncode})")
    return result.stdout_text.strip()


def _marker_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def gateway_fail_once(activate_gateway):
    """Fail the target gateway activation once; later calls stay real.

    Rollback recreates the previous gateway through the same patched symbol.
    Only the first call is the intended injection. Further calls delegate so
    rollback can reach rolled_back instead of dying on the same error.
    """
    injection: dict[str, object] = {
        "gateway_injections": 0,
        "gateway_delegations": 0,
        "phase": None,
        "error": "test-only gateway injection",
    }

    def injected(**kwargs):
        if _marker_int(injection["gateway_injections"]) == 0:
            injection["gateway_injections"] = 1
            injection["phase"] = "activating_gateway"
            raise ActivateError(str(injection["error"]))
        injection["gateway_delegations"] = (
            _marker_int(injection["gateway_delegations"]) + 1
        )
        return activate_gateway(**kwargs)

    return injected, injection


def intended_gateway_injection_reached(injection: dict[str, object]) -> bool:
    """True only when the target failed once and rollback delegated afterward."""
    return (
        injection.get("gateway_injections") == 1
        and _marker_int(injection.get("gateway_delegations")) >= 1
        and injection.get("phase") == "activating_gateway"
        and injection.get("error") == "test-only gateway injection"
    )


def _event_statuses(dep_dir: Path) -> list[str]:
    events = sorted((dep_dir / "events").glob("*.json"))
    return [p.name.split("_", 1)[1].removesuffix(".json") for p in events]


def _media_executor_image(deploy: Path, revision: str) -> tuple[str, str]:
    """Return (reference, image_id) for the media-executor artifact of revision."""
    raw = json.loads((deploy / "releases" / revision / "release.json").read_text())
    for image in raw["images"]:
        if image["service"] == "media-executor":
            ref = str(image["reference"])
            image_id = str(image["image_id"])
            if not ref.startswith("fetchnow-") or not image_id.startswith("sha256:"):
                raise RuntimeError("refusing non-test media-executor identity")
            return ref, image_id
    raise RuntimeError("media-executor image missing from release.json")


def _sha256_file(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if (
        platform.system() != "Linux"
        or platform.machine() not in {"x86_64", "amd64"}
        or os.geteuid() != 0
    ):
        print("NOT_RUN: requires native Linux AMD64/root")
        return 2
    if checked(["git", "status", "--porcelain"], ROOT):
        print("NOT_RUN: source must be committed and clean")
        return 2
    evidence = args.evidence_dir.resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    work = Path(tempfile.mkdtemp(prefix="fetchnow-media-release-test-"))
    clone, deploy = work / "clone", work / "deploy"
    deploy.mkdir()
    project = make_rollout_project_name()
    suffix = secrets.token_hex(6)
    slice_unit = f"fetchnow-media-release-test-{suffix}.slice"
    unit = Path("/run/systemd/system") / slice_unit
    env = work / "env"
    port = f"127.0.0.1:{18000 + secrets.randbelow(1800)}"
    cases: list[dict[str, object]] = []
    compose: list[str] = []
    installed = False
    rc = 1
    primary_failure: dict[str, Any] | None = None
    budget = parse_resource_budget(
        {
            "approved": True,
            "aggregate_memory_max_bytes": 3 * 1024**3,
            "aggregate_cpu_quota_milli": 1000,
            "aggregate_tasks_max": 512,
            "per_container_memory_bytes": {
                "media-executor": 1024**3,
                "media-net-executor": 1536 * 1024**2,
                "egress-proxy": 256 * 1024**2,
            },
            "concurrency": 1,
            "shared_ceiling": False,
            "notes": "disposable test only, not production approval",
        }
    )

    def record(name: str, ok: bool, **extra: object) -> None:
        entry: dict[str, object] = {
            "name": name,
            "status": "PASS" if ok else "FAIL",
            "utc": utc_now(),
        }
        entry.update(extra)
        cases.append(entry)
        if not ok:
            raise RuntimeError(f"matrix assertion failed: {name}")

    def media_state() -> MediaActivationState:
        state = load_media_activation_state(deploy)
        if state is None:
            raise RuntimeError("media authority missing")
        return state

    def prepare(revision: str) -> None:
        # Keep disposable media flags/deny/password; sync only the release pin.
        replace_env_file_key(env, "FETCHNOW_RELEASE_REVISION", revision)
        env_revision = load_env_file(env).get("FETCHNOW_RELEASE_REVISION")
        result = prepare_release(
            PrepareInput(
                project_name=project,
                env_file=env,
                compose_files=(clone / "compose.yaml", clone / "compose.staging.yaml"),
                expected_revision=revision,
                repo_root=clone,
                deploy_root=deploy,
                release_mode="media",
            )
        )
        evaluation = prepare_result_snapshot(
            requested_revision=revision,
            env_revision=env_revision,
            result=result,
        )
        write_json_evidence(evidence / f"prepare-{revision}.json", evaluation)
        if not result.ok:
            persist_failure_bundle(
                stage="prepare-" + revision,
                evaluation=evaluation,
                result=None,
            )
        record("prepare-" + revision, bool(result.ok), prepare=evaluation)

    def inp(revision: str, *, active: bool, bootstrap: bool = False) -> RolloutInput:
        return RolloutInput(
            project_name=project,
            env_file=env,
            expected_revision=revision,
            repo_root=clone,
            deploy_root=deploy,
            gateway_base_url=f"http://{port}",
            bootstrap=bootstrap,
            policy=StabilizationPolicy(
                consecutive_successes=2, interval_seconds=1, window_seconds=60
            ),
            media_activation=active,
            resource_budget=budget.to_dict() if active else None,
            required_deny_cidrs=("172.16.0.0/12",),
            required_deny_ips=("127.0.0.1",),
            media_denied_host="127.0.0.1",
            media_slice_unit=slice_unit,
        )

    def fail_gateway_once(revision: str) -> tuple[RolloutResult, dict[str, object]]:
        from fetchnow_release import rollout

        injected, injection = gateway_fail_once(rollout.activate_gateway)
        with patch.object(rollout, "activate_gateway", injected):
            result = rollout_release(inp(revision, active=True))
        return result, injection

    def project_inspect_rows() -> list[dict[str, Any]]:
        ids = checked(
            [
                "docker",
                "ps",
                "-a",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={project}",
            ],
            ROOT,
        ).split()
        if not ids:
            return []
        return list(json.loads(checked(["docker", "inspect", *ids], ROOT)))

    def sidecars() -> list[dict]:
        return [
            row
            for row in project_inspect_rows()
            if row["Config"]["Labels"].get("com.docker.compose.service")
            in {"media-executor", "media-net-executor", "egress-proxy"}
        ]

    def persist_failure_bundle(
        *,
        stage: str,
        evaluation: dict[str, Any],
        result: RolloutResult | None,
    ) -> None:
        nonlocal primary_failure
        diagnostic_error: str | None = None
        bundle: dict[str, Any] = {
            "stage": stage,
            "evaluation": evaluation,
            "captured_at_utc": utc_now(),
        }
        try:
            bundle["current_authority"] = safe_load_current_authority(
                lambda: read_json(current_path(deploy))
                if current_path(deploy).is_file()
                else None
            )
        except Exception as exc:  # noqa: BLE001
            diagnostic_error = type(exc).__name__
            bundle["current_authority"] = {"load_error": diagnostic_error}
        try:
            dep_id = None if result is None else result.deployment_id
            if dep_id is None and isinstance(evaluation.get("rollout"), dict):
                dep_id = evaluation["rollout"].get("deployment_id")
            bundle["deployment_journal"] = sanitized_deployment_journal(
                deploy_root=deploy, deployment_id=dep_id
            )
            if result is not None:
                bundle["failure_diagnostics_text"] = format_rollout_failure_diagnostics(
                    status=result.status,
                    messages=result.messages,
                    deployment_id=result.deployment_id,
                    deploy_root=deploy,
                )
        except Exception as exc:  # noqa: BLE001
            diagnostic_error = diagnostic_error or type(exc).__name__
            bundle["deployment_journal_error"] = type(exc).__name__
        try:
            bundle["containers"] = bounded_container_rows(project_inspect_rows())
        except Exception as exc:  # noqa: BLE001
            diagnostic_error = diagnostic_error or type(exc).__name__
            bundle["containers_error"] = type(exc).__name__
        merged = merge_primary_and_diagnostic_errors(
            primary=evaluation, diagnostic_error=diagnostic_error
        )
        bundle["evaluation"] = merged
        primary_failure = bundle
        write_json_evidence(evidence / f"{stage}.json", bundle)
        write_json_evidence(evidence / "primary_failure.json", bundle)

    def assert_source_only_off(result: RolloutResult) -> None:
        media = safe_load_media_authority(lambda: load_media_activation_state(deploy))
        evaluation = evaluate_source_only_off(result, media=media)
        # Always persist structured evidence before any assertion abort/cleanup.
        write_json_evidence(evidence / "source-only-off.json", evaluation)
        if not evaluation["ok"]:
            persist_failure_bundle(
                stage="source-only-off", evaluation=evaluation, result=result
            )
            print(
                "FAIL source-only-off:\n"
                + format_rollout_failure_diagnostics(
                    status=result.status,
                    messages=result.messages,
                    deployment_id=result.deployment_id,
                    deploy_root=deploy,
                )
            )
            print(
                "checks="
                + json.dumps(evaluation["checks"])
                + " failures="
                + json.dumps(evaluation["failures"])
            )
        record(
            "source-only-off",
            bool(evaluation["ok"]),
            rollout=rollout_result_snapshot(result),
            media_authority=media,
            checks=evaluation["checks"],
            failures=evaluation["failures"],
        )

    try:
        from fetchnow_release.media_prerequisites import (
            assert_rootful_docker_writable_cgroups,
            probe_landlock_abi,
        )

        assert_rootful_docker_writable_cgroups()
        probe_landlock_abi()
        if unit.exists():
            raise RuntimeError("refusing existing test slice")
        unit.write_text(
            "[Unit]\nDescription=FetchNow disposable media release proof\n"
            "[Slice]\nMemoryMax=3G\nCPUQuota=100%\nTasksMax=512\n"
        )
        installed = True
        checked(["systemctl", "daemon-reload"], ROOT)
        checked(["systemctl", "start", slice_unit], ROOT)
        run(
            ["git", "clone", "--local", "--no-hardlinks", str(ROOT), str(clone)],
            cwd=ROOT,
        )
        run(["git", "config", "user.email", "media-release@example.test"], cwd=clone)
        run(["git", "config", "user.name", "Media Release Proof"], cwd=clone)
        ensure_rollout_fixture_compatibility_policy(clone)
        # Unique temporary commits prevent image tag collisions.
        marker = clone / "docs" / "media-release-test-marker.txt"
        marker.write_text(suffix)
        revision_a = commit(clone, "test: isolated media release A")
        write_env(
            env,
            project=project,
            revision=revision_a,
            gateway_port=port,
            deploy_root=deploy,
        )
        with env.open("a") as stream:
            stream.write(
                "MEDIA_EXECUTOR_ENABLED=false\nMEDIA_NET_EXECUTOR_ENABLED=false\nEGRESS_DENY_CIDRS=172.16.0.0/12\nEGRESS_DENY_IPS=127.0.0.1\n"
            )
        compose = compose_argv(project, env, clone)
        prepare(revision_a)
        run(compose + ["up", "-d", "postgres"], cwd=clone)
        wait_for_postgres(compose, clone)
        run(compose + ["run", "--rm", "api", "alembic", "upgrade", "head"], cwd=clone)
        source = rollout_release(inp(revision_a, active=False, bootstrap=True))
        assert_source_only_off(source)
        before = current_path(deploy).read_bytes()
        failed, off_injection = fail_gateway_once(revision_a)
        record(
            "off-to-on-rollback",
            failed.status == STATUS_ROLLED_BACK
            and intended_gateway_injection_reached(off_injection)
            and any(
                "target rollout error: test-only gateway injection" in m
                for m in failed.messages
            )
            and not any(
                "rollback failed: test-only gateway injection" in m
                for m in failed.messages
            )
            and not sidecars()
            and current_path(deploy).read_bytes() == before
            and not media_state().activation,
            rollout=rollout_result_snapshot(failed),
            intended_injection=off_injection,
        )
        on = rollout_release(inp(revision_a, active=True))
        record(
            "same-source-activation",
            on.ok
            and not on.already_active
            and media_state().activation
            and len(sidecars()) == 3,
            rollout=rollout_result_snapshot(on),
        )
        stable = rollout_release(inp(revision_a, active=True))
        record(
            "active-idempotency",
            stable.ok and stable.already_active,
            rollout=rollout_result_snapshot(stable),
        )
        marker.write_text(suffix + " B")
        revision_b = commit(clone, "test: isolated media release B")
        prepare(revision_b)
        before = current_path(deploy).read_bytes()
        previous_ids = {
            row["Config"]["Labels"]["com.docker.compose.service"]: row["Image"]
            for row in sidecars()
        }
        failed, on_injection = fail_gateway_once(revision_b)
        actual_ids = {
            row["Config"]["Labels"]["com.docker.compose.service"]: row["Image"]
            for row in sidecars()
        }
        record(
            "on-to-on-rollback",
            failed.status == STATUS_ROLLED_BACK
            and intended_gateway_injection_reached(on_injection)
            and any(
                "target rollout error: test-only gateway injection" in m
                for m in failed.messages
            )
            and not any(
                "rollback failed: test-only gateway injection" in m
                for m in failed.messages
            )
            and current_path(deploy).read_bytes() == before
            and actual_ids == previous_ids
            and media_state().activation
            and media_state().revision == revision_a,
            rollout=rollout_result_snapshot(failed),
            intended_injection=on_injection,
        )
        from fetchnow_release import rollout

        original_commit = rollout.commit_media_state

        def failed_publication(**kwargs):
            if kwargs["revision"] == revision_b:
                raise OSError("test-only media publication failure")
            return original_commit(**kwargs)

        with patch.object(rollout, "commit_media_state", failed_publication):
            failed = rollout_release(inp(revision_b, active=True))
        record(
            "post-current-publication-rollback",
            failed.status == STATUS_ROLLED_BACK
            and current_path(deploy).read_bytes() == before,
            rollout=rollout_result_snapshot(failed),
        )
        upgraded = rollout_release(inp(revision_b, active=True))
        record(
            "on-to-on-commit",
            upgraded.ok and media_state().revision == revision_b,
            rollout=rollout_result_snapshot(upgraded),
        )
        if upgraded.deployment_id is None:
            raise RuntimeError("successful rollout has no deployment id")
        committed_plan = load_plan(deploy / "deployments" / upgraded.deployment_id)
        target_release = release_dir(deploy, revision_b)
        override_path = (
            deploy
            / "deployments"
            / upgraded.deployment_id
            / OVERRIDES_DIRNAME
            / "images.yaml"
        )
        if not override_path.is_file():
            raise RuntimeError("committed deployment missing images override")
        managed_compose = _compose_with_override(
            target_release, override_path, project_name=project
        )
        media_plan = media_plan_from_journal(committed_plan.media or {})
        if media_plan.activation:
            managed_compose = compose_for_media_plan(
                target_release, base_compose=managed_compose, plan=media_plan
            )
        managed_health_cwd = target_release / SOURCE_DIRNAME

        def managed_health_input() -> HealthInput:
            return HealthInput(
                project_name=project,
                env_file=env,
                compose_files=tuple(managed_compose),
                expected_revision=revision_b,
                repo_root=managed_health_cwd,
                gateway_base_url=f"http://{port}",
                deploy_root=deploy,
            )

        # --- Healthy media-ON prerequisites before F3/F4 (no synthetic recovery yet) ---
        prereq_failures: list[str] = []
        state_b = media_state()
        if state_b.revision != revision_b:
            prereq_failures.append("active_revision_not_b")
        if not state_b.activation:
            prereq_failures.append("media_activation_not_on")
        if state_b.resource_budget is None:
            prereq_failures.append("media_budget_missing")
        else:
            try:
                parse_resource_budget(state_b.resource_budget)
            except Exception:  # noqa: BLE001
                prereq_failures.append("media_budget_invalid")
        running_sidecars = {
            row["Config"]["Labels"].get("com.docker.compose.service")
            for row in sidecars()
            if row.get("State", {}).get("Running")
        }
        if running_sidecars != {
            "media-executor",
            "media-net-executor",
            "egress-proxy",
        }:
            prereq_failures.append(f"sidecars_not_running:{sorted(running_sidecars)}")
        unresolved = (
            tuple(find_unresolved_deployments(deploy))
            + tuple(find_unresolved_migrations(deploy))
            + tuple(find_unresolved_bootstraps(deploy))
            + tuple(find_unresolved_config_rollouts(deploy))
        )
        if unresolved:
            prereq_failures.append(f"unresolved_journals:{list(unresolved)}")
        baseline_health = run_health(managed_health_input())
        if not baseline_health.ok:
            prereq_failures.append(
                "managed_health_not_pass:"
                + redact(" | ".join(baseline_health.messages[:3]))
            )
        record(
            "f3-f4-prerequisites",
            not prereq_failures,
            failures=prereq_failures,
            health_messages=list(baseline_health.messages),
            unresolved=list(unresolved),
        )
        if prereq_failures:
            raise RuntimeError(
                "F3/F4 prerequisites not met (not an F3 defect): "
                + ", ".join(prereq_failures)
            )

        # --- F3: config-only refuses while media activation is ON ---
        env_before_f3 = env.read_bytes()
        current_before_f3 = current_path(deploy).read_bytes()
        media_before_f3 = media_state_path(deploy).read_bytes()
        sidecar_ids_before = {
            row["Id"]
            for row in sidecars()
            if row["Config"]["Labels"].get("com.docker.compose.service")
            in {"media-executor", "media-net-executor"}
        }
        cfg = run_config_rollout(
            ConfigRolloutInput(
                project_name=project,
                env_file=env,
                expected_revision=revision_b,
                repo_root=clone,
                deploy_root=deploy,
                gateway_base_url=f"http://{port}",
            )
        )
        record(
            "f3-config-rollout-refuses-media-on",
            (not cfg.ok)
            and any("media activation is ON" in m for m in cfg.messages)
            and not any("unresolved release journal" in m for m in cfg.messages)
            and env.read_bytes() == env_before_f3
            and current_path(deploy).read_bytes() == current_before_f3
            and media_state_path(deploy).read_bytes() == media_before_f3
            and media_state().activation
            and {
                row["Id"]
                for row in sidecars()
                if row["Config"]["Labels"].get("com.docker.compose.service")
                in {"media-executor", "media-net-executor"}
            }
            == sidecar_ids_before,
            rollout=rollout_result_snapshot(cfg),
            env_sha256=_sha256_file(env),
            current_sha256=_sha256_file(current_path(deploy)),
            media_sha256=_sha256_file(media_state_path(deploy)),
        )

        # --- F4: invalid ON budget fails managed run_health; state restored ---
        media_path = media_state_path(deploy)
        original_media = media_path.read_bytes()
        current_before_f4 = current_path(deploy).read_bytes()
        f4_ok = True
        f4_detail: dict[str, object] = {
            "baseline_ok": baseline_health.ok,
            "baseline_messages": list(baseline_health.messages),
        }
        try:
            for label, bad in (
                ("missing", None),
                ("null", {"resource_budget": None}),
                (
                    "unapproved",
                    {
                        "resource_budget": {
                            **budget.to_dict(),
                            "approved": False,
                        }
                    },
                ),
                ("malformed", {"resource_budget": {"approved": True}}),
            ):
                raw = json.loads(original_media)
                if bad is None:
                    raw.pop("resource_budget", None)
                else:
                    raw.update(bad)
                media_path.write_text(
                    json.dumps(raw, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                bad_health = run_health(managed_health_input())
                joined = " ".join(bad_health.messages)
                budget_fail = (not bad_health.ok) and (
                    "resource_budget" in joined
                    or "ON media expectation requires resource_budget" in joined
                )
                authority_intact = (
                    current_path(deploy).read_bytes() == current_before_f4
                )
                f4_detail[label] = {
                    "ok": bad_health.ok,
                    "budget_error": budget_fail,
                    "authority_unchanged": authority_intact,
                    "messages": list(bad_health.messages[:4]),
                }
                if not budget_fail or not authority_intact:
                    f4_ok = False
        finally:
            media_path.write_bytes(original_media)
        restored_bytes_ok = media_path.read_bytes() == original_media
        restored = load_media_activation_state(deploy)
        restored_health = run_health(managed_health_input())
        f4_detail["restored_bytes"] = restored_bytes_ok
        f4_detail["restored_health_ok"] = restored_health.ok
        f4_detail["restored_health_messages"] = list(restored_health.messages)
        if not restored_bytes_ok or not restored_health.ok:
            f4_ok = False
        record(
            "f4-invalid-on-budget-health-fail",
            f4_ok
            and restored is not None
            and restored.activation
            and restored.resource_budget is not None
            and restored_bytes_ok
            and restored_health.ok,
            detail=f4_detail,
        )

        # --- F2: real LandlockRunner disk limits (separate disposable container) ---
        image_ref, image_id = _media_executor_image(deploy, revision_b)
        disk = run_disk_probe(
            image_ref=image_ref,
            image_id=image_id,
            evidence_dir=evidence,
        )
        by_name = {str(c.get("name")): c for c in disk.get("cases", [])}
        for case_name in (
            "f2-disk-fast-growth-fail",
            "f2-disk-fast-tree-fail",
            "f2-disk-staged-inputs-success",
            "f2-disk-prestart-refuse",
            "f2-disk-long-writer-kill-reap",
            "f2-disk-neighbor-continues",
        ):
            entry = by_name.get(case_name, {})
            record(
                case_name,
                bool(disk.get("ok")) and bool(entry.get("ok")),
                image_id=image_id,
                image_ref=image_ref,
                case=entry,
                trust_boundary=disk.get("trust_boundary"),
            )

        # --- F1a/F1b: separate disposable project (does not pollute main matrix) ---
        from fetchnow_release import media_rollout as media_rollout_mod
        from fetchnow_release import rollout as rollout_mod

        f1_project = make_rollout_project_name()
        f1_deploy = work / "deploy-f1"
        f1_deploy.mkdir()
        f1_env = work / "env-f1"
        f1_port = f"127.0.0.1:{20000 + secrets.randbelow(1500)}"
        f1_compose: list[str] = []
        foreign_name = f"fetchnow-f1-foreign-{suffix}"
        write_env(
            f1_env,
            project=f1_project,
            revision=revision_a,
            gateway_port=f1_port,
            deploy_root=f1_deploy,
        )
        with f1_env.open("a") as stream:
            stream.write(
                "MEDIA_EXECUTOR_ENABLED=false\n"
                "MEDIA_NET_EXECUTOR_ENABLED=false\n"
                "EGRESS_DENY_CIDRS=172.16.0.0/12\n"
                "EGRESS_DENY_IPS=127.0.0.1\n"
            )
        replace_env_file_key(f1_env, "FETCHNOW_RELEASE_REVISION", revision_a)
        f1_prepare = prepare_release(
            PrepareInput(
                project_name=f1_project,
                env_file=f1_env,
                compose_files=(clone / "compose.yaml", clone / "compose.staging.yaml"),
                expected_revision=revision_a,
                repo_root=clone,
                deploy_root=f1_deploy,
                release_mode="media",
            )
        )
        if not f1_prepare.ok:
            raise RuntimeError("F1 prepare failed")
        f1_compose = compose_argv(f1_project, f1_env, clone)
        run(f1_compose + ["up", "-d", "postgres"], cwd=clone)
        wait_for_postgres(f1_compose, clone)
        run(
            f1_compose + ["run", "--rm", "api", "alembic", "upgrade", "head"],
            cwd=clone,
        )
        # Foreign sentinel: different compose project label; must survive cleanup.
        checked(
            [
                "docker",
                "run",
                "-d",
                "--name",
                foreign_name,
                "--label",
                f"com.docker.compose.project=foreign-{suffix}",
                "--label",
                "com.docker.compose.service=sentinel",
                "alpine:3.20",
                "sleep",
                "3600",
            ],
            ROOT,
        )
        foreign_id = checked(
            ["docker", "inspect", "--format", "{{.Id}}", foreign_name], ROOT
        )
        pg_before = checked(
            [
                "docker",
                "compose",
                "--env-file",
                str(f1_env),
                "--project-name",
                f1_project,
                "-f",
                str(clone / "compose.yaml"),
                "ps",
                "-q",
                "postgres",
            ],
            clone,
        ).strip()

        def f1_inp(*, active: bool) -> RolloutInput:
            return RolloutInput(
                project_name=f1_project,
                env_file=f1_env,
                expected_revision=revision_a,
                repo_root=clone,
                deploy_root=f1_deploy,
                gateway_base_url=f"http://{f1_port}",
                bootstrap=True,
                policy=StabilizationPolicy(
                    consecutive_successes=2, interval_seconds=1, window_seconds=60
                ),
                media_activation=active,
                resource_budget=budget.to_dict() if active else None,
                required_deny_cidrs=("172.16.0.0/12",),
                required_deny_ips=("127.0.0.1",),
                media_denied_host="127.0.0.1",
                media_slice_unit=slice_unit,
            )

        # F1a: real sidecars start, then test-only failure before worker.
        original_budget_assert = media_rollout_mod.assert_running_media_budget

        def fail_after_sidecars(**_kwargs: object) -> None:
            raise MediaRolloutError(
                "test-only media failure after partial sidecar activation"
            )

        with patch.object(
            media_rollout_mod, "assert_running_media_budget", fail_after_sidecars
        ):
            f1a = rollout_release(f1_inp(active=True))
        f1a_dep = (
            f1_deploy / "deployments" / f1a.deployment_id if f1a.deployment_id else None
        )
        f1a_statuses = _event_statuses(f1a_dep) if f1a_dep else []
        f1a_audit = [
            m for m in f1a.messages if m.startswith(BOOTSTRAP_CLEANUP_AUDIT_PREFIX)
        ]
        pg_after = checked(
            [
                "docker",
                "compose",
                "--env-file",
                str(f1_env),
                "--project-name",
                f1_project,
                "-f",
                str(clone / "compose.yaml"),
                "ps",
                "-q",
                "postgres",
            ],
            clone,
        ).strip()
        foreign_alive = checked(
            ["docker", "inspect", "--format", "{{.State.Running}}", foreign_name],
            ROOT,
        ).strip()
        f1_ids = checked(
            [
                "docker",
                "ps",
                "-a",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={f1_project}",
            ],
            ROOT,
        ).split()
        f1_media_left: list[str] = []
        if f1_ids:
            for row in json.loads(checked(["docker", "inspect", *f1_ids], ROOT)):
                svc = row["Config"]["Labels"].get("com.docker.compose.service")
                if svc in {"media-executor", "media-net-executor", "egress-proxy"}:
                    f1_media_left.append(svc)
        # Allow optional initializing_storage before activating_media.
        cleanup_chain = [
            STATUS_ACTIVATING_MEDIA,
            STATUS_BOOTSTRAP_CLEANUP_STARTED,
            STATUS_BOOTSTRAP_CLEANED,
            STATUS_FAILED,
        ]
        chain_ok = False
        for idx in range(0, max(0, len(f1a_statuses) - len(cleanup_chain) + 1)):
            if f1a_statuses[idx : idx + len(cleanup_chain)] == cleanup_chain:
                chain_ok = True
                break
        record(
            "f1a-bootstrap-media-on-partial-fail-cleanup",
            (not f1a.ok)
            and f1a.status == STATUS_FAILED
            and chain_ok
            and bool(f1a_audit)
            and not f1_media_left
            and pg_after == pg_before
            and foreign_alive == "true"
            and checked(
                ["docker", "inspect", "--format", "{{.Id}}", foreign_name], ROOT
            )
            == foreign_id
            and not current_path(f1_deploy).exists()
            and load_media_activation_state(f1_deploy) is None
            and any(
                "test-only media failure after partial sidecar activation" in m
                for m in f1a.messages
            ),
            rollout=rollout_result_snapshot(f1a),
            statuses=f1a_statuses,
            audit_present=bool(f1a_audit),
        )
        del original_budget_assert

        # F1b: cleanup removal fails → unresolved journal (no cleaned/failed).
        def boom_remove(**_kwargs: object) -> tuple:
            raise BootstrapCleanupError("test-only ownership removal failure")

        with (
            patch.object(
                media_rollout_mod, "assert_running_media_budget", fail_after_sidecars
            ),
            patch.object(rollout_mod, "remove_owned_bootstrap_containers", boom_remove),
        ):
            f1b = rollout_release(f1_inp(active=True))
        f1b_dep = (
            f1_deploy / "deployments" / f1b.deployment_id if f1b.deployment_id else None
        )
        f1b_statuses = _event_statuses(f1b_dep) if f1b_dep else []
        record(
            "f1b-bootstrap-media-on-cleanup-failure-unresolved",
            (not f1b.ok)
            and f1b.status is None
            and f1b_dep is not None
            and STATUS_ACTIVATING_MEDIA in f1b_statuses
            and STATUS_BOOTSTRAP_CLEANUP_STARTED in f1b_statuses
            and STATUS_BOOTSTRAP_CLEANED not in f1b_statuses
            and STATUS_FAILED not in f1b_statuses
            and not (f1b_dep / "result.json").exists()
            and deployment_blocks_rollout(f1b_dep)
            and any(
                "test-only media failure after partial sidecar activation" in m
                for m in f1b.messages
            )
            and any("bootstrap cleanup" in m.lower() for m in f1b.messages)
            and any("unresolved journal" in m.lower() for m in f1b.messages),
            rollout=rollout_result_snapshot(f1b),
            statuses=f1b_statuses,
        )
        # Scoped F1 teardown (only this project's resources + foreign sentinel).
        f1_cleanup_ids = checked(
            [
                "docker",
                "ps",
                "-a",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={f1_project}",
            ],
            ROOT,
        ).split()
        if f1_cleanup_ids:
            checked(["docker", "rm", "-f", *f1_cleanup_ids], ROOT)
        if f1_compose:
            checked(
                [
                    *f1_compose,
                    "-f",
                    str(clone / "compose.media-executor.yaml"),
                    "-f",
                    str(clone / "compose.media-net-executor.yaml"),
                    "--profile",
                    "media-executor",
                    "--profile",
                    "media-net-executor",
                    "down",
                    "-v",
                    "--remove-orphans",
                ],
                clone,
            )
        checked(["docker", "rm", "-f", foreign_name], ROOT)

        # --- Accept-target helper cases last (write_plan leaves unresolved journal;
        # negative path stops egress-proxy). Must not precede F3/F4/F2/F1. ---
        original_plan = committed_plan
        recovery_id = new_deployment_id()
        recovery_plan = replace(original_plan, deployment_id=recovery_id)
        recovery_dir = deploy / "deployments" / recovery_id
        write_plan(recovery_dir, recovery_plan)
        recovery_input = RecoverInput(
            project_name=project,
            env_file=env,
            deploy_root=deploy,
            repo_root=clone,
            deployment_id=recovery_id,
            action="accept-target",
            gateway_base_url=f"http://{port}",
        )

        def accept_target() -> str:
            return _execute_accept_target(
                inp=recovery_input,
                deploy=deploy,
                plan=recovery_plan,
                dep_dir=recovery_dir,
                compose_override_dir=recovery_dir / "overrides",
            )

        recovered = accept_target()
        record(
            "accept-target-media-health",
            recovered == revision_b and media_state().revision == revision_b,
        )
        proxy = next(
            row
            for row in sidecars()
            if row["Config"]["Labels"].get("com.docker.compose.service")
            == "egress-proxy"
        )
        checked(["docker", "stop", proxy["Id"]], ROOT)
        authority_before = current_path(deploy).read_bytes()
        rejected = False
        try:
            accept_target()
        except Exception:
            rejected = True
        record(
            "accept-target-rejects-stopped-media",
            rejected and current_path(deploy).read_bytes() == authority_before,
        )

        assert_current_state_v2(deploy, application_revision=revision_b)
        rc = 0
    except Exception as exc:
        cases.append(
            {
                "name": "execution",
                "status": "FAIL",
                "error_type": type(exc).__name__,
                "error_detail": redact(str(exc)),
            }
        )
        print(
            f"FAIL: native media release matrix ({type(exc).__name__}: {redact(str(exc))})"
        )
        if primary_failure is None:
            try:
                persist_failure_bundle(
                    stage="execution",
                    evaluation={
                        "stage": "execution",
                        "ok": False,
                        "error_type": type(exc).__name__,
                        "error_detail": redact(str(exc)),
                    },
                    result=None,
                )
            except Exception as diag_exc:  # noqa: BLE001
                write_json_evidence(
                    evidence / "execution_diagnostic_error.json",
                    {
                        "primary_error_type": type(exc).__name__,
                        "primary_error_detail": redact(str(exc)),
                        "diagnostic_error": type(diag_exc).__name__,
                    },
                )
    finally:
        cases.extend(
            mark_remaining_cases_not_run(planned=MATRIX_CASE_NAMES, recorded=cases)
        )
        # Evidence must exist before disposable teardown.
        if primary_failure is not None:
            write_json_evidence(evidence / "primary_failure.json", primary_failure)
        cleanup_ok = True
        cleanup_error: str | None = None
        artifacts = []
        references = []
        try:
            for path in sorted((deploy / "releases").glob("*/release.json")):
                raw = json.loads(path.read_text())
                artifacts.append({"revision": raw["revision"], "images": raw["images"]})
                for image in raw["images"]:
                    reference = image["reference"]
                    if not reference.startswith("fetchnow-") or not reference.endswith(
                        ":" + path.parent.name
                    ):
                        raise RuntimeError("refusing non-test image cleanup reference")
                    references.append(reference)
        except Exception as exc:
            cleanup_ok = False
            cleanup_error = type(exc).__name__
            rc = 1
            references.clear()
        try:
            ids = checked(
                [
                    "docker",
                    "ps",
                    "-a",
                    "-q",
                    "--filter",
                    f"label=com.docker.compose.project={project}",
                ],
                ROOT,
            ).split()
            if ids:
                checked(["docker", "rm", "-f", *ids], ROOT)
            if compose:
                checked(
                    [
                        *compose,
                        "-f",
                        str(clone / "compose.media-executor.yaml"),
                        "-f",
                        str(clone / "compose.media-net-executor.yaml"),
                        "--profile",
                        "media-executor",
                        "--profile",
                        "media-net-executor",
                        "down",
                        "-v",
                        "--remove-orphans",
                    ],
                    clone,
                )
            for reference in sorted(set(references)):
                checked(["docker", "image", "rm", reference], ROOT)
        except Exception as exc:
            cleanup_ok = False
            cleanup_error = cleanup_error or type(exc).__name__
            rc = 1
        if installed:
            try:
                checked(["systemctl", "stop", slice_unit], ROOT)
                unit.unlink()
                checked(["systemctl", "daemon-reload"], ROOT)
            except Exception as exc:
                cleanup_ok = False
                cleanup_error = cleanup_error or type(exc).__name__
                rc = 1
        cases.append(
            {
                "name": "scoped-cleanup",
                "status": "PASS" if cleanup_ok else "FAIL",
                **({"error_type": cleanup_error} if cleanup_error else {}),
            }
        )
        # Cleanup failure must not erase or rewrite primary failure evidence.
        (evidence / "result.json").write_text(
            json.dumps(
                {
                    "project": project,
                    "slice": slice_unit,
                    "source_sha": checked(["git", "rev-parse", "HEAD"], ROOT),
                    "status": "PASS" if rc == 0 else "FAIL",
                    "cases": cases,
                    "artifacts": artifacts,
                    "primary_failure_present": primary_failure is not None,
                    "security_residual_acceptance": False,
                },
                indent=2,
            )
            + "\n"
        )
        if cleanup_ok:
            shutil.rmtree(work)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
