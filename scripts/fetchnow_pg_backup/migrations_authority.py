"""Explicit Alembic migrations-directory and expected-head authority (PRD1C3B2B1)."""

from __future__ import annotations

from pathlib import Path

from .alembic_graph import AlembicGraphError, build_alembic_graph, validate_revision_id
from .checksums import sha256_bytes
from .fs_safety import PathSafetyError, reject_symlink_components


class MigrationsAuthorityError(ValueError):
    """Invalid migrations directory or expected Alembic heads."""


def validate_migrations_versions_dir(path: Path) -> Path:
    """Resolve and validate an authoritative migrations/versions directory."""
    resolved = reject_symlink_components(path.expanduser())
    if not resolved.exists():
        raise MigrationsAuthorityError(
            f"migrations versions directory not found: {resolved}"
        )
    if resolved.is_symlink():
        raise PathSafetyError(
            f"migrations versions directory must not be a symlink: {resolved}"
        )
    if not resolved.is_dir():
        raise MigrationsAuthorityError(
            f"migrations versions path is not a directory: {resolved}"
        )
    return resolved


def migrations_graph_content_sha256(migrations_versions_dir: Path) -> str:
    """Return a stable SHA-256 over sorted migration file names and contents."""
    directory = validate_migrations_versions_dir(migrations_versions_dir)
    files = sorted(
        p
        for p in directory.iterdir()
        if p.is_file() and not p.is_symlink() and p.suffix == ".py"
    )
    if not files:
        raise MigrationsAuthorityError(
            "migrations versions directory has no Python migration files"
        )
    lines: list[str] = []
    for path in files:
        lines.append(f"{path.name}:{sha256_bytes(path.read_bytes())}")
    return sha256_bytes("\n".join(lines).encode())


def resolve_static_migrations_authority(
    migrations_versions_dir: Path,
) -> tuple[tuple[str, ...], str]:
    """Return static Alembic heads and migrations content SHA-256 for one directory."""
    directory = validate_migrations_versions_dir(migrations_versions_dir)
    try:
        graph = build_alembic_graph(directory)
    except (AlembicGraphError, FileNotFoundError, RuntimeError) as exc:
        raise MigrationsAuthorityError(str(exc)) from exc
    return graph.heads, migrations_graph_content_sha256(directory)


def validate_expected_alembic_heads(
    heads: tuple[str, ...] | frozenset[str] | list[str],
) -> tuple[str, ...]:
    """Return sorted unique non-empty expected Alembic head revision IDs."""
    if isinstance(heads, frozenset):
        raw = tuple(heads)
    else:
        raw = tuple(heads)
    if not raw:
        raise MigrationsAuthorityError("expected Alembic heads must be non-empty")
    seen: set[str] = set()
    validated: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise MigrationsAuthorityError(
                "expected Alembic head entries must be non-empty strings"
            )
        rev = validate_revision_id(item.strip(), field="expected Alembic head")
        if rev in seen:
            raise MigrationsAuthorityError(
                f"duplicate expected Alembic head: {rev!r}"
            )
        seen.add(rev)
        validated.append(rev)
    return tuple(sorted(validated))


def static_alembic_heads_from_migrations_dir(
    migrations_versions_dir: Path,
) -> tuple[str, ...]:
    """Discover exact static head set from migration files (fail closed on bad graph)."""
    heads, _graph_sha = resolve_static_migrations_authority(migrations_versions_dir)
    return heads


def assert_expected_matches_static_heads(
    *,
    expected_alembic_heads: tuple[str, ...],
    migrations_versions_dir: Path,
) -> tuple[str, ...]:
    """Require explicit expected heads to equal the static graph head set."""
    expected = validate_expected_alembic_heads(expected_alembic_heads)
    static, _graph_sha = resolve_static_migrations_authority(migrations_versions_dir)
    if frozenset(expected) != frozenset(static):
        raise MigrationsAuthorityError(
            "expected Alembic heads "
            f"{list(expected)} do not equal static migrations graph heads "
            f"{list(static)}"
        )
    return expected
