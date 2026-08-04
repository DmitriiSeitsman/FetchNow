"""Activate application containers using immutable image overrides."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .c3_constants import APP_SERVICES_BEFORE_GATEWAY, APPLICATION_SERVICES
from .redact import redact


class ActivateError(RuntimeError):
    """Compose activation failure."""


def build_activate_argv(
    *,
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
    services: tuple[str, ...],
) -> list[str]:
    """Build compose up argv for application services only."""
    if not services:
        raise ActivateError("at least one service required")
    forbidden = {"postgres"}
    bad = set(services) & forbidden
    if bad:
        raise ActivateError(f"refusing to mutate postgres in rollout: {sorted(bad)}")
    unknown = set(services) - set(APPLICATION_SERVICES)
    if unknown:
        raise ActivateError(f"unknown application services: {sorted(unknown)}")
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
    argv.extend(
        [
            "up",
            "-d",
            "--no-build",
            "--no-deps",
            "--pull",
            "never",
            *services,
        ]
    )
    return argv


def activate_services(
    *,
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
    cwd: Path,
    services: tuple[str, ...],
) -> None:
    argv = build_activate_argv(
        project_name=project_name,
        env_file=env_file,
        compose_files=compose_files,
        services=services,
    )
    # Guard: argv must contain contract flags and must not mention postgres.
    joined = " ".join(argv)
    if "--no-build" not in argv or "--no-deps" not in argv:
        raise ActivateError("activation argv missing --no-build/--no-deps")
    if "postgres" in argv:
        raise ActivateError("activation argv must not include postgres")
    proc = subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise ActivateError(
            "compose up failed: "
            + redact(proc.stderr.strip() or proc.stdout.strip() or joined)
        )


def activate_app_tier(
    *,
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
    cwd: Path,
) -> None:
    activate_services(
        project_name=project_name,
        env_file=env_file,
        compose_files=compose_files,
        cwd=cwd,
        services=APP_SERVICES_BEFORE_GATEWAY,
    )


def activate_gateway(
    *,
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
    cwd: Path,
) -> None:
    activate_services(
        project_name=project_name,
        env_file=env_file,
        compose_files=compose_files,
        cwd=cwd,
        services=("gateway",),
    )
