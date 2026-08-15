#!/usr/bin/env python3
"""Isolated PRD1C3B2B2 verified migration transaction Docker integration test."""

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

from fetchnow_release.c2_constants import SOURCE_DIRNAME  # noqa: E402
from fetchnow_release.c3b1_constants import COMPATIBILITY_REL_PATH  # noqa: E402
from fetchnow_release.c3b2b2_constants import (  # noqa: E402
    HOLD_RELEASED_NAME,
    MIGRATION_EVENTS_DIRNAME,
    STATUS_MIGRATION_APPLICATION_STABILIZED,
    STATUS_MIGRATION_BACKUP_VERIFIED,
    STATUS_MIGRATION_COMMITTED,
    STATUS_MIGRATION_COMMAND_COMPLETED,
    STATUS_MIGRATION_HOLD_ACQUIRED,
    STATUS_MIGRATION_STATE_COMMITTED,
    STATUS_MIGRATION_TARGET_HEADS_VERIFIED,
)
from fetchnow_release.db_heads import (  # noqa: E402
    database_heads_via_postgres,
    target_heads_from_release_source,
)
from fetchnow_release.deploy_root import release_dir  # noqa: E402
from fetchnow_release.migrate import MigrationInput, run_migration  # noqa: E402
from fetchnow_release.migration_compatibility import load_compatibility_contract  # noqa: E402
from fetchnow_release.migration_journal import (  # noqa: E402
    find_unresolved_migrations,
    load_migration_result,
    migration_dir,
    migrations_dir,
)
from fetchnow_release.migration_project import (  # noqa: E402
    MigrationProjectError,
    assert_migration_project,
    make_migration_project_name,
)
from fetchnow_release.prepare import PrepareInput, prepare_release  # noqa: E402
from fetchnow_release.diagnostics import format_rollout_failure_diagnostics  # noqa: E402
from fetchnow_release.rollout import RolloutInput, rollout_release  # noqa: E402
from fetchnow_release.stabilize import StabilizationPolicy  # noqa: E402
from password_fixture import valid_test_password  # noqa: E402

FIXTURE_POLICY_PATH = ROOT / COMPATIBILITY_REL_PATH

MIGRATION_FILE = """\"\"\"Online expand migration for migration integration.\"\"\"

revision = "0007_expand"
down_revision = "0006_browser_delivery_grants"


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
"""

CONTRACT_WITH_MIGRATION = """{
  "schema_version": 1,
  "transitions": [
    {
      "from_heads": ["0006_browser_delivery_grants"],
      "to_heads": ["0007_expand"],
      "included_revisions": ["0007_expand"],
      "execution_mode": "online_expand",
      "online_with_previous_application": true,
      "previous_application_rollback_compatible": true,
      "data_loss": "none",
      "database_downgrade": "forbidden",
      "rationale": "Add nullable expand column while previous application remains compatible."
    }
  ]
}
"""


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
    run(["git", "config", "--local", "user.name", "FetchNow Migration Integration"], cwd=clone)
    run(
        ["git", "config", "--local", "user.email", "migration-integration@fetchnow.invalid"],
        cwd=clone,
    )
    run(["git", "config", "--local", "commit.gpgsign", "false"], cwd=clone)


def ensure_fixture_compatibility_policy(clone: Path) -> None:
    if not FIXTURE_POLICY_PATH.is_file():
        raise RuntimeError(f"repository fixture policy missing: {FIXTURE_POLICY_PATH}")
    load_compatibility_contract(FIXTURE_POLICY_PATH)
    fixture_bytes = FIXTURE_POLICY_PATH.read_bytes()
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
            ["git", "commit", "-m", "test: add migration compatibility contract"],
            cwd=clone,
        )
        revision = run(["git", "rev-parse", "HEAD"], cwd=clone).stdout.strip()
        run(["git", "update-ref", "refs/remotes/origin/main", revision], cwd=clone)


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


def insert_sentinel(compose: list[str], clone: Path, marker: str) -> None:
    run(
        compose
        + [
            "exec",
            "-T",
            "postgres",
            "sh",
            "-c",
            'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 '
            '-c "CREATE TABLE IF NOT EXISTS migration_it_marker (marker text primary key); '
            f"INSERT INTO migration_it_marker(marker) VALUES ('{marker}') "
            'ON CONFLICT DO NOTHING;"',
        ],
        cwd=clone,
    )


def read_sentinel(compose: list[str], clone: Path, marker: str) -> str:
    return run(
        compose
        + [
            "exec",
            "-T",
            "postgres",
            "sh",
            "-c",
            'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc '
            f"\"SELECT marker FROM migration_it_marker WHERE marker = '{marker}'\"",
        ],
        cwd=clone,
    ).stdout.strip()


def container_ids(compose: list[str], clone: Path) -> dict[str, str]:
    ids: dict[str, str] = {}
    for svc in ("api", "worker", "delivery", "web", "gateway", "postgres"):
        cid = run(compose + ["ps", "-q", svc], cwd=clone).stdout.strip()
        if not cid:
            raise RuntimeError(f"missing running container for {svc}")
        ids[svc] = cid
    return ids


def migration_event_statuses(mig_dir: Path) -> tuple[str, ...]:
    events = mig_dir / MIGRATION_EVENTS_DIRNAME
    if not events.is_dir():
        return ()
    statuses: list[str] = []
    for path in sorted(events.glob("*.json")):
        statuses.append(str(json.loads(path.read_text(encoding="utf-8"))["status"]))
    return tuple(statuses)


def backup_ids(backup_root: Path) -> set[str]:
    if not backup_root.is_dir():
        return set()
    return {
        p.name
        for p in backup_root.iterdir()
        if p.is_dir() and not p.is_symlink() and p.name not in {"holds", ".lock"}
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-file",
        type=Path,
        help="Optional path to write the exact per-run project name for CI cleanup",
    )
    args = parser.parse_args(argv)

    project = make_migration_project_name()
    assert_migration_project(project)
    if args.project_file is not None:
        args.project_file.parent.mkdir(parents=True, exist_ok=True)
        args.project_file.write_text(project + "\n", encoding="utf-8")
        os.chmod(args.project_file, 0o600)
        print(f"Wrote project file {args.project_file} -> {project}")

    clone = Path(tempfile.mkdtemp(prefix="fetchnow-migration-clone-"))
    deploy_root = Path(tempfile.mkdtemp(prefix="fetchnow-migration-deploy-"))
    backup_root = Path(tempfile.mkdtemp(prefix="fetchnow-migration-backup-"))
    env_dir = Path(tempfile.mkdtemp(prefix="fetchnow-migration-env-"))
    os.chmod(deploy_root, 0o750)
    os.chmod(backup_root, 0o700)
    env_file = env_dir / ".env.migration-test"
    gateway_port = f"127.0.0.1:{21000 + secrets.randbelow(1000)}"
    marker = f"migration-it-{secrets.token_hex(4)}"
    policy = StabilizationPolicy(
        consecutive_successes=2, interval_seconds=1, window_seconds=20
    )
    rc = 1
    cleanup_done = False
    compose: list[str] = []

    try:
        run(
            ["git", "clone", "--local", "--no-hardlinks", str(ROOT), str(clone)],
            cwd=ROOT,
        )
        configure_fixture_git_identity(clone)
        ensure_fixture_compatibility_policy(clone)
        baseline_rev = run(["git", "rev-parse", "HEAD"], cwd=clone).stdout.strip()
        run(["git", "update-ref", "refs/remotes/origin/main", baseline_rev], cwd=clone)

        write_env(
            env_file,
            project=project,
            revision=baseline_rev,
            gateway_port=gateway_port,
            deploy_root=deploy_root,
            backup_root=backup_root,
        )
        compose = compose_argv(project, env_file, clone)
        assert_passed(project.startswith("fetchnow-migration-test-"), "isolated project name")

        run(compose + ["build", "api"], cwd=clone)
        run(compose + ["up", "-d", "postgres"], cwd=clone)
        wait_for_postgres(compose, clone)
        run(compose + ["run", "--rm", "api", "alembic", "upgrade", "head"], cwd=clone)
        insert_sentinel(compose, clone, marker)
        assert_passed(read_sentinel(compose, clone, marker) == marker, "postgres sentinel inserted")

        prepare(
            project=project,
            env_file=env_file,
            revision=baseline_rev,
            clone=clone,
            deploy_root=deploy_root,
        )
        assert_passed(
            release_dir(deploy_root, baseline_rev).is_dir(),
            "baseline release prepared",
        )

        boot = rollout_release(
            RolloutInput(
                project_name=project,
                env_file=env_file,
                expected_revision=baseline_rev,
                repo_root=clone,
                deploy_root=deploy_root,
                gateway_base_url=f"http://{gateway_port}",
                bootstrap=True,
                policy=policy,
            )
        )
        boot_diag = format_rollout_failure_diagnostics(
            status=boot.status,
            messages=boot.messages,
            deployment_id=boot.deployment_id,
            deploy_root=deploy_root,
        )
        assert_passed(
            boot.ok and boot.status == "committed",
            "baseline bootstrap rollout committed",
            diagnostics=boot_diag,
        )
        assert_passed(
            "OK: storage-init completed" in boot.messages,
            "PR9 bootstrap ran storage-init before commit",
            diagnostics=boot_diag,
        )

        current_path = deploy_root / "state" / "current.json"
        baseline_state = json.loads(current_path.read_text(encoding="utf-8"))
        assert_passed(
            baseline_state.get("schema_version") == 2
            and baseline_state["application"]["revision"] == baseline_rev,
            "bootstrap published current.json schema v2 at baseline",
        )

        mig_versions = clone / "backend" / "migrations" / "versions"
        (mig_versions / "0007_expand.py").write_text(MIGRATION_FILE, encoding="utf-8")
        contract_path = clone / COMPATIBILITY_REL_PATH
        contract_path.write_text(CONTRACT_WITH_MIGRATION, encoding="utf-8")
        run(
            ["git", "add", "backend/migrations/versions/0007_expand.py", str(contract_path)],
            cwd=clone,
        )
        target_rev = commit(clone, "test: forward migration target release")
        write_env(
            env_file,
            project=project,
            revision=target_rev,
            gateway_port=gateway_port,
            deploy_root=deploy_root,
            backup_root=backup_root,
        )
        prepare(
            project=project,
            env_file=env_file,
            revision=target_rev,
            clone=clone,
            deploy_root=deploy_root,
        )
        target_release = release_dir(deploy_root, target_rev)
        target_heads = target_heads_from_release_source(target_release)
        assert_passed(
            "0007_expand" in target_heads,
            "target release exposes forward migration head",
        )

        baseline_compose = release_compose_files(deploy_root, baseline_rev)
        baseline_source = release_dir(deploy_root, baseline_rev) / SOURCE_DIRNAME
        backups_before = backup_ids(backup_root)
        container_ids_before = container_ids(compose, clone)
        postgres_id_before = container_ids_before["postgres"]
        migrated = run_migration(
            MigrationInput(
                project_name=project,
                env_file=env_file,
                compose_files=baseline_compose,
                expected_revision=target_rev,
                repo_root=clone,
                deploy_root=deploy_root,
                backup_root=backup_root,
                gateway_base_url=f"http://{gateway_port}",
            )
        )
        assert_passed(
            migrated.ok
            and not migrated.already_migrated
            and migrated.status == STATUS_MIGRATION_COMMITTED
            and migrated.migration_id is not None,
            "forward migration transaction committed",
        )
        migration_id = migrated.migration_id
        assert migration_id is not None
        mig_dirs_after_first = {
            p.name for p in migrations_dir(deploy_root).iterdir() if p.is_dir()
        }

        mig_dir = migration_dir(deploy_root, migration_id)
        event_statuses = migration_event_statuses(mig_dir)
        for required in (
            STATUS_MIGRATION_BACKUP_VERIFIED,
            STATUS_MIGRATION_HOLD_ACQUIRED,
            STATUS_MIGRATION_COMMAND_COMPLETED,
            STATUS_MIGRATION_TARGET_HEADS_VERIFIED,
            STATUS_MIGRATION_APPLICATION_STABILIZED,
            STATUS_MIGRATION_STATE_COMMITTED,
            STATUS_MIGRATION_COMMITTED,
        ):
            assert_passed(required in event_statuses, f"migration journal event {required}")

        result_doc = load_migration_result(mig_dir)
        assert_passed(
            result_doc is not None and result_doc.status == STATUS_MIGRATION_COMMITTED,
            "migration result.json terminal committed",
        )

        backups_after = backup_ids(backup_root)
        new_backups = backups_after - backups_before
        assert_passed(len(new_backups) == 1, "verified backup created under backup root")
        backup_id = next(iter(new_backups))
        assert_passed(
            (backup_root / backup_id / "database.dump").is_file(),
            "backup dump artifact present",
        )

        hold_dirs = list((backup_root / "holds").glob("*")) if (backup_root / "holds").is_dir() else []
        released_hold = any(
            (hold / HOLD_RELEASED_NAME).is_file()
            for hold in hold_dirs
            if hold.is_dir()
        )
        assert_passed(released_hold, "retention hold released after migration commit")

        live_heads = database_heads_via_postgres(
            project_name=project,
            env_file=env_file,
            compose_files=baseline_compose,
            cwd=baseline_source,
        )
        assert_passed(
            frozenset(live_heads) == target_heads,
            "live postgres alembic heads match target release",
        )

        after_state = json.loads(current_path.read_text(encoding="utf-8"))
        assert_passed(
            after_state["application"]["revision"] == baseline_rev
            and after_state["database"]["schema_release_revision"] == target_rev
            and after_state["database"]["last_migration_id"] == migration_id
            and "0007_expand" in after_state["database"]["heads"],
            "current.json database state committed after migration",
        )
        assert_passed(
            find_unresolved_migrations(deploy_root) == [],
            "committed migration journal does not block follow-up",
        )
        assert_passed(
            container_ids(compose, clone) == container_ids_before,
            "application and postgres container IDs unchanged during migration",
        )
        assert_passed(
            run(compose + ["ps", "-q", "postgres"], cwd=clone).stdout.strip()
            == postgres_id_before,
            "postgres container ID preserved",
        )
        assert_passed(
            read_sentinel(compose, clone, marker) == marker,
            "database sentinel preserved through migration",
        )
        current_bytes_after = current_path.read_bytes()
        current_mtime_after = current_path.stat().st_mtime_ns

        again = run_migration(
            MigrationInput(
                project_name=project,
                env_file=env_file,
                compose_files=baseline_compose,
                expected_revision=target_rev,
                repo_root=clone,
                deploy_root=deploy_root,
                backup_root=backup_root,
                gateway_base_url=f"http://{gateway_port}",
            )
        )
        assert_passed(
            again.ok and again.already_migrated and again.status == STATUS_MIGRATION_COMMITTED,
            "repeat migration is idempotent already_migrated",
        )
        assert_passed(
            current_path.read_bytes() == current_bytes_after,
            "current.json bytes stable on idempotent migration",
        )
        assert_passed(
            current_path.stat().st_mtime_ns == current_mtime_after,
            "current.json mtime stable on idempotent migration",
        )
        mig_dirs_after = {p.name for p in migrations_dir(deploy_root).iterdir() if p.is_dir()}
        assert_passed(
            mig_dirs_after == mig_dirs_after_first,
            "idempotent migration created no new journal",
        )

        from fetchnow_release.migration_recover import (  # noqa: E402
            MigrationRecoverInput,
            recover_migration,
        )
        from fetchnow_release.c3b2b2_constants import (  # noqa: E402
            MIGRATION_RECOVERY_OUTCOME_ACCEPT_SOURCE,
            STATUS_MIGRATION_REQUIRES_RECOVERY,
        )

        BROKEN_MIGRATION = """\"\"\"Intentional failing migration for recovery integration.\"\"\"

revision = "0008_broken"
down_revision = "0007_expand"


def upgrade() -> None:
    raise RuntimeError("intentional migration integration failure")


def downgrade() -> None:
    pass
"""
        CONTRACT_WITH_BROKEN = """{
  "schema_version": 1,
  "transitions": [
    {
      "from_heads": ["0006_browser_delivery_grants"],
      "to_heads": ["0007_expand"],
      "included_revisions": ["0007_expand"],
      "execution_mode": "online_expand",
      "online_with_previous_application": true,
      "previous_application_rollback_compatible": true,
      "data_loss": "none",
      "database_downgrade": "forbidden",
      "rationale": "Expand the PR14 database head to the synthetic test head."
    },
    {
      "from_heads": ["0007_expand"],
      "to_heads": ["0008_broken"],
      "included_revisions": ["0008_broken"],
      "execution_mode": "online_expand",
      "online_with_previous_application": true,
      "previous_application_rollback_compatible": true,
      "data_loss": "none",
      "database_downgrade": "forbidden",
      "rationale": "Deliberate failing migration for recovery integration."
    }
  ]
}
"""
        (mig_versions / "0008_broken.py").write_text(BROKEN_MIGRATION, encoding="utf-8")
        contract_path.write_text(CONTRACT_WITH_BROKEN, encoding="utf-8")
        run(
            [
                "git",
                "add",
                "backend/migrations/versions/0008_broken.py",
                str(contract_path),
            ],
            cwd=clone,
        )
        fail_target_rev = commit(clone, "test: failing migration target release")
        write_env(
            env_file,
            project=project,
            revision=fail_target_rev,
            gateway_port=gateway_port,
            deploy_root=deploy_root,
            backup_root=backup_root,
        )
        prepare(
            project=project,
            env_file=env_file,
            revision=fail_target_rev,
            clone=clone,
            deploy_root=deploy_root,
        )
        failed = run_migration(
            MigrationInput(
                project_name=project,
                env_file=env_file,
                compose_files=baseline_compose,
                expected_revision=fail_target_rev,
                repo_root=clone,
                deploy_root=deploy_root,
                backup_root=backup_root,
                gateway_base_url=f"http://{gateway_port}",
            )
        )
        assert_passed(
            not failed.ok and failed.status == STATUS_MIGRATION_REQUIRES_RECOVERY,
            "post-mutation alembic failure enters migration_requires_recovery",
        )
        assert failed.migration_id is not None
        fail_mig_dir = migration_dir(deploy_root, failed.migration_id)
        fail_result = load_migration_result(fail_mig_dir)
        assert_passed(
            fail_result is not None
            and fail_result.status == STATUS_MIGRATION_REQUIRES_RECOVERY,
            "failed migration journal result is migration_requires_recovery",
        )
        write_env(
            env_file,
            project=project,
            revision=baseline_rev,
            gateway_port=gateway_port,
            deploy_root=deploy_root,
            backup_root=backup_root,
        )
        assert_passed(
            failed.migration_id in find_unresolved_migrations(deploy_root),
            "failed migration journal blocks mutation",
        )
        blocked = rollout_release(
            RolloutInput(
                project_name=project,
                env_file=env_file,
                expected_revision=baseline_rev,
                repo_root=clone,
                deploy_root=deploy_root,
                gateway_base_url=f"http://{gateway_port}",
                policy=policy,
            )
        )
        assert_passed(
            not blocked.ok,
            f"rollout refused while migration unresolved: {blocked.messages[-1]}",
        )
        assert_passed(
            any("unresolved migration journal" in m for m in blocked.messages),
            "rollout message cites unresolved migration journal",
        )
        recovered = recover_migration(
            MigrationRecoverInput(
                project_name=project,
                env_file=env_file,
                compose_files=baseline_compose,
                repo_root=clone,
                deploy_root=deploy_root,
                backup_root=backup_root,
                migration_id=failed.migration_id,
                action=MIGRATION_RECOVERY_OUTCOME_ACCEPT_SOURCE,
                gateway_base_url=f"http://{gateway_port}",
            )
        )
        assert_passed(recovered.ok, "accept-source recovery after alembic failure")
        assert_passed(
            find_unresolved_migrations(deploy_root) == [],
            "accept-source resolution clears migration block",
        )

        print(f"project={project}")
        print(f"migration_id={migration_id}")
        print(f"backup_id={backup_id}")
        print(f"baseline_revision={baseline_rev}")
        print(f"target_revision={target_rev}")
        rc = 0
    except (MigrationProjectError, Exception):  # noqa: BLE001
        traceback.print_exc()
        rc = 1
    finally:
        try:
            assert_migration_project(project)
            if compose and clone.is_dir() and (clone / "compose.yaml").is_file():
                down = run(
                    compose + ["down", "-v", "--remove-orphans"],
                    cwd=clone,
                    check=False,
                )
                if down.returncode != 0:
                    raise RuntimeError(
                        f"cleanup down -v failed: {down.stderr.strip() or down.stdout.strip()}"
                    )
                cleanup_done = True
                print(f"OK: isolated Compose project cleanup completed ({project})")
        except Exception as cleanup_exc:  # noqa: BLE001
            print(f"ERROR: {cleanup_exc}", file=sys.stderr)
            cleanup_done = False
            rc = 1
        shutil.rmtree(clone, ignore_errors=True)
        shutil.rmtree(deploy_root, ignore_errors=True)
        shutil.rmtree(backup_root, ignore_errors=True)
        shutil.rmtree(env_dir, ignore_errors=True)

    if rc == 0 and not cleanup_done:
        print(
            "ERROR: refusing success without isolated Compose cleanup completion",
            file=sys.stderr,
        )
        return 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
