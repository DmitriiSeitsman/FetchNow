"""Docker / Compose CLI availability (read-only probes)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from .bounded_subprocess import (
    BoundedCancelledError,
    BoundedTimeoutError,
    run_docker_probe,
)


class DockerCheckError(ValueError):
    """Docker tooling unavailable."""


def require_docker_cli() -> None:
    if shutil.which("docker") is None:
        raise DockerCheckError("docker CLI not found on PATH")


def require_compose_v2() -> str:
    require_docker_cli()
    try:
        result = run_docker_probe(["docker", "compose", "version", "--short"])
    except (BoundedTimeoutError, BoundedCancelledError) as exc:
        raise DockerCheckError(
            "Docker Compose v2 probe timed out or was cancelled"
        ) from exc
    if result.returncode != 0:
        raise DockerCheckError(
            "Docker Compose v2 (`docker compose`) is required but not available"
        )
    return result.stdout_text.strip() or "unknown"


def require_docker_daemon() -> None:
    require_docker_cli()
    try:
        result = run_docker_probe(
            ["docker", "info", "--format", "{{.ServerVersion}}"]
        )
    except (BoundedTimeoutError, BoundedCancelledError) as exc:
        raise DockerCheckError("Docker daemon probe timed out or was cancelled") from exc
    if result.returncode != 0:
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
    try:
        result = run_docker_probe(argv, cwd=repo_root, env=env)
    except (BoundedTimeoutError, BoundedCancelledError) as exc:
        raise DockerCheckError(f"compose config timed out or cancelled: {exc}") from exc
    if result.returncode != 0:
        from .redact import redact

        raise DockerCheckError(
            "compose config failed: "
            + redact(result.stderr_text.strip() or result.stdout_text.strip())
        )
    try:
        return json.loads(result.stdout_text)
    except json.JSONDecodeError as exc:
        raise DockerCheckError(f"compose config JSON invalid: {exc}") from exc
