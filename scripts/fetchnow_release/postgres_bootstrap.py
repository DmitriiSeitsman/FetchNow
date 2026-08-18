"""Start ONLY the postgres service from an immutable release snapshot."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .c3b3_constants import LABEL_BOOTSTRAP_ID, LABEL_BOOTSTRAP_REVISION
from .redact import redact


class PostgresBootstrapError(RuntimeError):
    """Postgres-only compose activation failure."""


def build_postgres_up_argv(
    *,
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
) -> list[str]:
    if not compose_files:
        raise PostgresBootstrapError("at least one Compose file is required")
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
            "postgres",
        ]
    )
    _assert_postgres_only_up(argv)
    return argv


def _assert_postgres_only_up(argv: list[str]) -> None:
    if "up" not in argv or argv.count("up") != 1:
        raise PostgresBootstrapError("postgres start argv must contain exactly one up")
    if "--no-build" not in argv or "--no-deps" not in argv:
        raise PostgresBootstrapError("postgres start argv missing --no-build/--no-deps")
    if "--pull" not in argv or "never" not in argv:
        raise PostgresBootstrapError("postgres start argv must use --pull never")
    never_at = argv.index("never")
    if argv[never_at + 1 :] != ["postgres"]:
        raise PostgresBootstrapError(
            "postgres start argv must target postgres only, got "
            f"{argv[never_at + 1 :]}"
        )
    up_at = argv.index("up")
    forbidden = {"api", "worker", "web", "delivery", "gateway", "storage-init"}
    if forbidden.intersection(argv[up_at:]):
        raise PostgresBootstrapError(
            "postgres start argv must not mention application services"
        )
    joined = " ".join(argv).lower()
    if "password" in joined:
        raise PostgresBootstrapError("postgres start argv must not contain password literals")
    if "down" in argv or "-v" in argv:
        raise PostgresBootstrapError("postgres start argv must not tear down volumes")


def start_postgres_only(
    *,
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
    cwd: Path,
    bootstrap_id: str,
    target_revision: str,
) -> None:
    argv = build_postgres_up_argv(
        project_name=project_name,
        env_file=env_file,
        compose_files=compose_files,
    )
    env = {
        **os.environ,
        f"COMPOSE_LABEL_{LABEL_BOOTSTRAP_ID}": bootstrap_id,
        f"COMPOSE_LABEL_{LABEL_BOOTSTRAP_REVISION}": target_revision,
    }
    proc = subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if proc.returncode != 0:
        raise PostgresBootstrapError(
            "compose up postgres failed: "
            + redact(proc.stderr.strip() or proc.stdout.strip() or "unknown")
        )
