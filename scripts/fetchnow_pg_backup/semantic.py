"""Application-aware schema verification after restore."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import ALEMBIC_VERSION_TABLE, REQUIRED_PUBLIC_TABLES
from .compose_target import ComposeTarget
from .postgres_ops import RunFn, psql_on_database
from .process_util import run_command


@dataclass(frozen=True)
class SemanticCheckResult:
    name: str
    ok: bool
    detail: str


def discover_alembic_head_revisions(migrations_versions_dir: Path) -> tuple[str, ...]:
    """Discover Alembic head revision IDs from migration files (no DB access)."""
    from .alembic_graph import discover_alembic_head_revisions as _discover

    return _discover(migrations_versions_dir)


def build_table_semantic_sql() -> str:
    """SQL script for non-head semantic checks (tables, invalid indexes)."""
    table_checks = "\n".join(
        f"SELECT 'table_{t}=' || CASE WHEN to_regclass('public.{t}') IS NOT NULL "
        f"THEN 'ok' ELSE 'missing' END;"
        for t in REQUIRED_PUBLIC_TABLES
    )
    return f"""
SELECT 'connection=ok';
{table_checks}
SELECT 'invalid_indexes=' || COUNT(*)::text FROM pg_index WHERE NOT indisvalid;
"""


def build_semantic_sql(*, expected_revisions: tuple[str, ...]) -> str:
    """SQL script that exits on first error (ON_ERROR_STOP)."""
    if not expected_revisions:
        raise ValueError("expected_revisions must be non-empty")
    rev_list = ", ".join("'" + r.replace("'", "''") + "'" for r in expected_revisions)
    table_checks = "\n".join(
        f"SELECT 'table_{t}=' || CASE WHEN to_regclass('public.{t}') IS NOT NULL "
        f"THEN 'ok' ELSE 'missing' END;"
        for t in REQUIRED_PUBLIC_TABLES
    )
    return f"""
SELECT 'connection=ok';
SELECT 'alembic_table=' || CASE WHEN to_regclass('public.{ALEMBIC_VERSION_TABLE}') IS NOT NULL
     THEN 'ok' ELSE 'missing' END;
SELECT 'alembic_revision=' || version_num FROM {ALEMBIC_VERSION_TABLE};
SELECT 'alembic_revision_valid=' || CASE WHEN version_num IN ({rev_list})
     THEN 'ok' ELSE 'unexpected' END FROM {ALEMBIC_VERSION_TABLE};
{table_checks}
SELECT 'invalid_indexes=' || COUNT(*)::text FROM pg_index WHERE NOT indisvalid;
"""


def _kv(lines: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for ln in lines:
        if "=" in ln:
            key, val = ln.split("=", 1)
            out[key.strip()] = val.strip()
    return out


def run_table_semantic_checks(
    target: ComposeTarget,
    *,
    database: str,
    source_database: str,
    runner: RunFn = run_command,
) -> list[SemanticCheckResult]:
    """Run semantic checks excluding Alembic head equality (handled separately)."""
    sql = build_table_semantic_sql()
    output = psql_on_database(
        target,
        database,
        sql,
        source_database=source_database,
        runner=runner,
    )
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    kv = _kv(lines)
    results: list[SemanticCheckResult] = [
        SemanticCheckResult(
            "connection", kv.get("connection") == "ok", kv.get("connection", "")
        ),
    ]
    for table in REQUIRED_PUBLIC_TABLES:
        key = f"table_{table}"
        results.append(SemanticCheckResult(key, kv.get(key) == "ok", kv.get(key, "")))
    results.append(
        SemanticCheckResult(
            "invalid_indexes",
            kv.get("invalid_indexes") == "0",
            kv.get("invalid_indexes", ""),
        )
    )
    return results


def run_semantic_checks(
    target: ComposeTarget,
    *,
    database: str,
    source_database: str,
    expected_revisions: tuple[str, ...],
    runner: RunFn = run_command,
) -> list[SemanticCheckResult]:
    sql = build_semantic_sql(expected_revisions=expected_revisions)
    output = psql_on_database(
        target,
        database,
        sql,
        source_database=source_database,
        runner=runner,
    )
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    kv = _kv(lines)
    results: list[SemanticCheckResult] = [
        SemanticCheckResult(
            "connection", kv.get("connection") == "ok", kv.get("connection", "")
        ),
        SemanticCheckResult(
            "alembic_table",
            kv.get("alembic_table") == "ok",
            kv.get("alembic_table", ""),
        ),
        SemanticCheckResult(
            "alembic_revision",
            kv.get("alembic_revision") in expected_revisions,
            kv.get("alembic_revision", ""),
        ),
        SemanticCheckResult(
            "alembic_revision_valid",
            kv.get("alembic_revision_valid") == "ok",
            kv.get("alembic_revision_valid", ""),
        ),
    ]
    for table in REQUIRED_PUBLIC_TABLES:
        key = f"table_{table}"
        results.append(SemanticCheckResult(key, kv.get(key) == "ok", kv.get(key, "")))
    results.append(
        SemanticCheckResult(
            "invalid_indexes",
            kv.get("invalid_indexes") == "0",
            kv.get("invalid_indexes", ""),
        )
    )
    return results


def all_checks_passed(results: list[SemanticCheckResult]) -> bool:
    return all(r.ok for r in results)
