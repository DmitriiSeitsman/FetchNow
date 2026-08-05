#!/usr/bin/env python3
"""Isolated PRD1C3A application rollout transaction integration."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "tests" / "release"))

from fetchnow_release.c3_constants import (  # noqa: E402
    APPLICATION_SERVICES,
    LABEL_DEPLOYMENT_ID,
    LABEL_RELEASE_REVISION,
    STATUS_ACTIVATING_APP,
    STATUS_ACTIVATING_GATEWAY,
    STATUS_APP_HEALTHY,
    STATUS_FAILED,
    STATUS_PLANNED,
    STATUS_ROLLBACK_FAILED,
    STATUS_ROLLBACK_STARTED,
    STATUS_STABILIZING,
)
from fetchnow_release.bootstrap_cleanup import (  # noqa: E402
    COMPOSE_PROJECT_LABEL,
    COMPOSE_SERVICE_LABEL,
    discover_owned_bootstrap_containers,
)
from fetchnow_release.c2_constants import SOURCE_CONTRACT_VERSION_V2, SOURCE_DIRNAME  # noqa: E402
from fetchnow_release.c3b1_constants import COMPATIBILITY_REL_PATH  # noqa: E402
from fetchnow_release.journal import (  # noqa: E402
    PLAN_SCHEMA_VERSION,
    PlanDocument,
    append_event,
    deployment_dir,
    ensure_rollout_layout,
    find_unresolved_deployments,
    new_deployment_id,
    sha256_file,
    utc_now,
    write_plan,
    write_result,
)
from fetchnow_release.db_heads import (  # noqa: E402
    DbHeadsError,
    assert_heads_equal,
    database_heads_via_postgres,
    target_heads_from_release_source,
)
from fetchnow_release.deploy_root import release_dir  # noqa: E402
from fetchnow_release.image_identity import assert_release_images_present  # noqa: E402
from fetchnow_release.manifest import load_manifest, manifest_path  # noqa: E402
from fetchnow_release.migration_compatibility import load_compatibility_contract  # noqa: E402
from fetchnow_release.prepare import PrepareInput, prepare_release  # noqa: E402
from fetchnow_release.recover import RecoverInput, recover_deployment  # noqa: E402
from fetchnow_release.rollout import RolloutInput, rollout_release  # noqa: E402
from fetchnow_release.rollout_project import (  # noqa: E402
    assert_rollout_project,
    make_rollout_project_name,
)
from fetchnow_release.stabilize import StabilizationPolicy  # noqa: E402
from password_fixture import valid_test_password  # noqa: E402


FIXTURE_POLICY_PATH = ROOT / COMPATIBILITY_REL_PATH


def ensure_rollout_fixture_compatibility_policy(clone: Path) -> str:
    """Ensure the temporary trusted clone contains the tracked B1 policy file."""
    if not FIXTURE_POLICY_PATH.is_file():
        raise RuntimeError(f"repository fixture policy missing: {FIXTURE_POLICY_PATH}")
    load_compatibility_contract(FIXTURE_POLICY_PATH)
    fixture_bytes = FIXTURE_POLICY_PATH.read_bytes()
    fixture_sha256 = sha256_file(FIXTURE_POLICY_PATH)

    clone_policy = clone / COMPATIBILITY_REL_PATH
    if clone_policy.is_file():
        if clone_policy.is_symlink() or clone_policy.is_dir():
            raise RuntimeError("clone compatibility policy must be a regular file")
        if clone_policy.read_bytes() != fixture_bytes:
            raise RuntimeError(
                "clone compatibility policy bytes drift from repository fixture"
            )
    else:
        clone_policy.parent.mkdir(parents=True, exist_ok=True)
        clone_policy.write_bytes(fixture_bytes)
        run(["git", "add", str(clone_policy)], cwd=clone)
        run(
            ["git", "commit", "-m", "test: add migration compatibility contract for rollout"],
            cwd=clone,
        )
        revision = run(["git", "rev-parse", "HEAD"], cwd=clone).stdout.strip()
        run(["git", "update-ref", "refs/remotes/origin/main", revision], cwd=clone)

    return fixture_sha256


def assert_prepared_release_v2_policy(
    deploy_root: Path,
    revision: str,
    *,
    fixture_policy_sha256: str,
) -> None:
    release = release_dir(deploy_root, revision)
    manifest = load_manifest(manifest_path(release))
    if manifest.source_contract_version != SOURCE_CONTRACT_VERSION_V2:
        raise RuntimeError(
            f"prepared release {revision} must declare source_contract_version=2, "
            f"got {manifest.source_contract_version!r}"
        )
    policy_path = release / SOURCE_DIRNAME / COMPATIBILITY_REL_PATH
    if policy_path.is_symlink() or policy_path.is_dir() or not policy_path.is_file():
        raise RuntimeError(
            f"prepared release {revision} missing regular compatibility policy file"
        )
    if sha256_file(policy_path) != fixture_policy_sha256:
        raise RuntimeError(
            f"prepared release {revision} compatibility policy SHA-256 mismatch"
        )


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


def write_env(
    path: Path, *, project: str, revision: str, gateway_port: str, deploy_root: Path
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
                f"FETCHNOW_BACKUP_ROOT={deploy_root / 'backups'}",
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


def wait_for_postgres(compose: list[str], clone: Path) -> None:
    for _ in range(60):
        ready = run(
            compose
            + [
                "exec",
                "-T",
                "postgres",
                "sh",
                "-c",
                'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
            ],
            cwd=clone,
            check=False,
        )
        if ready.returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError("postgres did not become healthy")


def commit(clone: Path, message: str) -> str:
    run(["git", "add", "-A"], cwd=clone)
    run(["git", "commit", "-m", message], cwd=clone)
    revision = run(["git", "rev-parse", "HEAD"], cwd=clone).stdout.strip()
    run(["git", "update-ref", "refs/remotes/origin/main", revision], cwd=clone)
    return revision


def prepare(
    *, project: str, env_file: Path, revision: str, clone: Path, deploy_root: Path
) -> None:
    result = prepare_release(
        PrepareInput(
            project_name=project,
            env_file=env_file,
            compose_files=(clone / "compose.yaml", clone / "compose.staging.yaml"),
            expected_revision=revision,
            repo_root=clone,
            deploy_root=deploy_root,
        )
    )
    if not result.ok:
        raise RuntimeError(result.messages[0])


def current_revision(deploy_root: Path) -> str:
    path = deploy_root / "state" / "current.json"
    return str(json.loads(path.read_text(encoding="utf-8"))["revision"])


def container_image_ids(compose: list[str], clone: Path) -> dict[str, str]:
    ids: dict[str, str] = {}
    for svc in ("api", "worker", "web", "gateway"):
        cid = run(compose + ["ps", "-q", svc], cwd=clone).stdout.strip()
        if not cid:
            raise RuntimeError(f"missing container for {svc}")
        image = run(
            ["docker", "inspect", cid, "--format", "{{.Image}}"],
            cwd=clone,
        ).stdout.strip()
        ids[svc] = image
    return ids


def seed_unresolved_journal(
    *,
    deploy_root: Path,
    project: str,
    target_revision: str,
    previous_revision: str | None,
    bootstrap: bool,
    target_ids: dict[str, str],
    previous_ids: dict[str, str] | None,
    man_hash: str,
    database_heads: tuple[str, ...],
    event_trail: tuple[str, ...],
) -> str:
    ensure_rollout_layout(deploy_root)
    dep_id = new_deployment_id()
    dep_dir = deployment_dir(deploy_root, dep_id)
    plan = PlanDocument(
        schema_version=PLAN_SCHEMA_VERSION,
        deployment_id=dep_id,
        target_revision=target_revision,
        previous_revision=previous_revision,
        bootstrap=bootstrap,
        target_image_ids=target_ids,
        previous_image_ids=previous_ids,
        release_manifest_sha256=man_hash,
        database_heads=database_heads,
        created_at_utc=utc_now(),
        project_name=project,
    )
    write_plan(dep_dir, plan)
    for status in event_trail:
        append_event(dep_dir, status)
    return dep_id


def release_image_ids(deploy_root: Path, revision: str) -> dict[str, str]:
    rel = release_dir(deploy_root, revision)
    return assert_release_images_present(load_manifest(manifest_path(rel)))


def seed_rollback_failed_journal(
    *,
    deploy_root: Path,
    project: str,
    target_revision: str,
    previous_revision: str,
    target_ids: dict[str, str],
    previous_ids: dict[str, str],
    man_hash: str,
    database_heads: tuple[str, ...],
) -> str:
    dep_id = seed_unresolved_journal(
        deploy_root=deploy_root,
        project=project,
        target_revision=target_revision,
        previous_revision=previous_revision,
        bootstrap=False,
        target_ids=target_ids,
        previous_ids=previous_ids,
        man_hash=man_hash,
        database_heads=database_heads,
        event_trail=(STATUS_PLANNED, STATUS_ACTIVATING_APP, STATUS_ROLLBACK_STARTED),
    )
    dep_dir = deploy_root / "deployments" / dep_id
    write_result(
        dep_dir,
        deployment_id=dep_id,
        status=STATUS_ROLLBACK_FAILED,
        messages=("FAIL: simulated automatic rollback failure",),
    )
    return dep_id


def container_labels(compose: list[str], clone: Path, service: str) -> dict[str, str]:
    cid = run(compose + ["ps", "-q", service], cwd=clone).stdout.strip()
    if not cid:
        raise RuntimeError(f"missing container for {service}")
    raw = run(
        ["docker", "inspect", cid, "--format", "{{json .Config.Labels}}"],
        cwd=clone,
    ).stdout.strip()
    parsed = json.loads(raw or "{}")
    return {str(k): str(v) for k, v in parsed.items()}


def break_gateway_nginx(clone: Path) -> None:
    nginx = clone / "deploy" / "nginx" / "nginx.conf"
    nginx.write_text(
        nginx.read_text(encoding="utf-8").replace(
            "proxy_pass http://fetchnow_api;", "return 503;"
        ),
        encoding="utf-8",
    )


def restore_gateway_nginx(clone: Path) -> None:
    nginx = clone / "deploy" / "nginx" / "nginx.conf"
    nginx.write_text(
        (ROOT / "deploy" / "nginx" / "nginx.conf").read_text(encoding="utf-8"),
        encoding="utf-8",
    )


def insert_db_sentinel(compose: list[str], clone: Path, marker: str) -> None:
    run(
        compose
        + [
            "exec",
            "-T",
            "postgres",
            "sh",
            "-c",
            'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 '
            '-c "CREATE TABLE IF NOT EXISTS rollout_it_marker (marker text primary key); '
            f"INSERT INTO rollout_it_marker(marker) VALUES ('{marker}') "
            'ON CONFLICT DO NOTHING;"',
        ],
        cwd=clone,
    )


def read_db_sentinel(compose: list[str], clone: Path, marker: str) -> str:
    return run(
        compose
        + [
            "exec",
            "-T",
            "postgres",
            "sh",
            "-c",
            'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc '
            f"\"SELECT marker FROM rollout_it_marker WHERE marker = '{marker}'\"",
        ],
        cwd=clone,
    ).stdout.strip()


def project_volumes(project: str) -> set[str]:
    proc = subprocess.run(
        [
            "docker",
            "volume",
            "ls",
            "--format",
            "{{.Name}}",
            "--filter",
            f"label={COMPOSE_PROJECT_LABEL}={project}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"failed to list project volumes: {proc.stderr.strip()}")
    return {ln.strip() for ln in proc.stdout.splitlines() if ln.strip()}


def project_networks(project: str) -> set[str]:
    proc = subprocess.run(
        [
            "docker",
            "network",
            "ls",
            "--format",
            "{{.Name}}",
            "--filter",
            f"label={COMPOSE_PROJECT_LABEL}={project}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"failed to list project networks: {proc.stderr.strip()}")
    return {ln.strip() for ln in proc.stdout.splitlines() if ln.strip()}


def container_exists(container_id: str) -> bool:
    proc = subprocess.run(
        ["docker", "inspect", container_id],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0


def running_app_container_ids(compose: list[str], clone: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for svc in APPLICATION_SERVICES:
        cid = run(compose + ["ps", "-q", svc], cwd=clone, check=False).stdout.strip()
        if cid:
            out[svc] = cid
    return out


def parse_bootstrap_cleanup_audit(messages: tuple[str, ...]) -> list[dict[str, str]]:
    prefix = "OK: bootstrap cleanup audit="
    for msg in messages:
        if msg.startswith(prefix):
            raw = json.loads(msg[len(prefix) :])
            if not isinstance(raw, list) or not raw:
                raise RuntimeError("bootstrap cleanup audit payload empty")
            return [{str(k): str(v) for k, v in item.items()} for item in raw]
    raise RuntimeError("bootstrap cleanup audit missing from rollout messages")


def journal_compose_files(
    deploy_root: Path, revision: str, deployment_id: str
) -> tuple[Path, ...]:
    rel = release_dir(deploy_root, revision)
    source = rel / SOURCE_DIRNAME
    override = (
        deploy_root / "deployments" / deployment_id / "overrides" / "images.yaml"
    )
    return (
        (source / "compose.yaml").resolve(),
        (source / "compose.staging.yaml").resolve(),
        override.resolve(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-file", type=Path, default=None)
    args = parser.parse_args(argv)

    project = make_rollout_project_name()
    assert_rollout_project(project)
    if args.project_file is not None:
        args.project_file.parent.mkdir(parents=True, exist_ok=True)
        args.project_file.write_text(project + "\n", encoding="utf-8")
        os.chmod(args.project_file, 0o600)

    clone = Path(tempfile.mkdtemp(prefix="fetchnow-rollout-clone-"))
    deploy_root = Path(tempfile.mkdtemp(prefix="fetchnow-rollout-deploy-"))
    env_dir = Path(tempfile.mkdtemp(prefix="fetchnow-rollout-env-"))
    env_file = env_dir / ".env.rollout-test"
    gateway_port = f"127.0.0.1:{18000 + secrets.randbelow(1800)}"
    policy = StabilizationPolicy(
        consecutive_successes=2, interval_seconds=1, window_seconds=20
    )
    rc = 1
    compose: list[str] = []

    try:
        run(
            ["git", "clone", "--local", "--no-hardlinks", str(ROOT), str(clone)],
            cwd=ROOT,
        )
        run(
            ["git", "config", "user.email", "rollout-integration@example.test"],
            cwd=clone,
        )
        run(["git", "config", "user.name", "Rollout Integration"], cwd=clone)
        fixture_policy_sha256 = ensure_rollout_fixture_compatibility_policy(clone)
        print("OK: rollout fixture revisions use source contract v2 with exact policy")
        write_env(
            env_file,
            project=project,
            revision=run(["git", "rev-parse", "HEAD"], cwd=clone).stdout.strip(),
            gateway_port=gateway_port,
            deploy_root=deploy_root,
        )
        compose = compose_argv(project, env_file, clone)
        print(f"OK: isolated rollout project {project}")

        run(compose + ["up", "-d", "postgres"], cwd=clone)
        wait_for_postgres(compose, clone)
        run(compose + ["run", "--rm", "api", "alembic", "upgrade", "head"], cwd=clone)
        print("OK: setup postgres healthy and Alembic upgraded outside rollout")

        marker = "prd1c3a-rollout-sentinel"
        insert_db_sentinel(compose, clone, marker)
        postgres_id = run(compose + ["ps", "-q", "postgres"], cwd=clone).stdout.strip()
        if not postgres_id:
            raise RuntimeError("postgres container ID missing")
        volumes_before = project_volumes(project)
        networks_before = project_networks(project)
        if not volumes_before:
            raise RuntimeError("expected postgres named volume before bootstrap")
        if not networks_before:
            raise RuntimeError("expected compose project network before bootstrap")
        print("OK: database sentinel inserted before bootstrap scenarios")

        break_gateway_nginx(clone)
        revision_unhealthy = commit(clone, "test: unhealthy bootstrap nginx 503")
        write_env(
            env_file,
            project=project,
            revision=revision_unhealthy,
            gateway_port=gateway_port,
            deploy_root=deploy_root,
        )
        prepare(
            project=project,
            env_file=env_file,
            revision=revision_unhealthy,
            clone=clone,
            deploy_root=deploy_root,
        )
        assert_prepared_release_v2_policy(
            deploy_root, revision_unhealthy, fixture_policy_sha256=fixture_policy_sha256
        )
        if running_app_container_ids(compose, clone):
            raise RuntimeError("application containers present before unhealthy bootstrap")

        bad_boot = rollout_release(
            RolloutInput(
                project_name=project,
                env_file=env_file,
                expected_revision=revision_unhealthy,
                repo_root=clone,
                deploy_root=deploy_root,
                gateway_base_url=f"http://{gateway_port}",
                bootstrap=True,
                policy=policy,
            )
        )
        if bad_boot.ok:
            raise RuntimeError(
                f"unhealthy bootstrap unexpectedly succeeded: {bad_boot.messages}"
            )
        if bad_boot.status != STATUS_FAILED:
            raise RuntimeError(
                f"unhealthy bootstrap did not publish failed result: {bad_boot.messages}"
            )
        if not bad_boot.deployment_id:
            raise RuntimeError("unhealthy bootstrap missing deployment_id")

        audit = parse_bootstrap_cleanup_audit(bad_boot.messages)
        if len(audit) < len(APPLICATION_SERVICES):
            raise RuntimeError(
                "unhealthy bootstrap did not create all application containers "
                f"before health failure (audit removed {len(audit)})"
            )
        print(
            "OK: unhealthy bootstrap created transaction-owned app containers "
            f"({len(audit)} removed)"
        )

        audit_ids = {entry["container_id"] for entry in audit}
        for entry in audit:
            if entry["compose_project"] != project:
                raise RuntimeError("removed container compose project label mismatch")
            if entry["compose_service"] not in APPLICATION_SERVICES:
                raise RuntimeError("removed container service label not allowed")
            if entry["deployment_id"] != bad_boot.deployment_id:
                raise RuntimeError("removed container deployment-id label mismatch")
            if entry["release_revision"] != revision_unhealthy:
                raise RuntimeError("removed container release-revision label mismatch")
        print(
            "OK: ownership labels matched deployment and revision for removed containers"
        )
        for cid in audit_ids:
            if container_exists(cid):
                raise RuntimeError(
                    f"bootstrap cleanup left removed container present: {cid}"
                )
        print(
            "OK: exact owned container IDs removed "
            f"({', '.join(sorted(audit_ids))})"
        )

        if running_app_container_ids(compose, clone):
            raise RuntimeError("application containers remain after bootstrap cleanup")
        remaining_owned = discover_owned_bootstrap_containers(
            project_name=project,
            env_file=env_file,
            compose_files=journal_compose_files(
                deploy_root, revision_unhealthy, bad_boot.deployment_id
            ),
            cwd=release_dir(deploy_root, revision_unhealthy) / SOURCE_DIRNAME,
            deployment_id=bad_boot.deployment_id,
            release_revision=revision_unhealthy,
        )
        if remaining_owned:
            raise RuntimeError("discover_owned found bootstrap containers after cleanup")
        if run(compose + ["ps", "-q", "postgres"], cwd=clone).stdout.strip() != postgres_id:
            raise RuntimeError("bootstrap cleanup recreated postgres container")
        wait_for_postgres(compose, clone)
        if read_db_sentinel(compose, clone, marker) != marker:
            raise RuntimeError("database sentinel changed during unhealthy bootstrap")
        print("OK: postgres container ID and database sentinel preserved")

        if project_volumes(project) != volumes_before:
            raise RuntimeError("named volumes changed during unhealthy bootstrap")
        if project_networks(project) != networks_before:
            raise RuntimeError("project network changed during unhealthy bootstrap")
        print("OK: network and named volumes preserved")

        current_path = deploy_root / "state" / "current.json"
        if current_path.exists():
            raise RuntimeError("failed bootstrap published current.json")
        print("OK: failed bootstrap published no current state")

        bad_dep = deploy_root / "deployments" / bad_boot.deployment_id
        bad_result = json.loads((bad_dep / "result.json").read_text(encoding="utf-8"))
        if bad_result.get("status") != STATUS_FAILED:
            raise RuntimeError("bootstrap journal result is not failed")
        if not any(
            str(m).startswith("OK: bootstrap cleanup audit=")
            for m in bad_result.get("messages", [])
        ):
            raise RuntimeError("bootstrap result missing cleanup audit after success")
        print("OK: failed bootstrap cleanup completed with terminal failed result")

        if find_unresolved_deployments(deploy_root):
            raise RuntimeError(
                "terminal failed bootstrap blocks subsequent rollout unexpectedly"
            )

        restore_gateway_nginx(clone)
        revision_a = commit(clone, "test: rollout integration revision A healthy")
        write_env(
            env_file,
            project=project,
            revision=revision_a,
            gateway_port=gateway_port,
            deploy_root=deploy_root,
        )
        prepare(
            project=project,
            env_file=env_file,
            revision=revision_a,
            clone=clone,
            deploy_root=deploy_root,
        )
        assert_prepared_release_v2_policy(
            deploy_root, revision_a, fixture_policy_sha256=fixture_policy_sha256
        )
        boot = rollout_release(
            RolloutInput(
                project_name=project,
                env_file=env_file,
                expected_revision=revision_a,
                repo_root=clone,
                deploy_root=deploy_root,
                gateway_base_url=f"http://{gateway_port}",
                bootstrap=True,
                policy=policy,
            )
        )
        if not boot.ok or boot.status != "committed":
            raise RuntimeError(
                f"healthy bootstrap after terminal failure failed: {boot.messages}"
            )
        api_labels = container_labels(compose, clone, "api")
        if api_labels.get(LABEL_DEPLOYMENT_ID) != boot.deployment_id:
            raise RuntimeError("bootstrap api container missing deployment-id label")
        if api_labels.get(LABEL_RELEASE_REVISION) != revision_a:
            raise RuntimeError("bootstrap api container missing release-revision label")
        print("OK: healthy bootstrap after terminal failure succeeded with ownership labels")

        readme = clone / "README.md"
        readme.write_text(
            readme.read_text(encoding="utf-8") + "\n<!-- rollout integration B -->\n",
            encoding="utf-8",
        )
        revision_b = commit(clone, "test: rollout integration revision B")
        write_env(
            env_file,
            project=project,
            revision=revision_b,
            gateway_port=gateway_port,
            deploy_root=deploy_root,
        )
        prepare(
            project=project,
            env_file=env_file,
            revision=revision_b,
            clone=clone,
            deploy_root=deploy_root,
        )
        assert_prepared_release_v2_policy(
            deploy_root, revision_b, fixture_policy_sha256=fixture_policy_sha256
        )
        deployed_b = rollout_release(
            RolloutInput(
                project_name=project,
                env_file=env_file,
                expected_revision=revision_b,
                repo_root=clone,
                deploy_root=deploy_root,
                gateway_base_url=f"http://{gateway_port}",
                policy=policy,
            )
        )
        if not deployed_b.ok or current_revision(deploy_root) != revision_b:
            raise RuntimeError(f"revision B did not commit: {deployed_b.messages}")
        print("OK: revision B committed and current.json points to B")
        b_image_ids = container_image_ids(compose, clone)
        b_manifest_ids = release_image_ids(deploy_root, revision_b)

        if (
            run(compose + ["ps", "-q", "postgres"], cwd=clone).stdout.strip()
            != postgres_id
        ):
            raise RuntimeError("rollout recreated postgres before revision C")
        if read_db_sentinel(compose, clone, marker) != marker:
            raise RuntimeError("database sentinel missing before revision C")

        break_gateway_nginx(clone)
        revision_c = commit(clone, "test: break gateway health for rollback")
        write_env(
            env_file,
            project=project,
            revision=revision_c,
            gateway_port=gateway_port,
            deploy_root=deploy_root,
        )
        prepare(
            project=project,
            env_file=env_file,
            revision=revision_c,
            clone=clone,
            deploy_root=deploy_root,
        )
        assert_prepared_release_v2_policy(
            deploy_root, revision_c, fixture_policy_sha256=fixture_policy_sha256
        )
        failed_c = rollout_release(
            RolloutInput(
                project_name=project,
                env_file=env_file,
                expected_revision=revision_c,
                repo_root=clone,
                deploy_root=deploy_root,
                gateway_base_url=f"http://{gateway_port}",
                policy=policy,
            )
        )
        if failed_c.ok or failed_c.status != "rolled_back":
            raise RuntimeError(f"revision C did not roll back: {failed_c.messages}")
        if current_revision(deploy_root) != revision_b:
            raise RuntimeError("rollback changed current.json away from B")
        if (
            run(compose + ["ps", "-q", "postgres"], cwd=clone).stdout.strip()
            != postgres_id
        ):
            raise RuntimeError("rollout recreated the postgres container")
        sentinel = read_db_sentinel(compose, clone, marker)
        if sentinel != marker:
            raise RuntimeError("database sentinel changed during rollout")
        after_rollback_ids = container_image_ids(compose, clone)
        for svc in ("api", "worker", "web", "gateway"):
            if after_rollback_ids[svc] != b_image_ids[svc]:
                raise RuntimeError(
                    f"rollback did not restore {svc} container image ID "
                    f"(got {after_rollback_ids[svc]!r}, want {b_image_ids[svc]!r})"
                )
        print(
            "OK: revision C rolled_back; postgres ID, sentinel, and B image IDs unchanged"
        )

        b_release = release_dir(deploy_root, revision_b)
        b_heads = tuple(
            sorted(target_heads_from_release_source(b_release))
        )
        accept_dep = seed_unresolved_journal(
            deploy_root=deploy_root,
            project=project,
            target_revision=revision_b,
            previous_revision=revision_a,
            bootstrap=False,
            target_ids=b_manifest_ids,
            previous_ids=release_image_ids(deploy_root, revision_a),
            man_hash=sha256_file(manifest_path(b_release)),
            database_heads=b_heads,
            event_trail=(
                STATUS_PLANNED,
                STATUS_ACTIVATING_APP,
                STATUS_APP_HEALTHY,
                STATUS_ACTIVATING_GATEWAY,
                STATUS_STABILIZING,
            ),
        )
        accept = recover_deployment(
            RecoverInput(
                project_name=project,
                env_file=env_file,
                deploy_root=deploy_root,
                repo_root=clone,
                deployment_id=accept_dep,
                action="accept-target",
                gateway_base_url=f"http://{gateway_port}",
                policy=policy,
            )
        )
        if not accept.ok:
            raise RuntimeError(f"recover accept-target failed: {accept.messages}")
        accept_result = json.loads(
            (deploy_root / "deployments" / accept_dep / "result.json").read_text(
                encoding="utf-8"
            )
        )
        if accept_result.get("status") != "committed":
            raise RuntimeError("recover accept-target did not publish committed result")
        print("OK: recover accept-target resolved simulated crash journal")

        failed_dep = seed_rollback_failed_journal(
            deploy_root=deploy_root,
            project=project,
            target_revision=revision_c,
            previous_revision=revision_b,
            target_ids=release_image_ids(deploy_root, revision_c),
            previous_ids=b_manifest_ids,
            man_hash=sha256_file(manifest_path(release_dir(deploy_root, revision_c))),
            database_heads=b_heads,
        )
        failed_result_path = deploy_root / "deployments" / failed_dep / "result.json"
        failed_result_before = failed_result_path.read_bytes()
        failed_result_mtime = failed_result_path.stat().st_mtime_ns
        failed_recover = recover_deployment(
            RecoverInput(
                project_name=project,
                env_file=env_file,
                deploy_root=deploy_root,
                repo_root=clone,
                deployment_id=failed_dep,
                action="rollback",
                gateway_base_url=f"http://{gateway_port}",
                policy=policy,
            )
        )
        if not failed_recover.ok or not failed_recover.recovery_id:
            raise RuntimeError(
                f"recover rollback_failed failed: {failed_recover.messages}"
            )
        if failed_result_path.read_bytes() != failed_result_before:
            raise RuntimeError("recover mutated original rollback_failed result bytes")
        if failed_result_path.stat().st_mtime_ns != failed_result_mtime:
            raise RuntimeError("recover touched original rollback_failed result mtime")
        recovery_dir_path = (
            deploy_root
            / "deployments"
            / failed_dep
            / "recoveries"
            / failed_recover.recovery_id
        )
        if not (recovery_dir_path / "result.json").is_file():
            raise RuntimeError("recovery result.json missing")
        if not (deploy_root / "deployments" / failed_dep / "resolution.json").is_file():
            raise RuntimeError("resolution.json missing after recovery")
        print(
            "OK: recover rollback_failed preserved immutable result and wrote resolution"
        )

        c_release = release_dir(deploy_root, revision_c)
        c_manifest_ids = release_image_ids(deploy_root, revision_c)
        rollback_dep = seed_unresolved_journal(
            deploy_root=deploy_root,
            project=project,
            target_revision=revision_c,
            previous_revision=revision_b,
            bootstrap=False,
            target_ids=c_manifest_ids,
            previous_ids=b_manifest_ids,
            man_hash=sha256_file(manifest_path(c_release)),
            database_heads=b_heads,
            event_trail=(STATUS_PLANNED, STATUS_ACTIVATING_APP, STATUS_ROLLBACK_STARTED),
        )
        rollback_recover = recover_deployment(
            RecoverInput(
                project_name=project,
                env_file=env_file,
                deploy_root=deploy_root,
                repo_root=clone,
                deployment_id=rollback_dep,
                action="rollback",
                gateway_base_url=f"http://{gateway_port}",
                policy=policy,
            )
        )
        if not rollback_recover.ok:
            raise RuntimeError(f"recover rollback failed: {rollback_recover.messages}")
        rollback_result = json.loads(
            (deploy_root / "deployments" / rollback_dep / "result.json").read_text(
                encoding="utf-8"
            )
        )
        if rollback_result.get("status") != "rolled_back":
            raise RuntimeError("recover rollback did not publish rolled_back result")
        if current_revision(deploy_root) != revision_b:
            raise RuntimeError("recover rollback changed current.json away from B")
        print("OK: recover rollback restored previous release B")

        terminal_recover = recover_deployment(
            RecoverInput(
                project_name=project,
                env_file=env_file,
                deploy_root=deploy_root,
                repo_root=clone,
                deployment_id=str(deployed_b.deployment_id),
                action="accept-target",
                gateway_base_url=f"http://{gateway_port}",
                policy=policy,
            )
        )
        if terminal_recover.ok:
            raise RuntimeError("recover accepted already-committed deployment journal")
        print("OK: recover rejected terminal committed journal")

        bad_recover = recover_deployment(
            RecoverInput(
                project_name=project,
                env_file=env_file,
                deploy_root=deploy_root,
                repo_root=clone,
                deployment_id="12345678-1234-1234-1234-123456789abc",
                action="rollback",
                gateway_base_url=f"http://{gateway_port}",
                policy=policy,
            )
        )
        if bad_recover.ok:
            raise RuntimeError("recover accepted foreign deployment id")
        print("OK: recover rejected foreign deployment journal")

        try:
            assert_heads_equal(
                database_heads=frozenset({"baseline"}),
                target_heads=frozenset({"extra_fake_head"}),
            )
        except DbHeadsError:
            print("OK: migration-head mismatch gate rejected divergent heads")
        else:
            raise RuntimeError("migration-head mismatch gate did not fail")

        active_before = (deploy_root / "state" / "current.json").read_bytes()
        active_mtime = (deploy_root / "state" / "current.json").stat().st_mtime_ns
        dep_dirs_before = {
            p.name
            for p in (deploy_root / "deployments").iterdir()
            if p.is_dir()
        }
        container_ids_before = {
            svc: run(compose + ["ps", "-q", svc], cwd=clone).stdout.strip()
            for svc in ("api", "worker", "web", "gateway", "postgres")
        }
        again_b = rollout_release(
            RolloutInput(
                project_name=project,
                env_file=env_file,
                expected_revision=revision_b,
                repo_root=clone,
                deploy_root=deploy_root,
                gateway_base_url=f"http://{gateway_port}",
                policy=policy,
            )
        )
        if not again_b.ok or not again_b.already_active:
            raise RuntimeError(
                f"already-active B did not remain idempotent: {again_b.messages}"
            )
        if (deploy_root / "state" / "current.json").read_bytes() != active_before:
            raise RuntimeError("already-active rollout mutated current.json")
        if (
            (deploy_root / "state" / "current.json").stat().st_mtime_ns
            != active_mtime
        ):
            raise RuntimeError("already-active rollout touched current.json mtime")
        dep_dirs_after = {
            p.name
            for p in (deploy_root / "deployments").iterdir()
            if p.is_dir()
        }
        if dep_dirs_after != dep_dirs_before:
            raise RuntimeError("already-active rollout created new deployment journal")
        for svc, cid in container_ids_before.items():
            after = run(compose + ["ps", "-q", svc], cwd=clone).stdout.strip()
            if after != cid:
                raise RuntimeError(
                    f"already-active rollout recreated {svc} container"
                )
        print("OK: already-active B verified without mutation")

        for journal in (deploy_root / "deployments").rglob("*"):
            if journal.is_file():
                text = journal.read_text(encoding="utf-8")
                if (
                    "POSTGRES_PASSWORD" in text
                    or "DATABASE_URL" in text
                    or valid_test_password() in text
                ):
                    raise RuntimeError(
                        f"secret pattern appeared in journal: {journal.name}"
                    )
        print("OK: journals contain no password or DATABASE_URL patterns")
        rc = 0
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    finally:
        if compose:
            assert_rollout_project(project)
            run(compose + ["down", "-v", "--remove-orphans"], cwd=clone, check=False)
            print(f"OK: isolated Compose project cleanup completed ({project})")
        shutil.rmtree(clone, ignore_errors=True)
        shutil.rmtree(deploy_root, ignore_errors=True)
        shutil.rmtree(env_dir, ignore_errors=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
