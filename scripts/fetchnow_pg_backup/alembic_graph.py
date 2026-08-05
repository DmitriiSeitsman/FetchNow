"""Static Alembic revision graph discovery (no import/execution of migrations)."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

REVISION_ID_RE = re.compile(r"^[a-zA-Z0-9_]+$")


class AlembicGraphError(ValueError):
    """Invalid or ambiguous Alembic revision metadata."""


class AlembicTransitionError(ValueError):
    """Invalid head-set transition for deployment planning."""


_MISSING = object()


def validate_revision_id(revision: str, *, field: str = "revision") -> str:
    if not REVISION_ID_RE.fullmatch(revision):
        raise AlembicGraphError(f"{field} has invalid revision ID: {revision!r}")
    return revision


def _literal_string(node: ast.AST, *, field: str) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        value = node.value.strip()
        if not value:
            raise AlembicGraphError(f"{field} must be a non-empty string literal")
        validate_revision_id(value, field=field)
        return value
    raise AlembicGraphError(f"{field} must be a static string literal")


def _literal_none_or_parents(node: ast.AST, *, field: str) -> tuple[str, ...] | None:
    if isinstance(node, ast.Constant) and node.value is None:
        return None
    if isinstance(node, ast.Tuple | ast.List):
        parents: list[str] = []
        seen: set[str] = set()
        for elt in node.elts:
            parent = _literal_string(elt, field=f"{field} member")
            if parent in seen:
                raise AlembicGraphError(f"duplicate parent in {field}: {parent!r}")
            seen.add(parent)
            parents.append(parent)
        if not parents:
            raise AlembicGraphError(f"{field} tuple/list must not be empty")
        return tuple(sorted(parents))
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return (_literal_string(node, field=field),)
    raise AlembicGraphError(
        f"{field} must be None, a static string, or a tuple/list of static strings"
    )


def _assignment_targets_name(node: ast.Assign) -> str | None:
    if len(node.targets) != 1:
        return None
    target = node.targets[0]
    if isinstance(target, ast.Name):
        return target.id
    return None


def _iter_metadata_assignments(tree: ast.AST) -> list[tuple[str, ast.AST]]:
    items: list[tuple[str, ast.AST]] = []
    if not isinstance(tree, ast.Module):
        return items
    for node in tree.body:
        if isinstance(node, ast.Assign):
            name = _assignment_targets_name(node)
            if name is not None:
                items.append((name, node.value))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                items.append((node.target.id, node.value))
    return items


def _parse_migration_metadata(path: Path) -> tuple[str, tuple[str, ...] | None, bool]:
    """Return (revision, down_revision parents or None, has_depends_on)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        raise AlembicGraphError(f"syntax error in {path.name}: {exc}") from exc

    revision: str | None = None
    down_revision: tuple[str, ...] | None | object = _MISSING
    has_depends_on = False

    for name, value in _iter_metadata_assignments(tree):
        if name == "revision":
            revision = _literal_string(value, field="revision")
        elif name == "down_revision":
            down_revision = _literal_none_or_parents(value, field="down_revision")
        elif name == "depends_on":
            has_depends_on = True
            _literal_none_or_parents(value, field="depends_on")

    if revision is None:
        raise AlembicGraphError(f"{path.name} missing static revision assignment")

    if down_revision is _MISSING:
        raise AlembicGraphError(f"{path.name} missing down_revision assignment")

    return revision, down_revision, has_depends_on


def _looks_like_migration(path: Path) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return True
    for name, _value in _iter_metadata_assignments(tree):
        if name in {"revision", "down_revision", "depends_on"}:
            return True
    return False


@dataclass(frozen=True)
class AlembicGraph:
    """Immutable Alembic revision graph derived from static AST parsing."""

    revisions: dict[str, tuple[str, ...] | None]
    heads: tuple[str, ...]

    def ancestors(self, revision_ids: frozenset[str]) -> frozenset[str]:
        """All revisions reachable backward from the given heads (including them)."""
        unknown = revision_ids - frozenset(self.revisions)
        if unknown:
            raise AlembicGraphError(
                f"unknown revision IDs in head set: {sorted(unknown)}"
            )
        result: set[str] = set()
        stack = list(revision_ids)
        while stack:
            rev = stack.pop()
            if rev in result:
                continue
            result.add(rev)
            parents = self.revisions[rev]
            if parents:
                stack.extend(parents)
        return frozenset(result)

    def included_revisions(
        self, current_heads: frozenset[str], target_heads: frozenset[str]
    ) -> frozenset[str]:
        """Revisions applied on a valid forward upgrade path."""
        if current_heads == target_heads:
            return frozenset()
        current_ancestors = self.ancestors(current_heads)
        target_ancestors = self.ancestors(target_heads)
        return frozenset(target_ancestors - current_ancestors - current_heads)

    def classify_transition(
        self, current_heads: frozenset[str], target_heads: frozenset[str]
    ) -> str:
        """Return equal, forward, downgrade, or divergent."""
        if current_heads == target_heads:
            return "equal"
        current_ancestors = self.ancestors(current_heads)
        target_ancestors = self.ancestors(target_heads)
        if target_ancestors <= current_ancestors:
            return "downgrade"
        if not current_heads.issubset(target_ancestors):
            return "divergent"
        return "forward"


def merge_alembic_graphs(*graphs: AlembicGraph) -> AlembicGraph:
    """Merge revision metadata from multiple graphs for transition classification."""
    revisions: dict[str, tuple[str, ...] | None] = {}
    for graph in graphs:
        for rev, parents in graph.revisions.items():
            existing = revisions.get(rev)
            if existing is not None and existing != parents:
                raise AlembicGraphError(
                    f"conflicting down_revision metadata for revision {rev!r}"
                )
            revisions[rev] = parents
    pointed: set[str] = set()
    for parents in revisions.values():
        if parents:
            pointed.update(parents)
    heads = tuple(sorted(rev for rev in revisions if rev not in pointed))
    if not heads:
        raise AlembicGraphError("merged revision graph has no heads")
    return AlembicGraph(revisions=revisions, heads=heads)


def build_alembic_graph(migrations_versions_dir: Path) -> AlembicGraph:
    """Build a validated revision graph from migration files."""
    if not migrations_versions_dir.is_dir():
        raise FileNotFoundError(f"migrations dir not found: {migrations_versions_dir}")

    revisions: dict[str, tuple[str, ...] | None] = {}
    sources: dict[str, str] = {}

    for path in sorted(migrations_versions_dir.glob("*.py")):
        if path.name.startswith("__"):
            continue
        try:
            rev, parents, _depends = _parse_migration_metadata(path)
        except AlembicGraphError:
            if _looks_like_migration(path):
                raise
            continue
        if rev in revisions:
            raise AlembicGraphError(f"duplicate revision ID {rev!r}")
        revisions[rev] = parents
        sources[rev] = path.name

    if not revisions:
        raise RuntimeError("no Alembic head revisions discovered")

    for rev, parents in revisions.items():
        if parents is None:
            continue
        for parent in parents:
            if parent == rev:
                raise AlembicGraphError(
                    f"revision {rev!r} self-references in down_revision"
                )
            if parent not in revisions:
                raise AlembicGraphError(
                    f"revision {rev!r} ({sources[rev]}) missing parent {parent!r}"
                )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(rev: str) -> None:
        if rev in visiting:
            raise AlembicGraphError(f"cycle detected involving revision {rev!r}")
        if rev in visited:
            return
        visiting.add(rev)
        parents = revisions.get(rev)
        if parents:
            for parent in parents:
                visit(parent)
        visiting.remove(rev)
        visited.add(rev)

    for rev in revisions:
        visit(rev)

    pointed: set[str] = set()
    for parents in revisions.values():
        if parents:
            pointed.update(parents)

    heads = tuple(sorted(rev for rev in revisions if rev not in pointed))
    if not heads:
        raise AlembicGraphError("revision graph has no heads")
    return AlembicGraph(revisions=revisions, heads=heads)


def discover_alembic_head_revisions(migrations_versions_dir: Path) -> tuple[str, ...]:
    """Discover head revision IDs from migration files using static AST parsing."""
    return build_alembic_graph(migrations_versions_dir).heads
