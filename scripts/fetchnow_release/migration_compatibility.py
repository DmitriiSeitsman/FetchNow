"""Strict migration compatibility contract parsing (PRD1C3B1)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .c3b1_constants import (
    COMPATIBILITY_SCHEMA_VERSION,
    DATA_LOSS_NONE,
    DATABASE_DOWNGRADE_FORBIDDEN,
    EXECUTION_MODE_ONLINE_EXPAND,
    MAX_RATIONALE_LENGTH,
    MIN_RATIONALE_LENGTH,
    PLACEHOLDER_RATIONALE_MARKERS,
    REVISION_ID_RE,
)
from .journal import sha256_file

_REVISION_ID_PATTERN = re.compile(REVISION_ID_RE)


class CompatibilityError(ValueError):
    """Invalid migration compatibility contract."""


@dataclass(frozen=True)
class MigrationTransition:
    from_heads: tuple[str, ...]
    to_heads: tuple[str, ...]
    included_revisions: tuple[str, ...]
    execution_mode: str
    online_with_previous_application: bool
    previous_application_rollback_compatible: bool
    data_loss: str
    database_downgrade: str
    rationale: str


@dataclass(frozen=True)
class CompatibilityContract:
    schema_version: int
    transitions: tuple[MigrationTransition, ...]


def _validate_revision_list(raw: Any, *, field: str) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise CompatibilityError(f"{field} must be a JSON array")
    if not raw:
        raise CompatibilityError(f"{field} must be non-empty")
    seen: set[str] = set()
    items: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise CompatibilityError(f"{field} entries must be strings")
        value = item.strip()
        if not _REVISION_ID_PATTERN.fullmatch(value):
            raise CompatibilityError(f"{field} has invalid revision ID: {value!r}")
        if value in seen:
            raise CompatibilityError(f"{field} contains duplicate ID: {value!r}")
        seen.add(value)
        items.append(value)
    sorted_items = tuple(sorted(items))
    if list(sorted_items) != list(raw):
        raise CompatibilityError(f"{field} must be sorted unique revision IDs")
    return sorted_items


def _validate_rationale(raw: Any) -> str:
    if not isinstance(raw, str):
        raise CompatibilityError("rationale must be a string")
    text = raw.strip()
    if not text:
        raise CompatibilityError("rationale must be non-empty")
    if len(text) < MIN_RATIONALE_LENGTH:
        raise CompatibilityError("rationale is too short")
    if len(text) > MAX_RATIONALE_LENGTH:
        raise CompatibilityError("rationale exceeds maximum length")
    if any(ch in text for ch in ("\x00", "\x01", "\x02", "\x03", "\x04", "\x05", "\x06", "\x07")):
        raise CompatibilityError("rationale contains control characters")
    lowered = text.lower()
    for marker in PLACEHOLDER_RATIONALE_MARKERS:
        if marker in lowered:
            raise CompatibilityError(f"rationale contains placeholder marker: {marker!r}")
    return text


def _load_strict_json(text: str) -> dict[str, Any]:
    def object_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [key for key, _value in pairs]
        if len(keys) != len(set(keys)):
            raise CompatibilityError("duplicate JSON object keys")
        return dict(pairs)

    try:
        raw = json.loads(text, object_pairs_hook=object_pairs_hook)
    except json.JSONDecodeError as exc:
        raise CompatibilityError(f"invalid compatibility JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise CompatibilityError("compatibility contract must be a JSON object")
    return raw


def _require_exact_bool(raw: Any, *, field: str) -> None:
    if raw is not True:
        raise CompatibilityError(f"{field} must be true")


def _parse_transition(raw: Any) -> MigrationTransition:
    if not isinstance(raw, dict):
        raise CompatibilityError("transition must be a JSON object")
    allowed = {
        "from_heads",
        "to_heads",
        "included_revisions",
        "execution_mode",
        "online_with_previous_application",
        "previous_application_rollback_compatible",
        "data_loss",
        "database_downgrade",
        "rationale",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise CompatibilityError(f"transition has unknown fields: {sorted(unknown)}")
    missing = allowed - set(raw)
    if missing:
        raise CompatibilityError(f"transition missing fields: {sorted(missing)}")

    execution_mode = raw["execution_mode"]
    if execution_mode != EXECUTION_MODE_ONLINE_EXPAND:
        raise CompatibilityError(
            f"execution_mode must be {EXECUTION_MODE_ONLINE_EXPAND!r}, got {execution_mode!r}"
        )
    online_with_previous = raw["online_with_previous_application"]
    _require_exact_bool(online_with_previous, field="online_with_previous_application")
    rollback_compatible = raw["previous_application_rollback_compatible"]
    _require_exact_bool(
        rollback_compatible, field="previous_application_rollback_compatible"
    )
    data_loss = raw["data_loss"]
    if data_loss != DATA_LOSS_NONE:
        raise CompatibilityError(f"data_loss must be {DATA_LOSS_NONE!r}, got {data_loss!r}")
    database_downgrade = raw["database_downgrade"]
    if database_downgrade != DATABASE_DOWNGRADE_FORBIDDEN:
        raise CompatibilityError(
            f"database_downgrade must be {DATABASE_DOWNGRADE_FORBIDDEN!r}, "
            f"got {database_downgrade!r}"
        )

    return MigrationTransition(
        from_heads=_validate_revision_list(raw["from_heads"], field="from_heads"),
        to_heads=_validate_revision_list(raw["to_heads"], field="to_heads"),
        included_revisions=_validate_revision_list(
            raw["included_revisions"], field="included_revisions"
        ),
        execution_mode=execution_mode,
        online_with_previous_application=True,
        previous_application_rollback_compatible=True,
        data_loss=DATA_LOSS_NONE,
        database_downgrade=DATABASE_DOWNGRADE_FORBIDDEN,
        rationale=_validate_rationale(raw["rationale"]),
    )


def parse_compatibility_contract(raw: dict[str, Any]) -> CompatibilityContract:
    allowed_top = {"schema_version", "transitions"}
    unknown_top = set(raw) - allowed_top
    if unknown_top:
        raise CompatibilityError(f"contract has unknown fields: {sorted(unknown_top)}")
    missing_top = allowed_top - set(raw)
    if missing_top:
        raise CompatibilityError(f"contract missing fields: {sorted(missing_top)}")

    schema_version = raw["schema_version"]
    if type(schema_version) is not int or isinstance(schema_version, bool):
        raise CompatibilityError("schema_version must be integer 1")
    if schema_version != COMPATIBILITY_SCHEMA_VERSION:
        raise CompatibilityError(
            f"unsupported compatibility schema_version: {schema_version!r}"
        )
    transitions_raw = raw["transitions"]
    if not isinstance(transitions_raw, list):
        raise CompatibilityError("transitions must be a JSON array")
    transitions = tuple(_parse_transition(item) for item in transitions_raw)

    seen_keys: set[tuple[str, ...]] = set()
    for transition in transitions:
        key = (
            transition.from_heads,
            transition.to_heads,
            transition.included_revisions,
        )
        if key in seen_keys:
            raise CompatibilityError("duplicate transition entry")
        seen_keys.add(key)

    return CompatibilityContract(schema_version=schema_version, transitions=transitions)


def load_compatibility_contract(path: Path) -> CompatibilityContract:
    if path.is_symlink():
        raise CompatibilityError("compatibility contract must not be a symlink")
    if path.is_dir():
        raise CompatibilityError("compatibility contract must be a regular file")
    if not path.is_file():
        raise CompatibilityError(f"compatibility contract not found: {path}")
    raw = _load_strict_json(path.read_text(encoding="utf-8"))
    return parse_compatibility_contract(raw)


def compatibility_contract_sha256(path: Path) -> str:
    return sha256_file(path)


def find_matching_transition(
    contract: CompatibilityContract,
    *,
    from_heads: frozenset[str],
    to_heads: frozenset[str],
    included_revisions: frozenset[str],
) -> MigrationTransition:
    """Return exactly one contract entry matching the computed transition."""
    from_sorted = tuple(sorted(from_heads))
    to_sorted = tuple(sorted(to_heads))
    included_sorted = tuple(sorted(included_revisions))

    matches = [
        transition
        for transition in contract.transitions
        if transition.from_heads == from_sorted
        and transition.to_heads == to_sorted
        and transition.included_revisions == included_sorted
    ]
    if not matches:
        raise CompatibilityError(
            "no compatibility transition matches "
            f"from_heads={from_sorted}, to_heads={to_sorted}, "
            f"included_revisions={included_sorted}"
        )
    if len(matches) > 1:
        raise CompatibilityError("duplicate matching compatibility transitions")
    transition = matches[0]
    extra = set(transition.included_revisions) - included_revisions
    missing = included_revisions - set(transition.included_revisions)
    if extra or missing:
        raise CompatibilityError(
            "compatibility included_revisions do not match computed forward delta"
        )
    return transition
