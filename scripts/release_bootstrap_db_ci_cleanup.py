#!/usr/bin/env python3
"""CI fallback cleanup for initial DB bootstrap integration (exact project only)."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from fetchnow_release.bootstrap_project import (  # noqa: E402
    BootstrapProjectError,
    load_bootstrap_project_file,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-file", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)

    if not args.project_file.is_file():
        print(f"No project file at {args.project_file}; refusing cleanup mutation")
        return 0
    try:
        project = load_bootstrap_project_file(args.project_file)
    except BootstrapProjectError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    repo_root = args.repo_root.resolve()
    compose_files = (repo_root / "compose.yaml", repo_root / "compose.staging.yaml")
    if not all(path.is_file() for path in compose_files):
        print("ERROR: required Compose files not found", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="fetchnow-bootstrap-cleanup-") as tmp:
        env_file = Path(tmp) / ".env"
        env_file.write_text(
            "\n".join(
                [
                    f"COMPOSE_PROJECT_NAME={project}",
                    "FETCHNOW_RELEASE_REVISION=" + ("a" * 40),
                    "GATEWAY_PORT=127.0.0.1:19999",
                    "PUBLIC_SITE_URL=http://127.0.0.1",
                    "POSTGRES_DB=fetchnow",
                    "POSTGRES_USER=fetchnow",
                    "POSTGRES_PASSWORD=cleanup-placeholder-not-used-xxxxxxxx",
                    "FETCHNOW_DEPLOY_ROOT=/tmp/fetchnow-bootstrap-cleanup-deploy",
                    "FETCHNOW_BACKUP_ROOT=/tmp/fetchnow-bootstrap-cleanup-backup",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        proc = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                str(env_file),
                "--project-name",
                project,
                "-f",
                str(compose_files[0]),
                "-f",
                str(compose_files[1]),
                "down",
                "-v",
                "--remove-orphans",
            ],
            cwd=str(repo_root),
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
