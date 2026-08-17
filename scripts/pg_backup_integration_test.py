#!/usr/bin/env python3
"""Isolated Compose integration test for PRD1B backup/restore.

Creates a unique per-run Compose project under the fixed prefix
``fetchnow-backup-test-<suffix>``. Never touches fetchnow / fetchnow-staging /
fetchnow-production / fetchnow-prod. Always attempts cleanup of only that run's resources.

Explicitly builds the API image from the repository Dockerfile so a fresh
GitHub-hosted runner does not depend on a pre-existing fetchnow-api:local.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fetchnow_pg_backup.attestation import (  # noqa: E402
    VerificationAttestationV2,
    attestation_sha256,
    parse_attestation_file,
)
from fetchnow_pg_backup.checksums import sha256_file  # noqa: E402
from fetchnow_pg_backup.cli import main as cli_main  # noqa: E402
from fetchnow_pg_backup.compose_target import (  # noqa: E402
    ComposeTarget,
    build_compose_argv,
    build_exec_argv,
)
from fetchnow_pg_backup.integration_project import (  # noqa: E402
    ProjectNameError,
    assert_safe_project,
    make_project_name,
)
from fetchnow_pg_backup.locking import BackupRootLock, LockError  # noqa: E402
from fetchnow_pg_backup.migrations_authority import (  # noqa: E402
    migrations_graph_content_sha256,
    static_alembic_heads_from_migrations_dir,
)
from fetchnow_pg_backup.postgres_ops import read_database_name  # noqa: E402
from fetchnow_pg_backup.process_util import require_ok, run_command  # noqa: E402
from fetchnow_pg_backup.session import open_backup_root_session  # noqa: E402


def cleanup_project(target: ComposeTarget) -> None:
    assert_safe_project(target.project_name)
    down = build_compose_argv(target, "down", "-v", "--remove-orphans")
    result = run_command(down, cwd=str(ROOT))
    if result.returncode != 0:
        raise RuntimeError(
            f"cleanup down -v failed for {target.project_name}: "
            f"{result.stderr_text.strip()}"
        )
    print(f"OK: isolated Compose project cleanup completed ({target.project_name})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-file",
        type=Path,
        help="Optional path to write the exact per-run project name for CI cleanup",
    )
    args = parser.parse_args(argv)

    project = make_project_name()
    if args.project_file is not None:
        args.project_file.parent.mkdir(parents=True, exist_ok=True)
        args.project_file.write_text(project + "\n", encoding="utf-8")
        os.chmod(args.project_file, 0o600)
        print(f"Wrote project file {args.project_file} -> {project}")

    backup_root = Path(tempfile.mkdtemp(prefix="fetchnow-pg-backup-test-"))
    env_dir = Path(tempfile.mkdtemp(prefix="fetchnow-pg-backup-env-"))
    env_path = env_dir / ".env.backup-test"
    # Unique temp env — never loads repo .env / .env.staging.
    env_path.write_text(
        "\n".join(
            [
                f"COMPOSE_PROJECT_NAME={project}",
                "POSTGRES_DB=fetchnow",
                "POSTGRES_USER=fetchnow",
                "POSTGRES_PASSWORD=backup-test-only-not-secret",
                "APP_ENV=test",
                "PUBLIC_SITE_URL=http://127.0.0.1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(env_path, 0o600)

    target = ComposeTarget(
        project_name=project,
        env_file=env_path,
        compose_files=(ROOT / "compose.yaml",),
        repo_root=ROOT,
    )

    rc = 1
    cleanup_done = False
    source_marker = None
    try:
        assert_safe_project(project)
        print(f"OK: using isolated Compose project {project}")

        # Fresh-runner bootstrap: build API image from repo Dockerfile via Compose.
        # Does not use pre-existing fetchnow-api:local from another machine/job.
        print("Building API image (compose build api) for Alembic migrate…")
        build = build_compose_argv(target, "build", "api")
        require_ok(run_command(build, cwd=str(ROOT)), context="compose build api")
        print("OK: API image built for integration")

        print("Starting isolated PostgreSQL…")
        up = build_compose_argv(target, "up", "-d", "postgres")
        require_ok(run_command(up, cwd=str(ROOT)), context="compose up postgres")

        for _ in range(60):
            ready = run_command(
                build_exec_argv(
                    target,
                    "sh",
                    "-c",
                    'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
                ),
                cwd=str(ROOT),
            )
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("postgres did not become ready")
        print("OK: isolated PostgreSQL startup")

        print("Applying Alembic migrations…")
        migrate = build_compose_argv(
            target,
            "run",
            "--rm",
            "--no-deps",
            "api",
            "alembic",
            "upgrade",
            "head",
        )
        require_ok(run_command(migrate, cwd=str(ROOT)), context="alembic upgrade")
        print("OK: Alembic migration applied")

        source_db = read_database_name(target)
        marker = run_command(
            build_exec_argv(
                target,
                "sh",
                "-c",
                'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc '
                '"SELECT version_num FROM alembic_version;"',
            ),
            cwd=str(ROOT),
        )
        require_ok(marker, context="read alembic_version")
        source_marker = marker.stdout_text.strip()
        print("OK: database sentinel inserted before backup scenarios")

        postgres_id = run_command(
            build_compose_argv(target, "ps", "-q", "postgres"),
            cwd=str(ROOT),
        )
        require_ok(postgres_id, context="postgres container id")
        source_postgres_id = postgres_id.stdout_text.strip().splitlines()[0]
        if not source_postgres_id:
            raise RuntimeError("missing postgres container id")

        migrations_dir = ROOT / "backend" / "migrations" / "versions"
        expected_heads = static_alembic_heads_from_migrations_dir(migrations_dir)
        graph_sha = migrations_graph_content_sha256(migrations_dir)

        with open_backup_root_session(
            backup_root=backup_root,
            repo_root=ROOT,
        ) as session:
            created = session.create_backup(target=target)
            backup_id = created.backup_id
            dump = created.backup_dir / "database.dump"
            digest = sha256_file(dump)
            print(f"OK: created backup {backup_id} under single backup-root session")
            print(f"OK: manifest/checksum validation path ready sha256={digest}")
            try:
                BackupRootLock(backup_root, wait=False).acquire()
            except LockError:
                print("OK: backup-root lock continuously held during session")
            else:
                raise RuntimeError("backup-root lock was not held during session")

            verified = session.verify_backup(
                target=target,
                backup_dir=created.backup_dir,
                expected_alembic_heads=expected_heads,
                migrations_versions_dir=migrations_dir,
            )
        if not verified.passed:
            raise RuntimeError(
                f"backup verify failed: {verified.failure_reason or 'unknown'}"
            )
        if verified.attestation_schema_version != 2:
            raise RuntimeError(
                f"expected attestation schema v2, got {verified.attestation_schema_version}"
            )
        if not verified.exact_head_proof:
            raise RuntimeError("verify result missing exact-head proof")
        if verified.expected_alembic_heads != expected_heads:
            raise RuntimeError("expected heads mismatch in typed verify result")
        if verified.static_alembic_heads != expected_heads:
            raise RuntimeError("static heads mismatch in typed verify result")
        if verified.migrations_graph_sha256 != graph_sha:
            raise RuntimeError("migrations graph SHA-256 mismatch in typed verify result")
        if verified.verified_restored_heads != expected_heads:
            raise RuntimeError("restored heads mismatch in typed verify result")
        if verified.attestation_path is None or verified.attestation_sha256 is None:
            raise RuntimeError("verify result missing attestation identity")
        file_digest = attestation_sha256(verified.attestation_path)
        if file_digest != verified.attestation_sha256:
            raise RuntimeError("attestation SHA-256 mismatch between result and file")
        parsed = parse_attestation_file(verified.attestation_path)
        if parsed.schema_version != 2:
            raise RuntimeError("parsed attestation is not schema v2")
        if not isinstance(parsed, VerificationAttestationV2):
            raise RuntimeError("parsed attestation is not v2 typed record")
        if tuple(parsed.static_alembic_heads) != expected_heads:
            raise RuntimeError("static heads mismatch in attestation bytes")
        if parsed.migrations_graph_sha256 != graph_sha:
            raise RuntimeError("migrations graph SHA-256 mismatch in attestation bytes")
        if tuple(parsed.verified_restored_heads or ()) != expected_heads:
            raise RuntimeError("restored heads mismatch in attestation bytes")
        if not parsed.exact_head_proof:
            raise RuntimeError("attestation bytes missing exact-head proof")
        print("OK: attestation schema is v2")
        print("OK: expected, static, and restored head sets are exactly equal")
        print("OK: returned attestation SHA-256 matches file bytes")
        print("OK: verified backup (single-session create + restore verification)")

        dbs = run_command(
            build_exec_argv(
                target,
                "sh",
                "-c",
                'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc '
                '"SELECT datname FROM pg_database WHERE datname LIKE '
                "'fetchnow_restore_verify_%';\"",
            ),
            cwd=str(ROOT),
        )
        require_ok(dbs, context="list temp dbs")
        if dbs.stdout_text.strip():
            raise RuntimeError(f"temp restore DB not cleaned: {dbs.stdout_text}")
        print("OK: temporary restore database removed")

        # Corrupt a copy. Size/checksum checks must reject before any temp DB.
        corrupt_root = Path(tempfile.mkdtemp(prefix="fetchnow-pg-backup-corrupt-"))
        try:
            corrupt_dir = corrupt_root / backup_id
            shutil.copytree(backup_root / backup_id, corrupt_dir)
            cdump = corrupt_dir / "database.dump"
            cdump.write_bytes(cdump.read_bytes() + b"\x00CORRUPT")
            print(
                "EXPECTED: deliberately corrupted archive must be rejected "
                "(size/checksum mismatch; not a job failure)"
            )
            err = StringIO()
            out = StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                bad = cli_main(
                    [
                        "verify",
                        "--project-name",
                        project,
                        "--env-file",
                        str(env_path),
                        "--compose-file",
                        str(ROOT / "compose.yaml"),
                        "--backup-root",
                        str(corrupt_root),
                        "--backup-id",
                        backup_id,
                    ]
                )
            err_text = err.getvalue() + out.getvalue()
            if bad == 0:
                raise RuntimeError("corrupt backup was incorrectly accepted")
            if "mismatch" not in err_text.lower() and "sha-256" not in err_text.lower():
                raise RuntimeError(
                    "corrupt verify failed for unexpected reason "
                    f"(expected size/checksum mismatch): {err_text!r}"
                )
            print("OK: corrupted archive rejected as expected")
        finally:
            shutil.rmtree(corrupt_root, ignore_errors=True)

        after = run_command(
            build_exec_argv(
                target,
                "sh",
                "-c",
                'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc '
                '"SELECT version_num FROM alembic_version;"',
            ),
            cwd=str(ROOT),
        )
        require_ok(after, context="re-read alembic_version")
        if after.stdout_text.strip() != source_marker:
            raise RuntimeError("source database changed during verify")
        print(f"OK: source database preserved ({source_db})")

        postgres_after = run_command(
            build_compose_argv(target, "ps", "-q", "postgres"),
            cwd=str(ROOT),
        )
        require_ok(postgres_after, context="postgres container id after verify")
        after_postgres_id = postgres_after.stdout_text.strip().splitlines()[0]
        if after_postgres_id != source_postgres_id:
            raise RuntimeError("postgres container ID changed during verify")
        print("OK: source Postgres container ID unchanged")

        wrong_heads_argv = [
            "verify",
            "--project-name",
            project,
            "--env-file",
            str(env_path),
            "--compose-file",
            str(ROOT / "compose.yaml"),
            "--backup-root",
            str(backup_root),
            "--backup-id",
            backup_id,
            "--expected-alembic-head",
            "0000_nonexistent_head",
            "--migrations-versions-dir",
            str(migrations_dir),
        ]
        err = StringIO()
        out = StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            wrong_rc = cli_main(wrong_heads_argv)
        if wrong_rc == 0:
            raise RuntimeError("wrong expected heads were incorrectly accepted")
        attestation_files = list(
            (backup_root / backup_id / "verifications").glob("verify_*_passed*.json")
        )
        v2_passed = [
            p
            for p in attestation_files
            if parse_attestation_file(p).schema_version == 2
        ]
        if len(v2_passed) != 1:
            raise RuntimeError(
                "wrong-heads rejection must not create additional passed v2 attestations"
            )
        print("OK: wrong expected heads rejected without false passed attestation")

        legacy_fixture = ROOT / "tests" / "pg_backup" / "fixtures" / "attestation_v1.json"
        if legacy_fixture.is_file():
            before_bytes = legacy_fixture.read_bytes()
            before_mtime = legacy_fixture.stat().st_mtime
            parse_attestation_file(legacy_fixture)
            if legacy_fixture.read_bytes() != before_bytes:
                raise RuntimeError("legacy v1 fixture bytes changed")
            if legacy_fixture.stat().st_mtime != before_mtime:
                raise RuntimeError("legacy v1 fixture mtime changed")
            print("OK: legacy v1 fixture remains readable without rewrite")

        list_rc = cli_main(["list", "--backup-root", str(backup_root)])
        if list_rc != 0:
            raise RuntimeError("list failed")

        print("OK: integration backup/restore verification passed")
        print(f"project={project}")
        print(f"backup_id={backup_id}")
        print(f"sha256={digest}")
        print(f"source_database={source_db}")
        rc = 0
    except (ProjectNameError, Exception):  # noqa: BLE001
        traceback.print_exc()
        rc = 1
    finally:
        try:
            cleanup_project(target)
            cleanup_done = True
        except Exception as cleanup_exc:  # noqa: BLE001
            print(f"ERROR: {cleanup_exc}", file=sys.stderr)
            cleanup_done = False
            rc = 1
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
