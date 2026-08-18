"""Read-only Alembic heads equality gate (no upgrade/downgrade/stamp)."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .c2_constants import SOURCE_DIRNAME
from .readonly_guard import assert_readonly_subprocess
from .redact import redact

# Reuse PRD1B file-scan head discovery without importing backup create/restore.
_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from fetchnow_pg_backup.alembic_graph import validate_revision_id  # noqa: E402
from fetchnow_pg_backup.semantic import discover_alembic_head_revisions  # noqa: E402


class DbHeadsError(RuntimeError):
    """Database / target Alembic heads mismatch or query failure."""


@dataclass(frozen=True)
class DatabaseHeadsProbe:
    """Read-only classification of live alembic_version state."""

    status: str
    heads: frozenset[str]
    detail: str = ""

    @property
    def table_missing(self) -> bool:
        return self.status == "missing_table"

    @property
    def has_heads(self) -> bool:
        return self.status == "heads" and bool(self.heads)


def target_heads_from_release_source(release_dir: Path) -> frozenset[str]:
    versions = (
        release_dir / SOURCE_DIRNAME / "backend" / "migrations" / "versions"
    )
    if not versions.is_dir():
        raise DbHeadsError(f"migrations versions dir missing in release: {versions}")
    heads = discover_alembic_head_revisions(versions)
    return frozenset(heads)


def _alembic_version_select_argv(
    *,
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
) -> list[str]:
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
    return argv


def probe_database_heads(
    *,
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
    cwd: Path,
) -> DatabaseHeadsProbe:
    """Classify live alembic_version without treating missing-table as heads."""
    argv = _alembic_version_select_argv(
        project_name=project_name,
        env_file=env_file,
        compose_files=compose_files,
    )
    assert_readonly_subprocess(argv)
    proc = subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    detail = redact(proc.stderr.strip() or proc.stdout.strip() or "unknown")
    if proc.returncode != 0:
        lowered = detail.lower()
        if "alembic_version" in lowered and "does not exist" in lowered:
            return DatabaseHeadsProbe(status="missing_table", heads=frozenset())
        return DatabaseHeadsProbe(
            status="query_failed",
            heads=frozenset(),
            detail=detail,
        )
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    if not lines:
        if "alembic_version" in detail.lower() and "does not exist" in detail.lower():
            return DatabaseHeadsProbe(status="missing_table", heads=frozenset())
        return DatabaseHeadsProbe(
            status="empty_table",
            heads=frozenset(),
            detail="alembic_version query returned no rows",
        )
    heads: set[str] = set()
    for line in lines:
        heads.add(validate_revision_id(line, field="database head"))
    return DatabaseHeadsProbe(status="heads", heads=frozenset(heads))


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
    probe = probe_database_heads(
        project_name=project_name,
        env_file=env_file,
        compose_files=compose_files,
        cwd=cwd,
    )
    if probe.table_missing:
        raise DbHeadsError(
            "alembic_version table is missing — database is not initialized"
        )
    if probe.status == "empty_table":
        raise DbHeadsError("alembic_version query returned no rows")
    if probe.status == "query_failed":
        raise DbHeadsError(
            "failed to read alembic_version (read-only): " + probe.detail
        )
    if probe.status != "heads" or not probe.heads:
        raise DbHeadsError("alembic_version query returned no rows")
    return probe.heads


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
