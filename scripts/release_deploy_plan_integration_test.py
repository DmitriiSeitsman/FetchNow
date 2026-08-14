#!/usr/bin/env python3
"""Isolated PRD1C3B1 deploy-plan integration (read-only planner path).

Exit status: callers must inspect the process exit code directly. Piping
stdout/stderr through ``| tail`` or similar returns the pipe's exit code,
not Python's (use ``PIPESTATUS`` in bash when filtering output).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import traceback
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "tests" / "release"))

from fetchnow_release.archive import verify_required_files  # noqa: E402
from fetchnow_release.c2_constants import (  # noqa: E402
    SOURCE_CONTRACT_VERSION_V1,
    SOURCE_DIRNAME,
)
from fetchnow_release.current_state import (  # noqa: E402
    ApplicationState,
    build_bootstrap_database_state,
    build_committed_current_state,
    write_current_state,
)
from fetchnow_release.db_heads import target_heads_from_release_source  # noqa: E402
from fetchnow_release.deploy_plan import DeployPlanInput, run_deploy_plan  # noqa: E402
from fetchnow_release.deploy_plan_project import (  # noqa: E402
    assert_deploy_plan_project,
    make_deploy_plan_project_name,
)
from fetchnow_release.deploy_root import release_dir  # noqa: E402
from fetchnow_release.image_identity import assert_release_images_present  # noqa: E402
from fetchnow_release.journal import deployments_dir, sha256_file  # noqa: E402
from fetchnow_release.manifest import load_manifest, manifest_path  # noqa: E402
from fetchnow_release.prepare import (  # noqa: E402
    PrepareInput,
    _chmod_tree_readonly,
    prepare_release,
)
from fetchnow_release.verify_release import verify_prepared_release  # noqa: E402
from password_fixture import valid_test_password  # noqa: E402

INTEGRATION_SUCCESS_MARKER = "OK: deploy-plan integration passed"

MIGRATION_FILE = """\"\"\"Online expand migration for deploy-plan integration.\"\"\"

revision = "0006_expand"
down_revision = "0005_download_file_details"


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
"""

CONTRACT_WITH_MIGRATION = """{
  "schema_version": 1,
  "transitions": [
    {
      "from_heads": ["0005_download_file_details"],
      "to_heads": ["0006_expand"],
      "included_revisions": ["0006_expand"],
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


def run(cmd: list[str], *, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr}"
        )
    return proc


def configure_fixture_git_identity(clone: Path) -> None:
    """Repository-local author identity for deterministic fixture commits in CI."""
    run(["git", "config", "--local", "user.name", "FetchNow Integration Fixture"], cwd=clone)
    run(
        ["git", "config", "--local", "user.email", "integration@fetchnow.invalid"],
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


def compose_cmd(project: str, env_file: Path, clone: Path) -> list[str]:
    return [
        "docker",
        "compose",
        "--env-file",
        str(env_file),
        "--project-name",
        project,
        "-f",
        "compose.yaml",
        "-f",
        "compose.staging.yaml",
    ]


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.exists():
        return digest.hexdigest()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rel = str(path.relative_to(root)).encode("utf-8")
            digest.update(rel)
            digest.update(path.read_bytes())
    return digest.hexdigest()


def container_id(compose: list[str], service: str, cwd: Path) -> str:
    return run(compose + ["ps", "-q", service], cwd=cwd).stdout.strip()


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
            '-c "CREATE TABLE IF NOT EXISTS deploy_plan_it_marker (marker text primary key); '
            f"INSERT INTO deploy_plan_it_marker(marker) VALUES ('{marker}') "
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
            f"\"SELECT marker FROM deploy_plan_it_marker WHERE marker = '{marker}'\"",
        ],
        cwd=clone,
    ).stdout.strip()


def prepare_revision(
    *,
    clone: Path,
    env_file: Path,
    deploy_root: Path,
    revision: str,
    project: str,
) -> Path:
    compose = (
        (clone / "compose.yaml").resolve(),
        (clone / "compose.staging.yaml").resolve(),
    )
    result = prepare_release(
        PrepareInput(
            project_name=project,
            env_file=env_file,
            compose_files=compose,
            expected_revision=revision,
            repo_root=clone,
            deploy_root=deploy_root,
        )
    )
    if not result.ok or result.release_dir is None:
        raise RuntimeError(f"prepare failed: {result.messages}")
    return result.release_dir


def seed_current_state(deploy_root: Path, release_path: Path, revision: str) -> None:
    manifest = load_manifest(manifest_path(release_path))
    ids = assert_release_images_present(manifest)
    heads = target_heads_from_release_source(release_path)
    write_current_state(
        deploy_root,
        build_committed_current_state(
            application=ApplicationState(
                revision=revision,
                release_manifest_sha256=sha256_file(manifest_path(release_path)),
                image_ids=ids,
                deployment_id=str(uuid.uuid4()),
            ),
            database=build_bootstrap_database_state(
                heads=heads,
                schema_release_revision=revision,
            ),
            updated_at_utc="2026-01-01T00:00:00Z",
        ),
    )


def _chmod_writable(path: Path) -> None:
    import stat

    if path.is_symlink():
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    path.chmod(mode | 0o200)


def shape_legacy_v1_release(release_path: Path) -> None:
    """Simulate a historical v1 finalized release (new fixture, not in-place upgrade)."""
    source = release_path / SOURCE_DIRNAME
    compat = source / "deploy/migrations/compatibility.json"
    if compat.is_file():
        for parent in (compat.parent, compat.parent.parent, compat.parent.parent.parent):
            _chmod_writable(parent)
        _chmod_writable(compat)
        compat.unlink()
    contract_hashes = verify_required_files(
        source, source_contract_version=SOURCE_CONTRACT_VERSION_V1
    )
    man_path = manifest_path(release_path)
    _chmod_writable(release_path)
    _chmod_writable(man_path)
    data = json.loads(man_path.read_text(encoding="utf-8"))
    data.pop("source_contract_version", None)
    data["contract_hashes"] = contract_hashes
    man_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _chmod_tree_readonly(source)
    if not man_path.is_symlink():
        import stat

        mode = stat.S_IMODE(man_path.stat().st_mode)
        man_path.chmod(mode & ~0o222)


def invoke_plan(
    *,
    project: str,
    env_file: Path,
    clone: Path,
    deploy_root: Path,
    revision: str,
) -> tuple[bool, str, tuple[str, ...]]:
    compose = (
        (clone / "compose.yaml").resolve(),
        (clone / "compose.staging.yaml").resolve(),
    )
    result = run_deploy_plan(
        DeployPlanInput(
            project_name=project,
            env_file=env_file,
            compose_files=compose,
            expected_revision=revision,
            repo_root=clone,
            deploy_root=deploy_root,
        )
    )
    return result.ok, result.plan_json or "", result.messages


def assert_deploy_root_unchanged(before: str, after: str) -> None:
    if before != after:
        raise RuntimeError("deploy root digest changed during read-only deploy-plan")


def assert_no_secrets(text: str) -> None:
    for needle in ("POSTGRES_PASSWORD", "DATABASE_URL", valid_test_password()):
        if needle in text:
            raise RuntimeError(f"secret pattern leaked into deploy-plan output: {needle!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-file", type=Path, default=None)
    args = parser.parse_args(argv)

    project = make_deploy_plan_project_name()
    if args.project_file:
        args.project_file.parent.mkdir(parents=True, exist_ok=True)
        args.project_file.write_text(project + "\n", encoding="utf-8")
        os.chmod(args.project_file, 0o600)

    clone = Path(tempfile.mkdtemp(prefix="fetchnow-deploy-plan-clone-"))
    deploy_root = Path(tempfile.mkdtemp(prefix="fetchnow-deploy-plan-deploy-"))
    backup_root = Path(tempfile.mkdtemp(prefix="fetchnow-deploy-plan-backup-"))
    env_dir = Path(tempfile.mkdtemp(prefix="fetchnow-deploy-plan-env-"))
    os.chmod(deploy_root, 0o750)
    os.chmod(backup_root, 0o700)
    marker = f"deploy-plan-{secrets.token_hex(4)}"
    gateway_port = f"127.0.0.1:{20000 + secrets.randbelow(1000)}"
    rc = 1
    try:
        assert_deploy_plan_project(project)
        run(["git", "clone", "--local", "--no-hardlinks", str(ROOT), str(clone)], cwd=ROOT)
        configure_fixture_git_identity(clone)
        compat_src = ROOT / "deploy" / "migrations" / "compatibility.json"
        compat_dst = clone / "deploy" / "migrations" / "compatibility.json"
        if compat_src.is_file() and not compat_dst.is_file():
            compat_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(compat_src, compat_dst)
            run(["git", "add", str(compat_dst)], cwd=clone)
            run(["git", "commit", "-m", "test: ensure compatibility contract in clone"], cwd=clone)
        baseline_rev = run(["git", "rev-parse", "HEAD"], cwd=clone).stdout.strip()
        run(["git", "checkout", "--detach", baseline_rev], cwd=clone)
        run(["git", "update-ref", "refs/remotes/origin/main", baseline_rev], cwd=clone)

        env_file = env_dir / ".env.deploy-plan"
        write_env(
            env_file,
            project=project,
            revision=baseline_rev,
            gateway_port=gateway_port,
            deploy_root=deploy_root,
            backup_root=backup_root,
        )
        compose = compose_cmd(project, env_file, clone)

        run(compose + ["build", "api"], cwd=clone)
        run(compose + ["up", "-d", "postgres"], cwd=clone)
        run(compose + ["run", "--rm", "api", "alembic", "upgrade", "head"], cwd=clone)
        insert_sentinel(compose, clone, marker)
        print("OK: isolated Postgres setup reached baseline with DB sentinel")

        prepare_revision(
            clone=clone,
            env_file=env_file,
            deploy_root=deploy_root,
            revision=baseline_rev,
            project=project,
        )
        baseline_release = release_dir(deploy_root, baseline_rev)
        shape_legacy_v1_release(baseline_release)
        legacy_verify = verify_prepared_release(
            baseline_release, expected_revision=baseline_rev, repo_root=clone
        )
        if not legacy_verify.ok:
            raise RuntimeError(f"legacy v1 release verify failed: {legacy_verify.messages}")
        legacy_manifest = load_manifest(manifest_path(baseline_release))
        if legacy_manifest.source_contract_version != SOURCE_CONTRACT_VERSION_V1:
            raise RuntimeError("legacy release must verify as source_contract_version=1")
        print("OK: legacy v1 current release verifies under historical contract")

        seed_current_state(deploy_root, baseline_release, baseline_rev)
        postgres_id = container_id(compose, "postgres", clone)

        v1_target_ok, _, v1_target_msgs = invoke_plan(
            project=project,
            env_file=env_file,
            clone=clone,
            deploy_root=deploy_root,
            revision=baseline_rev,
        )
        if v1_target_ok:
            raise RuntimeError("deploy-plan must reject legacy v1 target release")
        if not any("source_contract_version" in msg for msg in v1_target_msgs):
            raise RuntimeError(f"v1 target rejection message unexpected: {v1_target_msgs}")
        print("OK: deploy-plan rejects legacy v1 target release")

        # Migration release commit in isolated clone only.
        mig_versions = clone / "backend" / "migrations" / "versions"
        (mig_versions / "0006_expand.py").write_text(MIGRATION_FILE, encoding="utf-8")
        contract_path = clone / "deploy" / "migrations" / "compatibility.json"
        contract_path.write_text(CONTRACT_WITH_MIGRATION, encoding="utf-8")
        run(["git", "add", "backend/migrations/versions/0006_expand.py", str(contract_path)], cwd=clone)
        run(["git", "commit", "-m", "test: deploy-plan expand migration"], cwd=clone)
        migrate_rev = run(["git", "rev-parse", "HEAD"], cwd=clone).stdout.strip()
        run(["git", "update-ref", "refs/remotes/origin/main", migrate_rev], cwd=clone)
        write_env(
            env_file,
            project=project,
            revision=migrate_rev,
            gateway_port=gateway_port,
            deploy_root=deploy_root,
            backup_root=backup_root,
        )
        prepare_revision(
            clone=clone,
            env_file=env_file,
            deploy_root=deploy_root,
            revision=migrate_rev,
            project=project,
        )
        migrate_manifest = load_manifest(manifest_path(release_dir(deploy_root, migrate_rev)))
        if migrate_manifest.source_contract_version != 2:
            raise RuntimeError("target release must be source_contract_version=2")

        ok3, plan_json3, _ = invoke_plan(
            project=project,
            env_file=env_file,
            clone=clone,
            deploy_root=deploy_root,
            revision=migrate_rev,
        )
        if not ok3:
            raise RuntimeError(f"legacy current + v2 target deploy-plan failed: {_}")
        plan3 = json.loads(plan_json3)
        if plan3["migration_required"] is not True:
            raise RuntimeError("expected migration_required=true")
        if plan3["included_revisions"] != ["0006_expand"]:
            raise RuntimeError(f"unexpected included revisions: {plan3['included_revisions']}")
        if plan3["verified_backup_required"] is not True:
            raise RuntimeError("expected verified_backup_required=true")
        if plan3.get("application_rollout_required") is not True:
            raise RuntimeError(
                "expected application_rollout_required=true for A→B application change"
            )
        print("OK: legacy v1 current + v2 target forward migration deploy plan")

        run(["git", "checkout", "--detach", migrate_rev], cwd=clone)
        wrong_from_path = clone / "deploy" / "migrations" / "compatibility.json"
        wrong_from_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "transitions": [
                        {
                            "from_heads": ["0009_wrong"],
                            "to_heads": ["0006_expand"],
                            "included_revisions": ["0006_expand"],
                            "execution_mode": "online_expand",
                            "online_with_previous_application": True,
                            "previous_application_rollback_compatible": True,
                            "data_loss": "none",
                            "database_downgrade": "forbidden",
                            "rationale": "Wrong from heads for deploy-plan integration rejection.",
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        run(["git", "add", str(wrong_from_path)], cwd=clone)
        run(["git", "commit", "-m", "test: wrong contract from heads"], cwd=clone)
        wrong_from_rev = run(["git", "rev-parse", "HEAD"], cwd=clone).stdout.strip()
        run(["git", "update-ref", "refs/remotes/origin/main", wrong_from_rev], cwd=clone)
        write_env(
            env_file,
            project=project,
            revision=wrong_from_rev,
            gateway_port=gateway_port,
            deploy_root=deploy_root,
            backup_root=backup_root,
        )
        prepare_revision(
            clone=clone,
            env_file=env_file,
            deploy_root=deploy_root,
            revision=wrong_from_rev,
            project=project,
        )
        bad_from, _, _ = invoke_plan(
            project=project,
            env_file=env_file,
            clone=clone,
            deploy_root=deploy_root,
            revision=wrong_from_rev,
        )
        if bad_from:
            raise RuntimeError("wrong contract heads should reject deploy-plan")
        print("OK: wrong contract heads rejected")

        wrong_included_path = clone / "deploy" / "migrations" / "compatibility.json"
        wrong_included_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "transitions": [
                        {
                            "from_heads": ["0005_download_file_details"],
                            "to_heads": ["0006_expand"],
                            "included_revisions": ["0009_wrong"],
                            "execution_mode": "online_expand",
                            "online_with_previous_application": True,
                            "previous_application_rollback_compatible": True,
                            "data_loss": "none",
                            "database_downgrade": "forbidden",
                            "rationale": "Wrong included revisions for deploy-plan integration rejection.",
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        run(["git", "add", str(wrong_included_path)], cwd=clone)
        run(["git", "commit", "-m", "test: wrong included revisions"], cwd=clone)
        wrong_inc_rev = run(["git", "rev-parse", "HEAD"], cwd=clone).stdout.strip()
        run(["git", "update-ref", "refs/remotes/origin/main", wrong_inc_rev], cwd=clone)
        write_env(
            env_file,
            project=project,
            revision=wrong_inc_rev,
            gateway_port=gateway_port,
            deploy_root=deploy_root,
            backup_root=backup_root,
        )
        prepare_revision(
            clone=clone,
            env_file=env_file,
            deploy_root=deploy_root,
            revision=wrong_inc_rev,
            project=project,
        )
        bad_inc, _, _ = invoke_plan(
            project=project,
            env_file=env_file,
            clone=clone,
            deploy_root=deploy_root,
            revision=wrong_inc_rev,
        )
        if bad_inc:
            raise RuntimeError("wrong included revisions should reject deploy-plan")
        print("OK: wrong included revisions rejected")

        unsafe_path = clone / "deploy" / "migrations" / "compatibility.json"
        unsafe_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "transitions": [
                        {
                            "from_heads": ["0005_download_file_details"],
                            "to_heads": ["0006_expand"],
                            "included_revisions": ["0006_expand"],
                            "execution_mode": "online_expand",
                            "online_with_previous_application": False,
                            "previous_application_rollback_compatible": True,
                            "data_loss": "none",
                            "database_downgrade": "forbidden",
                            "rationale": "Unsafe policy boolean must fail deploy-plan integration.",
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        run(["git", "add", str(unsafe_path)], cwd=clone)
        run(["git", "commit", "-m", "test: unsafe migration policy"], cwd=clone)
        unsafe_rev = run(["git", "rev-parse", "HEAD"], cwd=clone).stdout.strip()
        run(["git", "update-ref", "refs/remotes/origin/main", unsafe_rev], cwd=clone)
        write_env(
            env_file,
            project=project,
            revision=unsafe_rev,
            gateway_port=gateway_port,
            deploy_root=deploy_root,
            backup_root=backup_root,
        )
        prepare_revision(
            clone=clone,
            env_file=env_file,
            deploy_root=deploy_root,
            revision=unsafe_rev,
            project=project,
        )
        bad_unsafe, _, _ = invoke_plan(
            project=project,
            env_file=env_file,
            clone=clone,
            deploy_root=deploy_root,
            revision=unsafe_rev,
        )
        if bad_unsafe:
            raise RuntimeError("unsafe policy unexpectedly accepted by deploy-plan")
        print("OK: destructive/unsafe policy rejected")

        # Missing compatibility.json must fail C2 prepare before publication.
        run(["git", "checkout", "--detach", baseline_rev], cwd=clone)
        missing_path = clone / "deploy" / "migrations" / "compatibility.json"
        missing_path.unlink()
        run(["git", "add", str(missing_path)], cwd=clone)
        run(["git", "commit", "-m", "test: remove compatibility contract"], cwd=clone)
        missing_rev = run(["git", "rev-parse", "HEAD"], cwd=clone).stdout.strip()
        run(["git", "update-ref", "refs/remotes/origin/main", missing_rev], cwd=clone)
        write_env(
            env_file,
            project=project,
            revision=missing_rev,
            gateway_port=gateway_port,
            deploy_root=deploy_root,
            backup_root=backup_root,
        )
        releases_before = set((deploy_root / "releases").glob("*")) if (deploy_root / "releases").is_dir() else set()
        try:
            prepare_revision(
                clone=clone,
                env_file=env_file,
                deploy_root=deploy_root,
                revision=missing_rev,
                project=project,
            )
            raise RuntimeError("prepare without compatibility.json should fail")
        except RuntimeError as exc:
            if "prepare without compatibility.json should fail" in str(exc):
                raise
        releases_after = set((deploy_root / "releases").glob("*")) if (deploy_root / "releases").is_dir() else set()
        if releases_after - releases_before:
            raise RuntimeError("prepare without compatibility.json published a release")
        print("OK: missing compatibility contract rejected at prepare")

        # Divergent target: DB/current at 0006_expand, target graph head is sibling 0006_diverge only.
        run(["git", "checkout", "--detach", migrate_rev], cwd=clone)
        run(compose + ["run", "--rm", "api", "alembic", "upgrade", "head"], cwd=clone)
        seed_current_state(
            deploy_root, release_dir(deploy_root, migrate_rev), migrate_rev
        )
        (mig_versions / "0006_expand.py").unlink()
        (mig_versions / "0006_diverge.py").write_text(
            'revision = "0006_diverge"\ndown_revision = "0005_download_file_details"\n',
            encoding="utf-8",
        )
        diverge_contract = clone / "deploy" / "migrations" / "compatibility.json"
        diverge_contract.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "transitions": [
                        {
                            "from_heads": ["0006_expand"],
                            "to_heads": ["0006_diverge"],
                            "included_revisions": ["0006_diverge"],
                            "execution_mode": "online_expand",
                            "online_with_previous_application": True,
                            "previous_application_rollback_compatible": True,
                            "data_loss": "none",
                            "database_downgrade": "forbidden",
                            "rationale": "Divergent sibling branch for deploy-plan integration rejection.",
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        run(
            ["git", "add", str(mig_versions / "0006_diverge.py"), str(diverge_contract)],
            cwd=clone,
        )
        run(["git", "rm", str(mig_versions / "0006_expand.py")], cwd=clone)
        run(["git", "commit", "-m", "test: divergent sibling migration head"], cwd=clone)
        diverge_rev = run(["git", "rev-parse", "HEAD"], cwd=clone).stdout.strip()
        run(["git", "update-ref", "refs/remotes/origin/main", diverge_rev], cwd=clone)
        write_env(
            env_file,
            project=project,
            revision=diverge_rev,
            gateway_port=gateway_port,
            deploy_root=deploy_root,
            backup_root=backup_root,
        )
        prepare_revision(
            clone=clone,
            env_file=env_file,
            deploy_root=deploy_root,
            revision=diverge_rev,
            project=project,
        )
        diverge_ok, _, diverge_messages = invoke_plan(
            project=project,
            env_file=env_file,
            clone=clone,
            deploy_root=deploy_root,
            revision=diverge_rev,
        )
        if diverge_ok:
            raise RuntimeError("divergent target should reject deploy-plan")
        if not any("divergent" in msg.lower() for msg in diverge_messages):
            raise RuntimeError(f"divergent rejection message unexpected: {diverge_messages}")
        print("OK: divergent target rejected")

        # Downgrade rejection: current release + DB at 0006_expand, target v2 baseline release.
        run(["git", "checkout", "--detach", migrate_rev], cwd=clone)
        run(compose + ["build", "api"], cwd=clone)
        run(compose + ["run", "--rm", "api", "alembic", "upgrade", "head"], cwd=clone)
        seed_current_state(
            deploy_root, release_dir(deploy_root, migrate_rev), migrate_rev
        )
        run(["git", "checkout", "--detach", baseline_rev], cwd=clone)
        run(["git", "commit", "--allow-empty", "-m", "test: v2 baseline downgrade target"], cwd=clone)
        baseline_v2_rev = run(["git", "rev-parse", "HEAD"], cwd=clone).stdout.strip()
        run(["git", "update-ref", "refs/remotes/origin/main", baseline_v2_rev], cwd=clone)
        write_env(
            env_file,
            project=project,
            revision=baseline_v2_rev,
            gateway_port=gateway_port,
            deploy_root=deploy_root,
            backup_root=backup_root,
        )
        prepare_revision(
            clone=clone,
            env_file=env_file,
            deploy_root=deploy_root,
            revision=baseline_v2_rev,
            project=project,
        )
        downgrade_ok, _, downgrade_messages = invoke_plan(
            project=project,
            env_file=env_file,
            clone=clone,
            deploy_root=deploy_root,
            revision=baseline_v2_rev,
        )
        if downgrade_ok:
            raise RuntimeError("downgrade target should reject deploy-plan")
        if not any("downgrade" in msg.lower() for msg in downgrade_messages):
            raise RuntimeError(f"downgrade rejection message unexpected: {downgrade_messages}")
        print("OK: downgrade target rejected")

        write_env(
            env_file,
            project=project,
            revision=migrate_rev,
            gateway_port=gateway_port,
            deploy_root=deploy_root,
            backup_root=backup_root,
        )
        ok, plan_json, _messages = invoke_plan(
            project=project,
            env_file=env_file,
            clone=clone,
            deploy_root=deploy_root,
            revision=migrate_rev,
        )
        if not ok:
            raise RuntimeError(f"no-migration deploy-plan failed: {_messages}")
        plan = json.loads(plan_json)
        if plan["migration_required"] is not False:
            raise RuntimeError("expected migration_required=false for equal heads")
        if plan["included_revisions"]:
            raise RuntimeError("expected empty included_revisions for equal heads")
        if plan["verified_backup_required"] is not False:
            raise RuntimeError("expected verified_backup_required=false")
        if plan.get("application_rollout_required") is not False:
            raise RuntimeError(
                "expected application_rollout_required=false when target equals current"
            )
        print("OK: no-migration deploy plan")

        ok2, plan_json2, _ = invoke_plan(
            project=project,
            env_file=env_file,
            clone=clone,
            deploy_root=deploy_root,
            revision=migrate_rev,
        )
        if plan_json != plan_json2:
            raise RuntimeError("deploy-plan output is not byte-identical on repeat")
        print("OK: deterministic deploy-plan output")

        deploy_digest = tree_digest(deploy_root)

        if read_sentinel(compose, clone, marker) != marker:
            raise RuntimeError("DB sentinel changed during deploy-plan integration")
        if container_id(compose, "postgres", clone) != postgres_id:
            raise RuntimeError("postgres container ID changed during deploy-plan")
        assert_deploy_root_unchanged(deploy_digest, tree_digest(deploy_root))
        if deployments_dir(deploy_root).exists() and any(deployments_dir(deploy_root).iterdir()):
            raise RuntimeError("rollout journal directories appeared during deploy-plan")
        assert_no_secrets(plan_json3)
        print("OK: read-only DB/container/deploy-root preservation")

        run(compose + ["down", "-v", "--remove-orphans"], cwd=clone)
        print(f"OK: isolated Compose project cleanup completed ({project})")
        print(INTEGRATION_SUCCESS_MARKER)
        rc = 0
    except Exception:
        traceback.print_exc()
        rc = 1
        try:
            assert_deploy_plan_project(project)
            compose = compose_cmd(project, env_dir / ".env.deploy-plan", clone)
            if (clone / "compose.yaml").is_file():
                subprocess.run(
                    compose + ["down", "-v", "--remove-orphans"],
                    cwd=str(clone),
                    check=False,
                )
        except Exception:
            pass
    finally:
        for path in (clone, deploy_root, backup_root, env_dir):
            shutil.rmtree(path, ignore_errors=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
