"""Read-only freshness inspection for initial database bootstrap."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .c3_constants import RUNTIME_APPLICATION_SERVICES
from .c3b3_constants import (
    BOOTSTRAP_ALLOWED_EXTRA_SCHEMAS,
    BOOTSTRAP_ALLOWED_USER_RELATIONS,
    BOOTSTRAP_USER_RELKINDS,
    COMPOSE_PROJECT_LABEL,
    COMPOSE_VOLUME_LABEL,
    PGDATA_VOLUME_KEY,
)
from .db_heads import DatabaseHeadsProbe, probe_database_heads
from .readonly_guard import assert_readonly_subprocess
from .redact import redact
from .stabilize import _compose_ps

# Catalog SELECT used by probe_user_catalog. Keep this free of DML and of
# double quotes so it can be embedded in `psql -tAc "..."`.
# Extension-owned objects (pg_depend.deptype = 'e') are excluded so that
# enabling an extension is not mistaken for application data.
USER_CATALOG_SQL = (
    "SELECT 'relation' || chr(9) || n.nspname || '.' || c.relname || ':' || c.relkind::text "
    "FROM pg_catalog.pg_class c "
    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname <> 'pg_catalog' "
    "AND n.nspname <> 'information_schema' "
    "AND n.nspname <> 'pg_toast' "
    "AND n.nspname NOT LIKE 'pg_temp_%' "
    "AND n.nspname NOT LIKE 'pg_toast_temp_%' "
    "AND c.relkind IN ('r','p','v','m','S','f') "
    "AND NOT EXISTS ("
    "SELECT 1 FROM pg_catalog.pg_depend d "
    "WHERE d.objid = c.oid AND d.deptype = 'e'"
    ") "
    "UNION ALL "
    "SELECT 'schema' || chr(9) || n.nspname "
    "FROM pg_catalog.pg_namespace n "
    "WHERE n.nspname <> 'pg_catalog' "
    "AND n.nspname <> 'information_schema' "
    "AND n.nspname <> 'pg_toast' "
    "AND n.nspname <> 'public' "
    "AND n.nspname NOT LIKE 'pg_temp_%' "
    "AND n.nspname NOT LIKE 'pg_toast_temp_%' "
    "AND NOT EXISTS ("
    "SELECT 1 FROM pg_catalog.pg_depend d "
    "WHERE d.objid = n.oid AND d.deptype = 'e'"
    ") "
    "ORDER BY 1"
)


class BootstrapFreshnessError(RuntimeError):
    """Freshness inspection failed closed."""


CLASS_CLEAN = "clean"
CLASS_UNINITIALIZED = "uninitialized"
CLASS_INITIALIZED = "initialized"
CLASS_APP_PRESENT = "application_present"
CLASS_AMBIGUOUS = "ambiguous"
CLASS_DIRTY = "dirty"


@dataclass(frozen=True)
class UserCatalogProbe:
    """Read-only classification of non-system user/application catalog objects."""

    status: str
    relations: tuple[str, ...]
    extra_schemas: tuple[str, ...]
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass(frozen=True)
class BootstrapFreshness:
    classification: str
    reason: str
    application_services: tuple[str, ...]
    postgres_state: str | None
    postgres_health: str | None
    postgres_healthy: bool
    project_volumes: tuple[str, ...]
    pgdata_volume_present: bool
    alembic_probe: DatabaseHeadsProbe | None
    user_relations: tuple[str, ...] = ()
    extra_schemas: tuple[str, ...] = ()
    catalog_probe: UserCatalogProbe | None = None

    @property
    def safe_for_initial_plan(self) -> bool:
        return self.classification in {CLASS_CLEAN, CLASS_UNINITIALIZED}

    @property
    def uninitialized(self) -> bool:
        return self.classification in {CLASS_CLEAN, CLASS_UNINITIALIZED}

    @property
    def initialized_heads(self) -> frozenset[str] | None:
        if self.classification != CLASS_INITIALIZED or self.alembic_probe is None:
            return None
        return self.alembic_probe.heads


def unexpected_catalog_objects(
    *,
    relations: tuple[str, ...],
    extra_schemas: tuple[str, ...],
    allowed_relations: frozenset[str] = BOOTSTRAP_ALLOWED_USER_RELATIONS,
    allowed_schemas: frozenset[str] = BOOTSTRAP_ALLOWED_EXTRA_SCHEMAS,
) -> tuple[str, ...]:
    """Return unexpected user objects. Allowlist is explicit and narrow."""
    unexpected: list[str] = []
    for rel in relations:
        kind = rel.rsplit(":", 1)[-1] if ":" in rel else ""
        if kind and kind not in BOOTSTRAP_USER_RELKINDS:
            unexpected.append(rel)
            continue
        if rel not in allowed_relations:
            unexpected.append(rel)
    for schema in extra_schemas:
        if schema not in allowed_schemas:
            unexpected.append(f"{schema}:schema")
    return tuple(unexpected)


def classify_healthy_postgres_schema(
    alembic: DatabaseHeadsProbe,
    catalog: UserCatalogProbe,
) -> tuple[str, str]:
    """Classify a healthy postgres after Alembic + catalog probes.

    Missing ``alembic_version`` is not sufficient proof of a fresh database.
    """
    if not catalog.ok:
        return (
            CLASS_AMBIGUOUS,
            "database catalog inspection failed; refusing bootstrap: "
            + (catalog.detail or "unknown"),
        )
    unexpected = unexpected_catalog_objects(
        relations=catalog.relations,
        extra_schemas=catalog.extra_schemas,
    )
    if alembic.table_missing:
        if unexpected:
            listed = ", ".join(unexpected[:20])
            extra = "" if len(unexpected) <= 20 else f" (+{len(unexpected) - 20} more)"
            return (
                CLASS_DIRTY,
                "alembic_version is absent but unexpected user objects exist: "
                f"{listed}{extra}",
            )
        return (
            CLASS_UNINITIALIZED,
            "postgres is healthy, alembic_version is absent, and the "
            "application schema has no unexpected user relations",
        )
    if alembic.has_heads:
        return (
            CLASS_INITIALIZED,
            "database already has Alembic heads "
            f"{sorted(alembic.heads)}; not a proven-empty bootstrap",
        )
    return (
        CLASS_AMBIGUOUS,
        "alembic_version probe is inconclusive "
        f"({alembic.status}: {alembic.detail or 'no detail'})",
    )


def _user_catalog_select_argv(
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
    argv.extend(
        [
            "exec",
            "-T",
            "postgres",
            "sh",
            "-c",
            'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 '
            f'-tAc "{USER_CATALOG_SQL}"',
        ]
    )
    return argv


def probe_user_catalog(
    *,
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
    cwd: Path,
) -> UserCatalogProbe:
    """List non-system, non-extension user relations and extra schemas."""
    argv = _user_catalog_select_argv(
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
        return UserCatalogProbe(
            status="query_failed",
            relations=(),
            extra_schemas=(),
            detail=detail,
        )
    relations: list[str] = []
    extra_schemas: list[str] = []
    for raw in proc.stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        kind, sep, ident = line.partition("\t")
        if not sep or not ident:
            return UserCatalogProbe(
                status="query_failed",
                relations=(),
                extra_schemas=(),
                detail="malformed catalog row",
            )
        if kind == "relation":
            relations.append(ident)
        elif kind == "schema":
            extra_schemas.append(ident)
        else:
            return UserCatalogProbe(
                status="query_failed",
                relations=(),
                extra_schemas=(),
                detail="malformed catalog row kind",
            )
    return UserCatalogProbe(
        status="ok",
        relations=tuple(relations),
        extra_schemas=tuple(extra_schemas),
    )


def list_project_volumes(
    *,
    project_name: str,
) -> tuple[str, ...]:
    """List Compose volumes labeled for this project (read-only)."""
    argv = [
        "docker",
        "volume",
        "ls",
        "--filter",
        f"label={COMPOSE_PROJECT_LABEL}={project_name}",
        "--format",
        "{{.Name}}",
    ]
    assert_readonly_subprocess(argv)
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise BootstrapFreshnessError(
            "docker volume ls failed: "
            + redact(proc.stderr.strip() or proc.stdout.strip() or "unknown")
        )
    names = tuple(sorted(ln.strip() for ln in proc.stdout.splitlines() if ln.strip()))
    return names


def _pgdata_present(volume_names: tuple[str, ...], *, project_name: str) -> bool:
    expected = f"{project_name}_{PGDATA_VOLUME_KEY}"
    if expected in volume_names:
        return True
    argv = [
        "docker",
        "volume",
        "ls",
        "--filter",
        f"label={COMPOSE_PROJECT_LABEL}={project_name}",
        "--filter",
        f"label={COMPOSE_VOLUME_LABEL}={PGDATA_VOLUME_KEY}",
        "--format",
        "{{.Name}}",
    ]
    assert_readonly_subprocess(argv)
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise BootstrapFreshnessError(
            "docker volume ls (pgdata) failed: "
            + redact(proc.stderr.strip() or "unknown")
        )
    return any(ln.strip() for ln in proc.stdout.splitlines())


def _application_services_present(rows: list[dict]) -> tuple[str, ...]:
    app = set(RUNTIME_APPLICATION_SERVICES)
    present: list[str] = []
    for row in rows:
        svc = str(row.get("Service") or row.get("service") or "")
        state = str(row.get("State") or "").lower()
        if svc in app and state in {"running", "created", "restarting", "paused"}:
            present.append(svc)
    return tuple(sorted(set(present)))


def _postgres_row(rows: list[dict]) -> dict | None:
    for row in rows:
        svc = str(row.get("Service") or row.get("service") or "")
        if svc == "postgres":
            return row
    return None


def inspect_bootstrap_freshness(
    *,
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
    cwd: Path,
    query_database: bool = True,
) -> BootstrapFreshness:
    """Classify whether this environment is safe for initial schema bootstrap.

    Read-only: never starts postgres, never mutates volumes.
    ``current.json`` absence is NOT treated as proof of an empty database.
    Missing ``alembic_version`` is NOT treated as proof of a fresh database:
    the application schema must also contain no unexpected user relations.
    """
    rows = _compose_ps(
        project_name=project_name,
        env_file=env_file,
        compose_files=compose_files,
        cwd=cwd,
    )
    app_services = _application_services_present(rows)
    if app_services:
        return BootstrapFreshness(
            classification=CLASS_APP_PRESENT,
            reason=(
                "application containers already exist: " + ", ".join(app_services)
            ),
            application_services=app_services,
            postgres_state=None,
            postgres_health=None,
            postgres_healthy=False,
            project_volumes=(),
            pgdata_volume_present=False,
            alembic_probe=None,
        )

    volumes = list_project_volumes(project_name=project_name)
    pgdata = _pgdata_present(volumes, project_name=project_name)
    pg_row = _postgres_row(rows)
    pg_state = None if pg_row is None else str(pg_row.get("State") or "").lower()
    pg_health = None if pg_row is None else str(pg_row.get("Health") or "").lower()
    postgres_present = pg_row is not None and pg_state not in {"", "exited", "dead"}
    postgres_healthy = bool(
        pg_row is not None and pg_state == "running" and pg_health in {"healthy", ""}
    )

    if not postgres_present and not pgdata and not volumes:
        return BootstrapFreshness(
            classification=CLASS_CLEAN,
            reason="no application containers, no postgres container, no project volumes",
            application_services=(),
            postgres_state=pg_state,
            postgres_health=pg_health,
            postgres_healthy=False,
            project_volumes=volumes,
            pgdata_volume_present=False,
            alembic_probe=None,
        )

    if not postgres_healthy:
        return BootstrapFreshness(
            classification=CLASS_AMBIGUOUS,
            reason=(
                "postgres container or project volume exists but postgres is not "
                "healthy, so emptiness cannot be proven without mutation"
            ),
            application_services=(),
            postgres_state=pg_state,
            postgres_health=pg_health,
            postgres_healthy=False,
            project_volumes=volumes,
            pgdata_volume_present=pgdata,
            alembic_probe=None,
        )

    if not query_database:
        return BootstrapFreshness(
            classification=CLASS_AMBIGUOUS,
            reason="postgres is healthy but database was not queried",
            application_services=(),
            postgres_state=pg_state,
            postgres_health=pg_health,
            postgres_healthy=True,
            project_volumes=volumes,
            pgdata_volume_present=pgdata,
            alembic_probe=None,
        )

    probe = probe_database_heads(
        project_name=project_name,
        env_file=env_file,
        compose_files=compose_files,
        cwd=cwd,
    )
    catalog = probe_user_catalog(
        project_name=project_name,
        env_file=env_file,
        compose_files=compose_files,
        cwd=cwd,
    )
    classification, reason = classify_healthy_postgres_schema(probe, catalog)
    return BootstrapFreshness(
        classification=classification,
        reason=reason,
        application_services=(),
        postgres_state=pg_state,
        postgres_health=pg_health,
        postgres_healthy=True,
        project_volumes=volumes,
        pgdata_volume_present=pgdata,
        alembic_probe=probe,
        user_relations=catalog.relations,
        extra_schemas=catalog.extra_schemas,
        catalog_probe=catalog,
    )


def freshness_to_debug_dict(freshness: BootstrapFreshness) -> dict[str, object]:
    """Machine-readable freshness facts (no secrets)."""
    probe = freshness.alembic_probe
    return {
        "classification": freshness.classification,
        "reason": freshness.reason,
        "application_services": list(freshness.application_services),
        "postgres_state": freshness.postgres_state,
        "postgres_healthy": freshness.postgres_healthy,
        "project_volumes": list(freshness.project_volumes),
        "pgdata_volume_present": freshness.pgdata_volume_present,
        "alembic_status": None if probe is None else probe.status,
        "alembic_heads": None if probe is None else sorted(probe.heads),
        "user_relations": list(freshness.user_relations),
        "extra_schemas": list(freshness.extra_schemas),
        "catalog_status": None
        if freshness.catalog_probe is None
        else freshness.catalog_probe.status,
    }


def dump_freshness_json(freshness: BootstrapFreshness) -> str:
    return json.dumps(freshness_to_debug_dict(freshness), sort_keys=True)
