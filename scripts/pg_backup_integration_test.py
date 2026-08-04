#!/usr/bin/env python3
"""Isolated Compose integration test for PRD1B backup/restore.

Creates a unique per-run Compose project under the fixed prefix
``fetchnow-backup-test-<suffix>``. Never touches fetchnow / fetchnow-staging /
fetchnow-prod. Always attempts cleanup of only that run's resources.

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
from fetchnow_pg_backup.postgres_ops import read_database_name  # noqa: E402
from fetchnow_pg_backup.process_util import require_ok, run_command  # noqa: E402


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

        create_argv = [
            "create",
            "--project-name",
            project,
            "--env-file",
            str(env_path),
            "--compose-file",
            str(ROOT / "compose.yaml"),
            "--backup-root",
            str(backup_root),
        ]
        if cli_main(create_argv) != 0:
            raise RuntimeError("backup create failed")
        print("OK: created backup (pg_dump custom-format + manifest/checksum)")

        published = [
            p
            for p in backup_root.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        ]
        if len(published) != 1:
            raise RuntimeError(f"expected one backup, found {published}")
        backup_id = published[0].name
        dump = published[0] / "database.dump"
        digest = sha256_file(dump)
        print(f"OK: manifest/checksum validation path ready sha256={digest}")

        verify_argv = [
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
        ]
        if cli_main(verify_argv) != 0:
            raise RuntimeError("backup verify failed")
        print("OK: verified backup (temporary database restore + semantic checks)")

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
            shutil.copytree(published[0], corrupt_dir)
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
