"""CLI for FetchNow release preflight and health (PRD1C1)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import STAGING_PROJECT, __version__
from .health import HealthInput, run_health
from .preflight import PreflightInput, run_preflight


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
        description="FetchNow release preflight & health gate (PRD1C1)",
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
        "--gateway-base-url",
        default="http://127.0.0.1:8091",
        help="Loopback gateway base URL (staging default 127.0.0.1:8091)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    repo = (args.repo_root or _repo_root()).resolve()

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
            )
        )
        for line in result.messages:
            print(line, file=sys.stdout if result.ok else sys.stderr)
        return 0 if result.ok else 1

    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
