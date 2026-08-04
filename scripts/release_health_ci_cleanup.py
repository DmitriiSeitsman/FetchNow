#!/usr/bin/env python3
"""CI fallback cleanup for PRD1C1 health integration (exact project only)."""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path

PROJECT_RE = re.compile(r"^fetchnow-health-test-[a-z0-9]{8,32}$")
FORBIDDEN = frozenset(
    {"", "fetchnow", "fetchnow-staging", "fetchnow-prod", "fetchnow-health-test"}
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-file", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)

    if not args.project_file.is_file():
        print(f"No project file at {args.project_file}; refusing cleanup mutation")
        return 0
    project = "".join(args.project_file.read_text(encoding="utf-8").split())
    if project in FORBIDDEN or not PROJECT_RE.fullmatch(project):
        print(f"ERROR: refusing unsafe project name {project!r}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="fetchnow-health-cleanup-") as tmp:
        env = Path(tmp) / ".env"
        # Minimal env for compose down interpolation.
        env.write_text(
            "\n".join(
                [
                    f"COMPOSE_PROJECT_NAME={project}",
                    "FETCHNOW_RELEASE_REVISION=" + ("a" * 40),
                    "GATEWAY_PORT=127.0.0.1:19999",
                    "PUBLIC_SITE_URL=http://127.0.0.1",
                    "POSTGRES_DB=fetchnow",
                    "POSTGRES_USER=fetchnow",
                    "POSTGRES_PASSWORD=cleanup-placeholder-not-used-xxxxxxxx",
                    "FETCHNOW_DEPLOY_ROOT=/tmp/fetchnow-health-cleanup-deploy",
                    "FETCHNOW_BACKUP_ROOT=/tmp/fetchnow-health-cleanup-backup",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        import subprocess

        proc = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                str(env),
                "--project-name",
                project,
                "-f",
                "compose.yaml",
                "-f",
                "compose.staging.yaml",
                "down",
                "-v",
                "--remove-orphans",
            ],
            cwd=str(args.repo_root.resolve()),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            print(f"ERROR: cleanup failed: {proc.stderr.strip()}", file=sys.stderr)
            return 1
    print(f"OK: isolated Compose project cleanup completed ({project})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
