"""Docker / Compose CLI availability (read-only probes)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


class DockerCheckError(ValueError):
    """Docker tooling unavailable."""


def require_docker_cli() -> None:
    if shutil.which("docker") is None:
        raise DockerCheckError("docker CLI not found on PATH")


def require_compose_v2() -> str:
    require_docker_cli()
    proc = subprocess.run(
        ["docker", "compose", "version", "--short"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise DockerCheckError(
            "Docker Compose v2 (`docker compose`) is required but not available"
        )
    return proc.stdout.strip() or "unknown"


def require_docker_daemon() -> None:
    require_docker_cli()
    proc = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise DockerCheckError("Docker daemon is not responding")


def compose_config_json(
    *,
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
    repo_root: Path,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    argv = [
        "docker",
        "compose",
        "--env-file",
        str(env_file),
        "--project-name",
        project_name,
    ]
    for path in compose_files:
        argv.extend(["-f", str(path)])
    argv.extend(["config", "--format", "json"])
    env = {
        **dict(**{k: v for k, v in __import__("os").environ.items()}),
        **(extra_env or {}),
    }
    # Avoid host .env leaking: compose still may load project .env; operators
    # should run with explicit --env-file. We do not mutate files.
    proc = subprocess.run(
        argv,
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if proc.returncode != 0:
        from .redact import redact

        raise DockerCheckError(
            "compose config failed: "
            + redact(proc.stderr.strip() or proc.stdout.strip())
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise DockerCheckError(f"compose config JSON invalid: {exc}") from exc
