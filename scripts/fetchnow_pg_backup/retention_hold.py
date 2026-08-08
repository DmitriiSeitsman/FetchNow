"""Backup retention holds for migration transactions (PRD1C3B2B2)."""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .checksums import sha256_bytes
from .fs_safety import (
    PathSafetyError,
    assert_direct_child,
    chmod_dir_0700,
    chmod_file_0600,
    fsync_dir,
    fsync_file,
    is_safe_relative_filename,
)

_HOLD_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class RetentionHoldError(ValueError):
    """Invalid retention hold document or lifecycle."""


@dataclass(frozen=True)
class RetentionHoldAcquired:
    schema_version: int
    hold_id: str
    backup_id: str
    backup_manifest_sha256: str
    backup_dump_sha256: str
    migration_id: str
    attestation_schema_version: int
    attestation_path: str
    attestation_sha256: str
    expected_alembic_heads: tuple[str, ...]
    static_alembic_heads: tuple[str, ...]
    verified_restored_heads: tuple[str, ...]
    migrations_graph_sha256: str
    acquired_at_utc: str
    tool_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "hold_id": self.hold_id,
            "backup_id": self.backup_id,
            "backup_manifest_sha256": self.backup_manifest_sha256,
            "backup_dump_sha256": self.backup_dump_sha256,
            "migration_id": self.migration_id,
            "attestation_schema_version": self.attestation_schema_version,
            "attestation_path": self.attestation_path,
            "attestation_sha256": self.attestation_sha256,
            "expected_alembic_heads": list(self.expected_alembic_heads),
            "static_alembic_heads": list(self.static_alembic_heads),
            "verified_restored_heads": list(self.verified_restored_heads),
            "migrations_graph_sha256": self.migrations_graph_sha256,
            "acquired_at_utc": self.acquired_at_utc,
            "tool_version": self.tool_version,
        }


@dataclass(frozen=True)
class RetentionHoldReleased:
    schema_version: int
    hold_id: str
    released_at_utc: str
    reason: str
    tool_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "hold_id": self.hold_id,
            "released_at_utc": self.released_at_utc,
            "reason": self.reason,
            "tool_version": self.tool_version,
        }


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_strict_json(text: str) -> dict[str, Any]:
    def object_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [key for key, _value in pairs]
        if len(keys) != len(set(keys)):
            raise RetentionHoldError("duplicate JSON object keys")
        return dict(pairs)

    try:
        raw = json.loads(text, object_pairs_hook=object_pairs_hook)
    except json.JSONDecodeError as exc:
        raise RetentionHoldError(f"invalid hold JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise RetentionHoldError("hold document must be a JSON object")
    return raw


def _parse_head_list(raw: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise RetentionHoldError(f"{field} must be a non-empty JSON array")
    items = [str(item) for item in raw]
    if tuple(sorted(items)) != tuple(items):
        raise RetentionHoldError(f"{field} must be sorted unique")
    if len(set(items)) != len(items):
        raise RetentionHoldError(f"{field} contains duplicates")
    return tuple(items)


def parse_acquired_document(raw: dict[str, Any]) -> RetentionHoldAcquired:
    allowed = frozenset(RetentionHoldAcquired.__dataclass_fields__)
    unknown = set(raw) - allowed
    if unknown:
        raise RetentionHoldError(f"acquired hold has unknown fields: {sorted(unknown)}")
    missing = allowed - set(raw)
    if missing:
        raise RetentionHoldError(f"acquired hold missing fields: {sorted(missing)}")
    if type(raw["schema_version"]) is not int or isinstance(raw["schema_version"], bool):
        raise RetentionHoldError("schema_version must be an exact integer")
    hold_id = str(raw["hold_id"])
    if not _HOLD_ID_RE.fullmatch(hold_id):
        raise RetentionHoldError("hold_id must be a UUID")
    for field in ("backup_manifest_sha256", "backup_dump_sha256", "attestation_sha256", "migrations_graph_sha256"):
        value = raw[field]
        if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
            raise RetentionHoldError(f"{field} must be lowercase hex SHA-256")
    att_path = str(raw["attestation_path"])
    if not is_safe_relative_filename(Path(att_path).name):
        raise RetentionHoldError("attestation_path must be a safe relative filename leaf")
    return RetentionHoldAcquired(
        schema_version=raw["schema_version"],
        hold_id=hold_id,
        backup_id=str(raw["backup_id"]),
        backup_manifest_sha256=raw["backup_manifest_sha256"],
        backup_dump_sha256=raw["backup_dump_sha256"],
        migration_id=str(raw["migration_id"]),
        attestation_schema_version=raw["attestation_schema_version"],
        attestation_path=att_path,
        attestation_sha256=raw["attestation_sha256"],
        expected_alembic_heads=_parse_head_list(
            raw["expected_alembic_heads"], field="expected_alembic_heads"
        ),
        static_alembic_heads=_parse_head_list(
            raw["static_alembic_heads"], field="static_alembic_heads"
        ),
        verified_restored_heads=_parse_head_list(
            raw["verified_restored_heads"], field="verified_restored_heads"
        ),
        migrations_graph_sha256=raw["migrations_graph_sha256"],
        acquired_at_utc=str(raw["acquired_at_utc"]),
        tool_version=str(raw["tool_version"]),
    )


def parse_released_document(raw: dict[str, Any]) -> RetentionHoldReleased:
    allowed = frozenset(RetentionHoldReleased.__dataclass_fields__)
    unknown = set(raw) - allowed
    if unknown:
        raise RetentionHoldError(f"released hold has unknown fields: {sorted(unknown)}")
    if type(raw["schema_version"]) is not int or isinstance(raw["schema_version"], bool):
        raise RetentionHoldError("schema_version must be an exact integer")
    hold_id = str(raw["hold_id"])
    if not _HOLD_ID_RE.fullmatch(hold_id):
        raise RetentionHoldError("hold_id must be a UUID")
    return RetentionHoldReleased(
        schema_version=raw["schema_version"],
        hold_id=hold_id,
        released_at_utc=str(raw["released_at_utc"]),
        reason=str(raw["reason"]),
        tool_version=str(raw["tool_version"]),
    )


def holds_root(backup_root: Path) -> Path:
    return backup_root / "holds"


def hold_dir(backup_root: Path, hold_id: str) -> Path:
    if not _HOLD_ID_RE.fullmatch(hold_id):
        raise RetentionHoldError(f"malformed hold id: {hold_id!r}")
    root = holds_root(backup_root)
    path = root / hold_id
    try:
        assert_direct_child(path, root)
    except PathSafetyError as exc:
        raise RetentionHoldError(str(exc)) from exc
    return path


def new_hold_id() -> str:
    return str(uuid.uuid4())


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise RetentionHoldError(f"refusing to overwrite hold file: {path}")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    chmod_dir_0700(path.parent)
    tmp = path.with_name(f".incomplete-{path.name}")
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    tmp.write_text(text, encoding="utf-8")
    chmod_file_0600(tmp)
    fsync_file(tmp)
    os.replace(tmp, path)
    chmod_file_0600(path)
    fsync_file(path)
    fsync_dir(path.parent)


def publish_acquired_hold(
    *,
    backup_root: Path,
    acquired: RetentionHoldAcquired,
) -> Path:
    directory = hold_dir(backup_root, acquired.hold_id)
    if directory.exists():
        raise RetentionHoldError(f"hold directory already exists: {acquired.hold_id}")
    holds_root(backup_root).mkdir(mode=0o700, parents=True, exist_ok=True)
    chmod_dir_0700(holds_root(backup_root))
    directory.mkdir(mode=0o700)
    chmod_dir_0700(directory)
    path = directory / "acquired.json"
    _atomic_write_json(path, acquired.to_dict())
    return path


def publish_released_hold(
    *,
    backup_root: Path,
    released: RetentionHoldReleased,
) -> Path:
    directory = hold_dir(backup_root, released.hold_id)
    acquired_path = directory / "acquired.json"
    if not acquired_path.is_file():
        raise RetentionHoldError(f"missing acquired hold: {released.hold_id}")
    released_path = directory / "released.json"
    if released_path.exists():
        raise RetentionHoldError(f"hold already released: {released.hold_id}")
    _atomic_write_json(released_path, released.to_dict())
    return released_path


def load_acquired_hold(backup_root: Path, hold_id: str) -> RetentionHoldAcquired:
    path = hold_dir(backup_root, hold_id) / "acquired.json"
    if path.is_symlink() or not path.is_file():
        raise RetentionHoldError(f"missing acquired hold file for {hold_id}")
    return parse_acquired_document(_load_strict_json(path.read_text(encoding="utf-8")))


def hold_is_active(backup_root: Path, hold_id: str) -> bool:
    directory = hold_dir(backup_root, hold_id)
    acquired = directory / "acquired.json"
    released = directory / "released.json"
    if not acquired.is_file() or acquired.is_symlink():
        return False
    if released.exists():
        return False
    return True


def active_hold_for_backup(backup_root: Path, backup_id: str) -> RetentionHoldAcquired | None:
    root = holds_root(backup_root)
    if not root.is_dir():
        return None
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.is_symlink():
            continue
        if not _HOLD_ID_RE.fullmatch(child.name):
            continue
        if (child / "released.json").exists():
            continue
        acquired_path = child / "acquired.json"
        if not acquired_path.is_file():
            continue
        try:
            acquired = parse_acquired_document(
                _load_strict_json(acquired_path.read_text(encoding="utf-8"))
            )
        except RetentionHoldError:
            raise
        if acquired.backup_id == backup_id:
            return acquired
    return None


def active_hold_for_migration(
    backup_root: Path, migration_id: str
) -> RetentionHoldAcquired | None:
    root = holds_root(backup_root)
    if not root.is_dir():
        return None
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.is_symlink():
            continue
        if not _HOLD_ID_RE.fullmatch(child.name):
            continue
        if (child / "released.json").exists():
            continue
        acquired_path = child / "acquired.json"
        if not acquired_path.is_file() or acquired_path.is_symlink():
            continue
        acquired = parse_acquired_document(
            _load_strict_json(acquired_path.read_text(encoding="utf-8"))
        )
        if acquired.migration_id == migration_id:
            return acquired
    return None


def hold_document_sha256(path: Path) -> str:
    return sha256_bytes(path.read_bytes())
