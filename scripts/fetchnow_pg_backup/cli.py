"""CLI for FetchNow PostgreSQL backup tooling."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import DEFAULT_KEEP_COUNT, __version__
from .compose_target import ComposeTarget, normalize_compose_files
from .create import create_backup
from .list_backups import list_backups
from .locking import LockError
from .migrations_authority import (
    static_alembic_heads_from_migrations_dir,
    validate_expected_alembic_heads,
    validate_migrations_versions_dir,
)
from .prune import prune_backups
from .process_util import CommandError
from .redaction import redact
from .verify import verify_backup


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _add_shared_target_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--project-name",
        required=True,
        help="Explicit Compose project name (never inferred from CWD)",
    )
    parser.add_argument(
        "--env-file",
        required=True,
        type=Path,
        help="Compose env file (e.g. .env.staging)",
    )
    parser.add_argument(
        "--compose-file",
        action="append",
        dest="compose_files",
        required=True,
        help="Compose file (repeatable; ordered)",
    )
    parser.add_argument(
        "--backup-root",
        required=True,
        type=Path,
        help="Backup root directory (must be outside the Git worktree)",
    )
    parser.add_argument(
        "--wait-lock",
        action="store_true",
        help="Wait for exclusive backup-root lock (default: fail immediately)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fetchnow_pg_backup",
        description="FetchNow PostgreSQL logical backup / restore-verify (PRD1B)",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create_p = sub.add_parser("create", help="Create a logical database backup")
    _add_shared_target_args(create_p)

    verify_p = sub.add_parser("verify", help="Restore-verify a published backup")
    _add_shared_target_args(verify_p)
    verify_p.add_argument(
        "--backup-id",
        required=True,
        help="Published backup directory name / id",
    )
    verify_p.add_argument(
        "--expected-alembic-head",
        action="append",
        dest="expected_alembic_heads",
        help=(
            "Expected Alembic head revision ID (repeatable for multi-head). "
            "When omitted, heads are inferred from --migrations-versions-dir "
            "(legacy CLI default; lower authority than explicit values)."
        ),
    )
    verify_p.add_argument(
        "--migrations-versions-dir",
        type=Path,
        default=None,
        help=(
            "Authoritative Alembic migrations/versions directory. "
            "Defaults to repository backend/migrations/versions for legacy CLI use."
        ),
    )

    list_p = sub.add_parser("list", help="List backups under the backup root")
    list_p.add_argument("--backup-root", required=True, type=Path)

    prune_p = sub.add_parser("prune", help="Prune old backups (default: dry-run)")
    prune_p.add_argument("--backup-root", required=True, type=Path)
    prune_p.add_argument(
        "--keep",
        type=int,
        default=DEFAULT_KEEP_COUNT,
        help=f"Number of newest valid backups to keep (default {DEFAULT_KEEP_COUNT})",
    )
    prune_p.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete (without this flag, dry-run only)",
    )
    prune_p.add_argument("--wait-lock", action="store_true")

    return parser


def _target_from_args(args: argparse.Namespace, repo_root: Path) -> ComposeTarget:
    if not args.project_name.strip():
        raise SystemExit("ERROR: --project-name must be non-empty")
    env_file = args.env_file.expanduser()
    if not env_file.is_file():
        raise SystemExit(f"ERROR: env file not found: {env_file}")
    files = normalize_compose_files(args.compose_files, repo_root=repo_root)
    from fetchnow_release.env_file import EnvFileError, load_env_file
    from fetchnow_release.environment import (
        EnvironmentError,
        assert_real_environment_bundle,
        real_identity,
    )

    if real_identity(args.project_name.strip()) is None:
        raise SystemExit(
            "ERROR: operator backup create/verify requires "
            "fetchnow-staging or fetchnow-production"
        )
    try:
        env = load_env_file(env_file)
        assert_real_environment_bundle(
            project_name=args.project_name.strip(),
            env=env,
            env_file=env_file,
            compose_files=files,
            cli_backup_root=args.backup_root,
            require_cli_backup_root=True,
            require_deploy_root=False,
        )
    except (EnvFileError, EnvironmentError, ValueError) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    return ComposeTarget(
        project_name=args.project_name.strip(),
        env_file=env_file.resolve(),
        compose_files=files,
        repo_root=repo_root,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    repo_root = _repo_root()
    default_migrations_dir = repo_root / "backend" / "migrations" / "versions"

    try:
        if args.command == "create":
            target = _target_from_args(args, repo_root)
            result = create_backup(
                target=target,
                backup_root=args.backup_root,
                repo_root=repo_root,
                wait_lock=args.wait_lock,
            )
            print(f"OK: created backup {result.backup_id}")
            print(f"path={result.backup_dir}")
            print(f"sha256={result.manifest.sha256}")
            print(f"size_bytes={result.manifest.dump_size_bytes}")
            return 0

        if args.command == "verify":
            target = _target_from_args(args, repo_root)
            backup_dir = Path(args.backup_root) / args.backup_id
            migrations_dir = (
                default_migrations_dir
                if args.migrations_versions_dir is None
                else args.migrations_versions_dir
            )
            migrations_dir = validate_migrations_versions_dir(migrations_dir)
            expected: tuple[str, ...] | None
            if args.expected_alembic_heads:
                expected = validate_expected_alembic_heads(tuple(args.expected_alembic_heads))
            else:
                expected = static_alembic_heads_from_migrations_dir(migrations_dir)
            result = verify_backup(
                target=target,
                backup_dir=backup_dir,
                backup_root=args.backup_root,
                repo_root=repo_root,
                migrations_versions_dir=migrations_dir,
                expected_alembic_heads=expected,
                wait_lock=args.wait_lock,
            )
            if not result.passed:
                print(
                    f"FAIL: verify {result.backup_id}: "
                    f"{redact(result.failure_reason or '')}"
                )
                if result.attestation_path:
                    print(f"attestation={result.attestation_path}")
                return 1
            print(f"OK: verified backup {result.backup_id}")
            print(f"attestation={result.attestation_path}")
            print(f"attestation_schema_version={result.attestation_schema_version}")
            print(f"attestation_sha256={result.attestation_sha256}")
            print(f"expected_alembic_heads={list(result.expected_alembic_heads or ())}")
            print(f"static_alembic_heads={list(result.static_alembic_heads or ())}")
            print(f"migrations_graph_sha256={result.migrations_graph_sha256}")
            print(
                f"verified_restored_heads={list(result.verified_restored_heads or ())}"
            )
            print(f"exact_head_proof={result.exact_head_proof}")
            return 0

        if args.command == "list":
            result = list_backups(backup_root=args.backup_root, repo_root=repo_root)
            for item in result.items:
                print(
                    f"{item.status:10} {item.backup_id} "
                    f"size={item.size_bytes} sha={item.sha256_prefix} "
                    f"rev={item.git_revision} structural={item.structural_ok} "
                    f"verify={item.verify_result}@{item.verify_at}"
                )
            for entry in result.invalid_entries:
                print(f"INVALID {entry}", file=sys.stderr)
            return 0

        if args.command == "prune":
            result = prune_backups(
                backup_root=args.backup_root,
                repo_root=repo_root,
                keep=args.keep,
                apply=args.apply,
                wait_lock=args.wait_lock,
            )
            mode = "DRY-RUN" if result.dry_run else "APPLY"
            print(f"{mode} prune keep={args.keep}")
            for decision in result.decisions:
                print(f"{decision.action:6} {decision.backup_id} ({decision.reason})")
            if result.deleted:
                print("deleted: " + ", ".join(result.deleted))
            return 0

        parser.error(f"unknown command {args.command}")
        return 2
    except (LockError, CommandError, ValueError, OSError, RuntimeError) as exc:
        print(f"ERROR: {redact(str(exc))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
