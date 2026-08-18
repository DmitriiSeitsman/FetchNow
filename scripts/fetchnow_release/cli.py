"""CLI for FetchNow release preflight, health, prepare, verify & rollout (PRD1C1–C3A)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import STAGING_PROJECT, __version__
from .bootstrap_db import BootstrapDbInput, run_bootstrap_db
from .deploy_plan import DeployPlanInput, emit_deploy_plan, run_deploy_plan
from .deploy_root import release_dir, validate_deploy_root
from .health import HealthInput, run_health
from .preflight import PreflightInput, run_preflight
from .prepare import PrepareInput, prepare_release
from .migrate import MigrationInput, run_migration
from .migration_recover import MigrationRecoverInput, recover_migration
from .recover import RecoverInput, recover_deployment
from .rollout import RolloutInput, rollout_release
from .verify_release import verify_prepared_release


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _compose_files(args: argparse.Namespace, repo: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    for item in args.compose_files:
        path = Path(item)
        if not path.is_absolute():
            path = (repo / path).resolve()
        else:
            path = path.resolve()
        if not path.is_file():
            raise SystemExit(f"ERROR: compose file not found: {path}")
        files.append(path)
    return tuple(files)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fetchnow_release",
        description=(
            "FetchNow release preflight, health, prepare, verify, "
            "deploy-plan, bootstrap-db, application rollout & recover"
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    pre = sub.add_parser("preflight", help="Read-only staging deployment preflight")
    pre.add_argument("--project-name", default=STAGING_PROJECT)
    pre.add_argument("--env-file", type=Path, required=True)
    pre.add_argument(
        "--compose-file", action="append", dest="compose_files", required=True
    )
    pre.add_argument("--expected-revision", required=True)
    pre.add_argument("--repo-root", type=Path, default=None)
    pre.add_argument("--deploy-root", type=Path, default=None)
    pre.add_argument("--backup-root", type=Path, default=None)

    health = sub.add_parser("health", help="Read-only Compose + HTTP health gate")
    health.add_argument("--project-name", required=True)
    health.add_argument("--env-file", type=Path, required=True)
    health.add_argument(
        "--compose-file", action="append", dest="compose_files", required=True
    )
    health.add_argument("--expected-revision", required=True)
    health.add_argument("--repo-root", type=Path, default=None)
    health.add_argument(
        "--deploy-root",
        type=Path,
        default=None,
        help=(
            "Managed mode: derive authoritative image IDs from schema-v2 "
            "state/current.json (omit only for isolated tag-based health tests)"
        ),
    )
    health.add_argument(
        "--gateway-base-url",
        default="http://127.0.0.1:8091",
        help="Loopback gateway base URL (staging default 127.0.0.1:8091)",
    )

    prep = sub.add_parser(
        "prepare",
        help="Materialize exact revision + build images (no stack mutation)",
    )
    prep.add_argument("--project-name", default=STAGING_PROJECT)
    prep.add_argument("--env-file", type=Path, required=True)
    prep.add_argument(
        "--compose-file", action="append", dest="compose_files", required=True
    )
    prep.add_argument("--expected-revision", required=True)
    prep.add_argument("--deploy-root", type=Path, required=True)
    prep.add_argument("--repo-root", type=Path, default=None)
    prep.add_argument(
        "--wait-lock",
        action="store_true",
        help="Wait for exclusive release-root lock (default: fail immediately)",
    )

    ver = sub.add_parser(
        "verify-release",
        help="Read-only verification of a published release snapshot",
    )
    ver.add_argument("--expected-revision", required=True)
    ver.add_argument("--deploy-root", type=Path, required=True)
    ver.add_argument("--repo-root", type=Path, default=None)

    plan = sub.add_parser(
        "deploy-plan",
        help="Read-only migration-aware deployment plan (no mutation)",
    )
    plan.add_argument("--project-name", default=STAGING_PROJECT)
    plan.add_argument("--env-file", type=Path, required=True)
    plan.add_argument(
        "--compose-file", action="append", dest="compose_files", required=True
    )
    plan.add_argument("--expected-revision", required=True)
    plan.add_argument("--deploy-root", type=Path, required=True)
    plan.add_argument("--backup-root", type=Path, default=None)
    plan.add_argument("--repo-root", type=Path, default=None)

    roll = sub.add_parser(
        "rollout",
        help="Application-only rollout transaction (no DB mutation)",
    )
    roll.add_argument("--project-name", default=STAGING_PROJECT)
    roll.add_argument("--env-file", type=Path, required=True)
    roll.add_argument("--expected-revision", required=True)
    roll.add_argument("--deploy-root", type=Path, required=True)
    roll.add_argument("--repo-root", type=Path, default=None)
    roll.add_argument(
        "--gateway-base-url",
        default="http://127.0.0.1:8091",
    )
    roll.add_argument(
        "--bootstrap",
        action="store_true",
        help="First application rollout when current.json is absent",
    )
    roll.add_argument(
        "--wait-lock",
        action="store_true",
        help="Wait for exclusive rollout lock (default: fail immediately)",
    )

    rec = sub.add_parser(
        "recover",
        help="Recover an unresolved deployment journal (explicit action)",
    )
    rec.add_argument("--project-name", default=STAGING_PROJECT)
    rec.add_argument("--env-file", type=Path, required=True)
    rec.add_argument("--deploy-root", type=Path, required=True)
    rec.add_argument("--deployment-id", required=True)
    rec.add_argument(
        "--action",
        required=True,
        choices=("accept-target", "rollback"),
    )
    rec.add_argument("--repo-root", type=Path, default=None)
    rec.add_argument(
        "--gateway-base-url",
        default="http://127.0.0.1:8091",
    )
    rec.add_argument("--wait-lock", action="store_true")

    mig = sub.add_parser(
        "migrate",
        help="Verified database migration transaction (no application activation)",
    )
    mig.add_argument("--project-name", default=STAGING_PROJECT)
    mig.add_argument("--env-file", type=Path, required=True)
    mig.add_argument(
        "--compose-file", action="append", dest="compose_files", required=True
    )
    mig.add_argument("--expected-revision", required=True)
    mig.add_argument("--deploy-root", type=Path, required=True)
    mig.add_argument("--backup-root", type=Path, default=None)
    mig.add_argument("--repo-root", type=Path, default=None)
    mig.add_argument(
        "--gateway-base-url",
        default="http://127.0.0.1:8091",
    )
    mig.add_argument("--wait-lock", action="store_true")

    boot = sub.add_parser(
        "bootstrap-db",
        help="Initial database bootstrap (schema only; no application activation)",
    )
    boot.add_argument("--project-name", default=STAGING_PROJECT)
    boot.add_argument("--env-file", type=Path, required=True)
    boot.add_argument(
        "--compose-file", action="append", dest="compose_files", required=True
    )
    boot.add_argument("--expected-revision", required=True)
    boot.add_argument("--deploy-root", type=Path, required=True)
    boot.add_argument("--repo-root", type=Path, default=None)
    boot.add_argument("--wait-lock", action="store_true")

    mrec = sub.add_parser(
        "recover-migration",
        help="Recover an unresolved migration journal (explicit action)",
    )
    mrec.add_argument("--project-name", default=STAGING_PROJECT)
    mrec.add_argument("--env-file", type=Path, required=True)
    mrec.add_argument(
        "--compose-file", action="append", dest="compose_files", required=True
    )
    mrec.add_argument("--deploy-root", type=Path, required=True)
    mrec.add_argument("--backup-root", type=Path, default=None)
    mrec.add_argument("--migration-id", required=True)
    mrec.add_argument(
        "--action",
        required=True,
        choices=("accept_source", "accept_target", "release_hold"),
    )
    mrec.add_argument("--repo-root", type=Path, default=None)
    mrec.add_argument(
        "--gateway-base-url",
        default="http://127.0.0.1:8091",
    )
    mrec.add_argument("--wait-lock", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    repo = (args.repo_root or _repo_root()).resolve()

    # Refuse known unsafe bypass flags if ever passed as unknowns — argparse
    # already rejects unknown options. Explicit denylist for help-text audits.
    help_text = parser.format_help()
    for banned in (
        "skip-health",
        "allow-dirty",
        "skip-preflight",
        "skip-db-check",
        "test-mode",
    ):
        if banned in help_text:
            print(f"ERROR: forbidden flag leaked into CLI: {banned}", file=sys.stderr)
            return 2

    if args.command == "preflight":
        result = run_preflight(
            PreflightInput(
                project_name=args.project_name,
                env_file=args.env_file.expanduser().resolve(),
                compose_files=_compose_files(args, repo),
                expected_revision=args.expected_revision,
                repo_root=repo,
                deploy_root=args.deploy_root,
                backup_root=args.backup_root,
            )
        )
        for line in result.messages:
            print(line, file=sys.stdout if result.ok else sys.stderr)
        return 0 if result.ok else 1

    if args.command == "health":
        base = args.gateway_base_url
        if not base.startswith("http://127.0.0.1:") and not base.startswith(
            "http://localhost:"
        ):
            print("ERROR: --gateway-base-url must be loopback-only", file=sys.stderr)
            return 1
        result = run_health(
            HealthInput(
                project_name=args.project_name,
                env_file=args.env_file.expanduser().resolve(),
                compose_files=_compose_files(args, repo),
                expected_revision=args.expected_revision,
                repo_root=repo,
                gateway_base_url=base,
                deploy_root=(
                    args.deploy_root.expanduser().resolve()
                    if args.deploy_root is not None
                    else None
                ),
            )
        )
        for line in result.messages:
            print(line, file=sys.stdout if result.ok else sys.stderr)
        return 0 if result.ok else 1

    if args.command == "prepare":
        result = prepare_release(
            PrepareInput(
                project_name=args.project_name,
                env_file=args.env_file.expanduser().resolve(),
                compose_files=_compose_files(args, repo),
                expected_revision=args.expected_revision,
                repo_root=repo,
                deploy_root=args.deploy_root.expanduser().resolve(),
                wait_lock=bool(args.wait_lock),
            )
        )
        for line in result.messages:
            print(line, file=sys.stdout if result.ok else sys.stderr)
        return 0 if result.ok else 1

    if args.command == "verify-release":
        deploy = validate_deploy_root(
            args.deploy_root.expanduser().resolve(), repo_root=repo
        )
        final = release_dir(deploy, args.expected_revision)
        result = verify_prepared_release(
            final,
            expected_revision=args.expected_revision,
            repo_root=repo,
        )
        for line in result.messages:
            print(line, file=sys.stdout if result.ok else sys.stderr)
        return 0 if result.ok else 1

    if args.command == "deploy-plan":
        result = run_deploy_plan(
            DeployPlanInput(
                project_name=args.project_name,
                env_file=args.env_file.expanduser().resolve(),
                compose_files=_compose_files(args, repo),
                expected_revision=args.expected_revision,
                repo_root=repo,
                deploy_root=args.deploy_root.expanduser().resolve(),
                backup_root=(
                    args.backup_root.expanduser().resolve()
                    if args.backup_root is not None
                    else None
                ),
            )
        )
        return emit_deploy_plan(result)

    if args.command == "rollout":
        base = args.gateway_base_url
        if not base.startswith("http://127.0.0.1:") and not base.startswith(
            "http://localhost:"
        ):
            print("ERROR: --gateway-base-url must be loopback-only", file=sys.stderr)
            return 1
        result = rollout_release(
            RolloutInput(
                project_name=args.project_name,
                env_file=args.env_file.expanduser().resolve(),
                expected_revision=args.expected_revision,
                repo_root=repo,
                deploy_root=args.deploy_root.expanduser().resolve(),
                gateway_base_url=base,
                bootstrap=bool(args.bootstrap),
                wait_lock=bool(args.wait_lock),
            )
        )
        for line in result.messages:
            print(line, file=sys.stdout if result.ok else sys.stderr)
        return 0 if result.ok else 1

    if args.command == "recover":
        base = args.gateway_base_url
        if not base.startswith("http://127.0.0.1:") and not base.startswith(
            "http://localhost:"
        ):
            print("ERROR: --gateway-base-url must be loopback-only", file=sys.stderr)
            return 1
        result = recover_deployment(
            RecoverInput(
                project_name=args.project_name,
                env_file=args.env_file.expanduser().resolve(),
                deploy_root=args.deploy_root.expanduser().resolve(),
                repo_root=repo,
                deployment_id=args.deployment_id,
                action=args.action,
                gateway_base_url=base,
                wait_lock=bool(args.wait_lock),
            )
        )
        for line in result.messages:
            print(line, file=sys.stdout if result.ok else sys.stderr)
        return 0 if result.ok else 1

    if args.command == "migrate":
        base = args.gateway_base_url
        if not base.startswith("http://127.0.0.1:") and not base.startswith(
            "http://localhost:"
        ):
            print("ERROR: --gateway-base-url must be loopback-only", file=sys.stderr)
            return 1
        result = run_migration(
            MigrationInput(
                project_name=args.project_name,
                env_file=args.env_file.expanduser().resolve(),
                compose_files=_compose_files(args, repo),
                expected_revision=args.expected_revision,
                repo_root=repo,
                deploy_root=args.deploy_root.expanduser().resolve(),
                backup_root=(
                    args.backup_root.expanduser().resolve()
                    if args.backup_root is not None
                    else None
                ),
                gateway_base_url=base,
                wait_lock=bool(args.wait_lock),
            )
        )
        for line in result.messages:
            print(line, file=sys.stdout if result.ok else sys.stderr)
        if result.already_migrated:
            print("OK: already migrated (read-only verify)")
        return 0 if result.ok else 1

    if args.command == "bootstrap-db":
        result = run_bootstrap_db(
            BootstrapDbInput(
                project_name=args.project_name,
                env_file=args.env_file.expanduser().resolve(),
                compose_files=_compose_files(args, repo),
                expected_revision=args.expected_revision,
                repo_root=repo,
                deploy_root=args.deploy_root.expanduser().resolve(),
                wait_lock=bool(args.wait_lock),
            )
        )
        for line in result.messages:
            print(line, file=sys.stdout if result.ok else sys.stderr)
        if result.already_initialized:
            print("OK: already initialized (exact-head verify)")
        return 0 if result.ok else 1

    if args.command == "recover-migration":
        base = args.gateway_base_url
        if not base.startswith("http://127.0.0.1:") and not base.startswith(
            "http://localhost:"
        ):
            print("ERROR: --gateway-base-url must be loopback-only", file=sys.stderr)
            return 1
        result = recover_migration(
            MigrationRecoverInput(
                project_name=args.project_name,
                env_file=args.env_file.expanduser().resolve(),
                compose_files=_compose_files(args, repo),
                repo_root=repo,
                deploy_root=args.deploy_root.expanduser().resolve(),
                backup_root=(
                    args.backup_root.expanduser().resolve()
                    if args.backup_root is not None
                    else None
                ),
                migration_id=args.migration_id,
                action=args.action,
                gateway_base_url=base,
                wait_lock=bool(args.wait_lock),
            )
        )
        for line in result.messages:
            print(line, file=sys.stdout if result.ok else sys.stderr)
        return 0 if result.ok else 1

    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
