#!/usr/bin/env python3
"""CI fallback cleanup for PRD1B backup integration (exact project only)."""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from fetchnow_pg_backup.compose_target import ComposeTarget, build_compose_argv  # noqa: E402
from fetchnow_pg_backup.integration_project import (  # noqa: E402
    ProjectNameError,
    load_project_file,
)
from fetchnow_pg_backup.process_util import run_command  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-file",
        type=Path,
        required=True,
        help="File containing the exact per-run fetchnow-backup-test-<suffix> name",
    )
    parser.add_argument(
        "--compose-file",
        type=Path,
        default=Path("compose.yaml"),
        help="Compose file used for down -v (default: compose.yaml)",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository root (default: cwd)",
    )
    args = parser.parse_args(argv)

    if not args.project_file.is_file():
        print(
            f"No project file at {args.project_file}; refusing cleanup mutation",
            file=sys.stderr,
        )
        return 0

    try:
        project = load_project_file(args.project_file)
    except ProjectNameError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    compose_file = args.compose_file
    if not compose_file.is_absolute():
        compose_file = (args.repo_root / compose_file).resolve()
    if not compose_file.is_file():
        print(f"ERROR: compose file not found: {compose_file}", file=sys.stderr)
        return 1

    # Minimal env so Compose can interpolate without loading .env / .env.staging.
    with tempfile.TemporaryDirectory(prefix="fetchnow-pg-backup-cleanup-env-") as tmp:
        env_path = Path(tmp) / ".env.cleanup"
        env_path.write_text(
            "\n".join(
                [
                    f"COMPOSE_PROJECT_NAME={project}",
                    "POSTGRES_DB=fetchnow",
                    "POSTGRES_USER=fetchnow",
                    "POSTGRES_PASSWORD=cleanup-placeholder-not-used",
                    "PUBLIC_SITE_URL=http://127.0.0.1",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        target = ComposeTarget(
            project_name=project,
            env_file=env_path,
            compose_files=(compose_file,),
            repo_root=args.repo_root.resolve(),
        )
        argv_cmd = build_compose_argv(target, "down", "-v", "--remove-orphans")
        print(f"Cleaning Compose project {project}")
        result = run_command(argv_cmd, cwd=str(args.repo_root.resolve()))
        if result.returncode != 0:
            print(
                f"ERROR: cleanup failed for {project}: {result.stderr_text.strip()}",
                file=sys.stderr,
            )
            return 1
    print(f"OK: isolated Compose project cleanup completed ({project})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
