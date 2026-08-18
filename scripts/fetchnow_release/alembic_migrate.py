"""Execute Alembic forward migration from the target release API image (PRD1C3B2B2)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .c3b2b2_constants import LABEL_MIGRATION_ID, LABEL_MIGRATION_REVISION
from .redact import redact


class AlembicMigrateError(RuntimeError):
    """Alembic migration execution failure."""


@dataclass(frozen=True)
class AlembicMigrateResult:
    exit_code: int
    command_argv: tuple[str, ...]
    started_at_utc: str
    finished_at_utc: str
    stdout_summary: str
    stderr_summary: str


def build_alembic_upgrade_argv(
    *,
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
    destination: str,
) -> list[str]:
    if not destination.strip():
        raise AlembicMigrateError("Alembic destination must be non-empty")
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
            "run",
            "--rm",
            "--no-deps",
            "--pull",
            "never",
            "api",
            "alembic",
            "upgrade",
            destination,
        ]
    )
    joined = " ".join(argv)
    if "postgres" in argv[argv.index("run") :]:
        raise AlembicMigrateError("migration argv must not target postgres service")
    if "--pull" not in argv or "never" not in argv:
        raise AlembicMigrateError("migration argv must use --pull never")
    if "password" in joined.lower():
        raise AlembicMigrateError("migration argv must not contain password literals")
    return argv


def alembic_destination_for_heads(heads: tuple[str, ...] | frozenset[str]) -> str:
    items = tuple(sorted(frozenset(heads)))
    if not items:
        raise AlembicMigrateError("target heads must be non-empty")
    if len(items) == 1:
        return items[0]
    return "heads"


def run_alembic_upgrade(
    *,
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
    cwd: Path,
    target_heads: tuple[str, ...] | frozenset[str],
    migration_id: str,
    target_revision: str,
    extra_env: dict[str, str] | None = None,
) -> AlembicMigrateResult:
    from datetime import UTC, datetime

    destination = alembic_destination_for_heads(target_heads)
    argv = build_alembic_upgrade_argv(
        project_name=project_name,
        env_file=env_file,
        compose_files=compose_files,
        destination=destination,
    )
    started = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    env = {
        **__import__("os").environ,
        f"COMPOSE_LABEL_{LABEL_MIGRATION_ID}": migration_id,
        f"COMPOSE_LABEL_{LABEL_MIGRATION_REVISION}": target_revision,
    }
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    finished = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return AlembicMigrateResult(
        exit_code=proc.returncode,
        command_argv=tuple(argv),
        started_at_utc=started,
        finished_at_utc=finished,
        stdout_summary=redact((proc.stdout or "")[:2000]),
        stderr_summary=redact((proc.stderr or "")[:2000]),
    )
