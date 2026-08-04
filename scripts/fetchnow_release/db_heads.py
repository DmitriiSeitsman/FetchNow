"""Read-only Alembic heads equality gate (no upgrade/downgrade/stamp)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .c2_constants import SOURCE_DIRNAME
from .redact import redact

# Reuse PRD1B file-scan head discovery without importing backup create/restore.
_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from fetchnow_pg_backup.semantic import discover_alembic_head_revisions  # noqa: E402


class DbHeadsError(RuntimeError):
    """Database / target Alembic heads mismatch or query failure."""


def target_heads_from_release_source(release_dir: Path) -> frozenset[str]:
    versions = (
        release_dir / SOURCE_DIRNAME / "backend" / "migrations" / "versions"
    )
    if not versions.is_dir():
        raise DbHeadsError(f"migrations versions dir missing in release: {versions}")
    heads = discover_alembic_head_revisions(versions)
    return frozenset(heads)


def database_heads_via_postgres(
    *,
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
    cwd: Path,
) -> frozenset[str]:
    """Query alembic_version through the running postgres container.

    Credentials come from the container environment — never argv.
    """
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
    # Use container env for user/db; do not pass password on argv.
    argv.extend(
        [
            "exec",
            "-T",
            "postgres",
            "sh",
            "-c",
            'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 '
            '-tAc "SELECT version_num FROM alembic_version ORDER BY 1"',
        ]
    )
    proc = subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise DbHeadsError(
            "failed to read alembic_version (read-only): "
            + redact(proc.stderr.strip() or proc.stdout.strip() or "unknown")
        )
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    if not lines:
        raise DbHeadsError("alembic_version query returned no rows")
    return frozenset(lines)


def assert_heads_equal(
    *,
    database_heads: frozenset[str],
    target_heads: frozenset[str],
) -> None:
    if database_heads != target_heads:
        raise DbHeadsError(
            "database Alembic heads "
            f"{sorted(database_heads)} do not match target release heads "
            f"{sorted(target_heads)}; PRD1C3B migration is required before "
            "application rollout (PRD1C3A never mutates schema)"
        )
