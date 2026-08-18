#!/usr/bin/env python3
"""Isolated initial DB bootstrap Docker integration (ephemeral project only).

Uses only ``fetchnow-bootstrap-test-*``. Never staging or production.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "tests" / "release"))

from fetchnow_release.bootstrap_db import BootstrapDbInput, run_bootstrap_db  # noqa: E402
from fetchnow_release.bootstrap_journal import (  # noqa: E402
    BootstrapIdentity,
    find_matching_committed_bootstrap,
    load_bootstrap_plan,
    load_bootstrap_result,
    bootstrap_dir,
)
from fetchnow_release.bootstrap_project import (  # noqa: E402
    BootstrapProjectError,
    assert_bootstrap_project,
    make_bootstrap_project_name,
)
from fetchnow_release.c2_constants import SOURCE_DIRNAME  # noqa: E402
from fetchnow_release.c3_constants import RUNTIME_APPLICATION_SERVICES  # noqa: E402
from fetchnow_release.c3b3_constants import STATUS_BOOTSTRAP_COMMITTED  # noqa: E402
from fetchnow_release.current_state import current_path  # noqa: E402
from fetchnow_release.db_heads import (  # noqa: E402
    probe_database_heads,
    target_heads_from_release_source,
)
from fetchnow_release.deploy_plan import DeployPlanInput, run_deploy_plan  # noqa: E402
from fetchnow_release.deploy_root import release_dir  # noqa: E402
from fetchnow_release.journal import sha256_file  # noqa: E402
from fetchnow_release.manifest import load_manifest, manifest_path  # noqa: E402
from fetchnow_release.override import image_ids_from_manifest  # noqa: E402
from fetchnow_release.prepare import PrepareInput, prepare_release  # noqa: E402
from fetchnow_release.stabilize import StabilizationPolicy, wait_services_healthy  # noqa: E402
from password_fixture import valid_test_password  # noqa: E402

INTEGRATION_SUCCESS_MARKER = "OK: bootstrap-db integration passed"
APP_SERVICES = RUNTIME_APPLICATION_SERVICES


def assert_passed(condition: bool, marker: str, *, diagnostics: str = "") -> None:
    if not condition:
        suffix = f"\n{diagnostics}" if diagnostics else ""
        raise RuntimeError(f"ASSERTION_FAILED: {marker}{suffix}")
    print(f"ASSERTION_PASSED: {marker}")


def run(
    command: list[str], *, cwd: Path, check: bool = True
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        command, cwd=str(cwd), capture_output=True, text=True, check=False
    )
    if check and proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "no command output"
        raise RuntimeError(
            f"command failed ({proc.returncode}): {command[0]}: {detail}"
        )
    return proc


def configure_fixture_git_identity(clone: Path) -> None:
    run(["git", "config", "--local", "user.name", "FetchNow Bootstrap Integration"], cwd=clone)
    run(
        ["git", "config", "--local", "user.email", "bootstrap-integration@fetchnow.invalid"],
        cwd=clone,
    )
    run(["git", "config", "--local", "commit.gpgsign", "false"], cwd=clone)


def write_env(
    path: Path,
    *,
    project: str,
    revision: str,
    gateway_port: str,
    deploy_root: Path,
    backup_root: Path,
) -> None:
    path.write_text(
        "\n".join(
            [
                f"COMPOSE_PROJECT_NAME={project}",
                f"FETCHNOW_RELEASE_REVISION={revision}",
                f"GATEWAY_PORT={gateway_port}",
                "APP_ENV=test",
                "PUBLIC_SITE_URL=http://127.0.0.1",
                "POSTGRES_DB=fetchnow",
                "POSTGRES_USER=fetchnow",
                f"POSTGRES_PASSWORD={valid_test_password()}",
                f"FETCHNOW_DEPLOY_ROOT={deploy_root}",
                f"FETCHNOW_BACKUP_ROOT={backup_root}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def compose_argv(project: str, env_file: Path, clone: Path) -> list[str]:
    return [
        "docker",
        "compose",
        "--env-file",
        str(env_file),
        "--project-name",
        project,
        "-f",
        str(clone / "compose.yaml"),
        "-f",
        str(clone / "compose.staging.yaml"),
    ]


def release_compose_files(deploy_root: Path, revision: str) -> tuple[Path, ...]:
    source = release_dir(deploy_root, revision) / SOURCE_DIRNAME
    return (
        (source / "compose.yaml").resolve(),
        (source / "compose.staging.yaml").resolve(),
    )


def running_services(compose: list[str], clone: Path) -> set[str]:
    proc = run(compose + ["ps", "-a", "--format", "json"], cwd=clone, check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        return set()
    text = proc.stdout.strip()
    rows: list[dict[str, object]]
    if text.startswith("["):
        data = json.loads(text)
        rows = data if isinstance(data, list) else []
    else:
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    present: set[str] = set()
    for row in rows:
        svc = str(row.get("Service") or row.get("service") or "")
        state = str(row.get("State") or "").lower()
        if svc and state in {"running", "created", "restarting", "paused"}:
            present.add(svc)
    return present


def down_project(compose: list[str], clone: Path, project: str) -> None:
    assert_bootstrap_project(project)
    proc = run(compose + ["down", "-v", "--remove-orphans"], cwd=clone, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"cleanup down -v failed for {project}: "
            + (proc.stderr.strip() or proc.stdout.strip() or "unknown")
        )
    print(f"OK: isolated Compose project cleanup completed ({project})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-file", type=Path, default=None)
    parser.add_argument("--dirty-project-file", type=Path, default=None)
    args = parser.parse_args(argv)

    project = make_bootstrap_project_name()
    dirty_project = make_bootstrap_project_name()
    while dirty_project == project:
        dirty_project = make_bootstrap_project_name()
    if args.project_file:
        args.project_file.parent.mkdir(parents=True, exist_ok=True)
        args.project_file.write_text(project + "\n", encoding="utf-8")
        os.chmod(args.project_file, 0o600)
    if args.dirty_project_file:
        args.dirty_project_file.parent.mkdir(parents=True, exist_ok=True)
        args.dirty_project_file.write_text(dirty_project + "\n", encoding="utf-8")
        os.chmod(args.dirty_project_file, 0o600)

    clone = Path(tempfile.mkdtemp(prefix="fetchnow-bootstrap-clone-"))
    deploy_root = Path(tempfile.mkdtemp(prefix="fetchnow-bootstrap-deploy-"))
    backup_root = Path(tempfile.mkdtemp(prefix="fetchnow-bootstrap-backup-"))
    env_dir = Path(tempfile.mkdtemp(prefix="fetchnow-bootstrap-env-"))
    os.chmod(deploy_root, 0o750)
    os.chmod(backup_root, 0o700)
    gateway_port = f"127.0.0.1:{21000 + secrets.randbelow(1000)}"
    dirty_gateway = f"127.0.0.1:{22000 + secrets.randbelow(1000)}"
    compose: list[str] | None = None
    dirty_compose: list[str] | None = None
    cleanup_done = False
    dirty_cleanup_done = False
    rc = 1
    try:
        assert_bootstrap_project(project)
        assert_bootstrap_project(dirty_project)
        for banned in ("fetchnow-staging", "fetchnow-production"):
            if banned in {project, dirty_project}:
                raise RuntimeError(f"refusing real project name {banned}")

        run(["git", "clone", "--local", "--no-hardlinks", str(ROOT), str(clone)], cwd=ROOT)
        configure_fixture_git_identity(clone)
        revision = run(["git", "rev-parse", "HEAD"], cwd=clone).stdout.strip()
        run(["git", "checkout", "--detach", revision], cwd=clone)
        run(["git", "update-ref", "refs/remotes/origin/main", revision], cwd=clone)

        env_file = env_dir / ".env.bootstrap"
        write_env(
            env_file,
            project=project,
            revision=revision,
            gateway_port=gateway_port,
            deploy_root=deploy_root,
            backup_root=backup_root,
        )
        dirty_env = env_dir / ".env.bootstrap-dirty"
        write_env(
            dirty_env,
            project=dirty_project,
            revision=revision,
            gateway_port=dirty_gateway,
            deploy_root=deploy_root,
            backup_root=backup_root,
        )
        compose = compose_argv(project, env_file, clone)
        dirty_compose = compose_argv(dirty_project, dirty_env, clone)
        clone_files = (
            (clone / "compose.yaml").resolve(),
            (clone / "compose.staging.yaml").resolve(),
        )
        policy = StabilizationPolicy()

        prepared = prepare_release(
            PrepareInput(
                project_name=project,
                env_file=env_file,
                compose_files=clone_files,
                expected_revision=revision,
                repo_root=clone,
                deploy_root=deploy_root,
            )
        )
        assert_passed(prepared.ok, f"prepare succeeded: {prepared.messages}")
        target_release = release_dir(deploy_root, revision)
        target_heads = target_heads_from_release_source(target_release)
        assert_passed(bool(target_heads), "target release has Alembic heads")
        snapshot = release_compose_files(deploy_root, revision)
        cwd = target_release / SOURCE_DIRNAME

        plan = run_deploy_plan(
            DeployPlanInput(
                project_name=project,
                env_file=env_file,
                compose_files=clone_files,
                expected_revision=revision,
                repo_root=clone,
                deploy_root=deploy_root,
                backup_root=backup_root,
            )
        )
        assert_passed(plan.ok, f"initial deploy-plan succeeded: {plan.messages}")
        plan_payload = json.loads(plan.plan_json or "{}")
        assert_passed(plan_payload.get("initial_bootstrap") is True, "plan is initial bootstrap")
        assert_passed(
            plan_payload.get("initial_schema_required") is True,
            "initial_schema_required is true before bootstrap-db",
        )
        print("OK: initial deploy-plan on clean ephemeral environment")

        first = run_bootstrap_db(
            BootstrapDbInput(
                project_name=project,
                env_file=env_file,
                compose_files=clone_files,
                expected_revision=revision,
                repo_root=clone,
                deploy_root=deploy_root,
                wait_lock=True,
                policy=policy,
            )
        )
        assert_passed(first.ok, f"bootstrap-db succeeded: {first.messages}")
        assert_passed(first.already_initialized is False, "first bootstrap is not idempotent skip")
        assert_passed(
            first.status == STATUS_BOOTSTRAP_COMMITTED,
            f"bootstrap status committed, got {first.status}",
        )

        present = running_services(compose, clone)
        assert_passed("postgres" in present, "postgres is running after bootstrap-db")
        for svc in APP_SERVICES:
            assert_passed(svc not in present, f"{svc} was not started")
        assert_passed(
            not current_path(deploy_root).exists(),
            "current.json was not written",
        )

        live = probe_database_heads(
            project_name=project,
            env_file=env_file,
            compose_files=snapshot,
            cwd=cwd,
        )
        assert_passed(live.has_heads, f"live alembic heads present: {live.status}")
        assert_passed(
            live.heads == target_heads,
            f"live heads {sorted(live.heads)} equal target {sorted(target_heads)}",
        )

        manifest = load_manifest(manifest_path(target_release))
        ids = image_ids_from_manifest(manifest)
        identity = BootstrapIdentity(
            project_name=project,
            target_revision=revision,
            target_db_heads=tuple(sorted(target_heads)),
            release_manifest_sha256=sha256_file(manifest_path(target_release)),
            target_api_image_id=ids["api"],
            compose_overlay=snapshot[-1].name,
            source_contract_version=manifest.source_contract_version,
        )
        committed = find_matching_committed_bootstrap(deploy_root, identity)
        assert_passed(committed is not None, "committed bootstrap journal bound to identity")
        assert committed is not None
        result_doc = load_bootstrap_result(bootstrap_dir(deploy_root, committed.bootstrap_id))
        assert_passed(result_doc is not None, "bootstrap result.json exists")
        assert result_doc is not None
        assert_passed(
            result_doc.status == STATUS_BOOTSTRAP_COMMITTED
            and result_doc.current_json_written is False
            and tuple(result_doc.verified_db_heads) == tuple(sorted(target_heads)),
            "committed result binds target heads and does not write current.json",
        )
        plan_doc = load_bootstrap_plan(bootstrap_dir(deploy_root, committed.bootstrap_id))
        assert_passed(
            plan_doc.project_name == project
            and plan_doc.target_revision == revision
            and plan_doc.release_manifest_sha256 == identity.release_manifest_sha256,
            "committed plan binds project, revision, and prepared release",
        )
        print("OK: bootstrap journal committed with exact identity")

        second = run_bootstrap_db(
            BootstrapDbInput(
                project_name=project,
                env_file=env_file,
                compose_files=clone_files,
                expected_revision=revision,
                repo_root=clone,
                deploy_root=deploy_root,
                wait_lock=True,
                policy=policy,
            )
        )
        assert_passed(second.ok, f"second bootstrap-db succeeded: {second.messages}")
        assert_passed(
            second.already_initialized is True,
            "second bootstrap-db is explicit already-initialized behavior",
        )
        assert_passed(
            not current_path(deploy_root).exists(),
            "current.json still absent after idempotent bootstrap-db",
        )
        present_after = running_services(compose, clone)
        for svc in APP_SERVICES:
            assert_passed(svc not in present_after, f"{svc} still not started after retry")
        print("OK: second bootstrap-db is idempotent already-initialized")

        run(
            dirty_compose
            + ["up", "-d", "--no-build", "--no-deps", "--pull", "never", "postgres"],
            cwd=clone,
        )
        wait_services_healthy(
            project_name=dirty_project,
            env_file=dirty_env,
            compose_files=clone_files,
            cwd=clone,
            services=("postgres",),
            policy=policy,
        )
        run(
            dirty_compose
            + [
                "exec",
                "-T",
                "postgres",
                "sh",
                "-c",
                'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 '
                '-c "CREATE TABLE leftover_bootstrap_it (id integer);"',
            ],
            cwd=clone,
        )
        dirty = run_bootstrap_db(
            BootstrapDbInput(
                project_name=dirty_project,
                env_file=dirty_env,
                compose_files=clone_files,
                expected_revision=revision,
                repo_root=clone,
                deploy_root=deploy_root,
                wait_lock=True,
                policy=policy,
            )
        )
        assert_passed(not dirty.ok, f"dirty leftover table must fail closed: {dirty.messages}")
        assert_passed(
            any("unexpected user objects" in msg for msg in dirty.messages),
            f"dirty failure cites leftover objects: {dirty.messages}",
        )
        print("OK: leftover table in non-Alembic DB fails closed")

        print(f"project={project}")
        print(f"dirty_project={dirty_project}")
        print(f"revision={revision}")
        print(f"bootstrap_id={first.bootstrap_id}")
        print(INTEGRATION_SUCCESS_MARKER)
        rc = 0
    except (BootstrapProjectError, Exception):  # noqa: BLE001
        traceback.print_exc()
        rc = 1
    finally:
        try:
            if compose is not None and clone.is_dir() and (clone / "compose.yaml").is_file():
                down_project(compose, clone, project)
                cleanup_done = True
        except Exception as cleanup_exc:  # noqa: BLE001
            print(f"ERROR: {cleanup_exc}", file=sys.stderr)
            cleanup_done = False
            rc = 1
        try:
            if dirty_compose is not None and clone.is_dir() and (clone / "compose.yaml").is_file():
                down_project(dirty_compose, clone, dirty_project)
                dirty_cleanup_done = True
        except Exception as cleanup_exc:  # noqa: BLE001
            print(f"ERROR: {cleanup_exc}", file=sys.stderr)
            dirty_cleanup_done = False
            rc = 1
        shutil.rmtree(clone, ignore_errors=True)
        shutil.rmtree(deploy_root, ignore_errors=True)
        shutil.rmtree(backup_root, ignore_errors=True)
        shutil.rmtree(env_dir, ignore_errors=True)

    if rc == 0 and (not cleanup_done or not dirty_cleanup_done):
        print(
            "ERROR: refusing success without isolated Compose cleanup completion",
            file=sys.stderr,
        )
        return 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
