#!/usr/bin/env python3
"""CI fallback cleanup for gateway routing integration (exact project only).

Removes disposable Docker containers and the project-named bridge network.
Does not use Compose and never touches staging/production project names.
Intended for ``if: always()`` workflow steps so cleanup survives job cancel
and interpreter termination that skip Python ``finally`` handlers.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

PROJECT_RE = re.compile(r"^fetchnow-routing-test-[a-z0-9]{8,32}$")
FORBIDDEN = frozenset(
    {
        "",
        "fetchnow",
        "fetchnow-staging",
        "fetchnow-production",
        "fetchnow-prod",
        "fetchnow-routing-test",
    }
)


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def _validate_project(raw: str) -> str:
    project = "".join(raw.split())
    if project in FORBIDDEN or not PROJECT_RE.fullmatch(project):
        raise ValueError(f"refusing unsafe project name {project!r}")
    return project


def cleanup_project(project: str) -> None:
    project = _validate_project(project)
    listed = _run(["docker", "ps", "-a", "--format", "{{.Names}}"])
    if listed.returncode != 0:
        raise RuntimeError(
            f"docker ps failed: {(listed.stderr or listed.stdout or '').strip()}"
        )
    prefix = f"{project}-"
    targets = [
        name.strip()
        for name in listed.stdout.splitlines()
        if name.strip() == project or name.strip().startswith(prefix)
    ]
    if targets:
        removed = _run(["docker", "rm", "-f", *targets])
        if removed.returncode != 0:
            raise RuntimeError(
                "docker rm failed: "
                f"{(removed.stderr or removed.stdout or '').strip()}"
            )
    net = _run(["docker", "network", "inspect", project])
    if net.returncode == 0:
        gone = _run(["docker", "network", "rm", project])
        if gone.returncode != 0:
            raise RuntimeError(
                "docker network rm failed: "
                f"{(gone.stderr or gone.stdout or '').strip()}"
            )
    print(f"OK: isolated routing project cleanup completed ({project})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-file", type=Path, required=True)
    # Kept for workflow symmetry with sibling cleanup scripts.
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    _ = args.repo_root.resolve()

    if not args.project_file.is_file():
        print(f"No project file at {args.project_file}; refusing cleanup mutation")
        return 0
    try:
        project = _validate_project(args.project_file.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    try:
        cleanup_project(project)
    except Exception as exc:  # noqa: BLE001 — surface exact cleanup failure to CI
        print(f"ERROR: cleanup failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
