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
from fetchnow_release.c3_constants import STATUS_ROLLED_BACK  # noqa: E402
from fetchnow_release.current_state import current_path  # noqa: E402
from fetchnow_release.diagnostics import format_rollout_failure_diagnostics  # noqa: E402
from fetchnow_release.journal import (  # noqa: E402
    load_plan,
    new_deployment_id,
    utc_now,
    write_plan,
)
from fetchnow_release.journal_io import read_json  # noqa: E402
from fetchnow_release.media_release_evidence import (  # noqa: E402
    MATRIX_CASE_NAMES,
    bounded_container_rows,
    evaluate_source_only_off,
    mark_remaining_cases_not_run,
    merge_primary_and_diagnostic_errors,
    rollout_result_snapshot,
    safe_load_current_authority,
    safe_load_media_authority,
    sanitized_deployment_journal,
    write_json_evidence,
)
from fetchnow_release.media_state import (  # noqa: E402
    MediaActivationState,
    load_media_activation_state,
)
from fetchnow_release.prepare import PrepareInput, prepare_release  # noqa: E402
from fetchnow_release.recover import RecoverInput, _execute_accept_target  # noqa: E402
from fetchnow_release.redact import redact  # noqa: E402
from fetchnow_release.resource_budget import parse_resource_budget  # noqa: E402
from fetchnow_release.rollout import RolloutInput, RolloutResult, rollout_release  # noqa: E402
from fetchnow_release.rollout_project import make_rollout_project_name  # noqa: E402
from fetchnow_release.stabilize import StabilizationPolicy  # noqa: E402
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
        record("prepare-" + revision, result.ok)

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
        original_plan = load_plan(deploy / "deployments" / upgraded.deployment_id)
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
