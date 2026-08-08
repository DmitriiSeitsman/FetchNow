"""Migration transaction journal (PRD1C3B2B2)."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .c3b2b2_constants import (
    MIGRATION_EVENTS_DIRNAME,
    MIGRATION_PLAN_NAME,
    MIGRATION_RECOVERIES_DIRNAME,
    MIGRATION_RESOLUTION_NAME,
    MIGRATION_RESOLUTION_SCHEMA_VERSION,
    MIGRATION_RESULT_NAME,
    MIGRATION_EVENT_SCHEMA_VERSION,
    MIGRATIONS_DIRNAME,
    MIGRATION_LEGAL_TRANSITIONS,
    MIGRATION_TERMINAL_STATUSES,
    STATUS_MIGRATION_COMMITTED,
    STATUS_MIGRATION_FAILED,
    STATUS_MIGRATION_PLANNED,
    STATUS_MIGRATION_REQUIRES_RECOVERY,
)
from .deploy_root import DeployRootError, ensure_deploy_layout
from .journal_io import atomic_write_json, read_json

_MIGRATION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REV_RE = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class MigrationJournalError(ValueError):
    """Invalid migration journal document or transition."""


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_migration_id() -> str:
    return str(uuid.uuid4())


def new_recovery_id() -> str:
    return str(uuid.uuid4())


def migrations_dir(deploy_root: Path) -> Path:
    return deploy_root / MIGRATIONS_DIRNAME


def migration_dir(deploy_root: Path, migration_id: str) -> Path:
    if not _MIGRATION_ID_RE.fullmatch(migration_id):
        raise MigrationJournalError(f"malformed migration id: {migration_id!r}")
    path = migrations_dir(deploy_root) / migration_id
    if path.resolve().parent != migrations_dir(deploy_root).resolve():
        raise MigrationJournalError("migration path escape rejected")
    return path


def ensure_migration_layout(deploy_root: Path) -> None:
    ensure_deploy_layout(deploy_root)
    root = migrations_dir(deploy_root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink():
        raise DeployRootError(f"refusing symlink migrations dir: {root}")
    import os

    os.chmod(root, 0o700)


def assert_legal_migration_transition(current: str, nxt: str) -> None:
    allowed = MIGRATION_LEGAL_TRANSITIONS.get(current)
    if allowed is None:
        raise MigrationJournalError(f"unknown migration status: {current!r}")
    if nxt not in allowed:
        raise MigrationJournalError(
            f"illegal migration transition {current!r} -> {nxt!r}"
        )


def _parse_exact_int(value: object, *, field: str) -> int:
    if type(value) is not int or isinstance(value, bool):
        raise MigrationJournalError(f"{field} must be an exact integer")
    return value


def _parse_sha(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _REV_RE.fullmatch(value):
        raise MigrationJournalError(f"{field} must be a 40-char git SHA")
    return value


def _parse_sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise MigrationJournalError(f"{field} must be a 64-char hex SHA-256")
    return value


def _parse_head_list(raw: object, *, field: str) -> list[str]:
    if not isinstance(raw, list) or not raw:
        raise MigrationJournalError(f"{field} must be a non-empty JSON array")
    items = [str(item) for item in raw]
    if tuple(sorted(items)) != tuple(items):
        raise MigrationJournalError(f"{field} must be sorted unique")
    if len(set(items)) != len(items):
        raise MigrationJournalError(f"{field} contains duplicates")
    return items


@dataclass(frozen=True)
class MigrationPlanDocument:
    schema_version: int
    migration_id: str
    target_revision: str
    current_state_sha256: str
    source_application_revision: str
    source_application_image_ids: dict[str, str]
    source_database_release_revision: str
    source_database_heads: list[str]
    source_migrations_graph_sha256: str
    target_database_release_revision: str
    target_database_heads: list[str]
    target_migrations_graph_sha256: str
    included_revisions: list[str]
    compatibility_contract_sha256: str
    target_api_image_id: str
    backup_root_identity_sha256: str
    expected_post_migration_state_sha256: str
    project_name: str
    compose_files: list[str]
    created_at_utc: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "migration_id": self.migration_id,
            "target_revision": self.target_revision,
            "current_state_sha256": self.current_state_sha256,
            "source_application_revision": self.source_application_revision,
            "source_application_image_ids": dict(self.source_application_image_ids),
            "source_database_release_revision": self.source_database_release_revision,
            "source_database_heads": list(self.source_database_heads),
            "source_migrations_graph_sha256": self.source_migrations_graph_sha256,
            "target_database_release_revision": self.target_database_release_revision,
            "target_database_heads": list(self.target_database_heads),
            "target_migrations_graph_sha256": self.target_migrations_graph_sha256,
            "included_revisions": list(self.included_revisions),
            "compatibility_contract_sha256": self.compatibility_contract_sha256,
            "target_api_image_id": self.target_api_image_id,
            "backup_root_identity_sha256": self.backup_root_identity_sha256,
            "expected_post_migration_state_sha256": self.expected_post_migration_state_sha256,
            "project_name": self.project_name,
            "compose_files": list(self.compose_files),
            "created_at_utc": self.created_at_utc,
        }


def parse_migration_plan(raw: dict[str, Any]) -> MigrationPlanDocument:
    allowed = frozenset(MigrationPlanDocument.__dataclass_fields__)
    unknown = set(raw) - allowed
    if unknown:
        raise MigrationJournalError(f"plan has unknown fields: {sorted(unknown)}")
    missing = allowed - set(raw)
    if missing:
        raise MigrationJournalError(f"plan missing fields: {sorted(missing)}")
    ids = raw["source_application_image_ids"]
    if not isinstance(ids, dict) or set(ids) != {"api", "worker", "web", "gateway"}:
        raise MigrationJournalError("source_application_image_ids malformed")
    for key, value in ids.items():
        if not isinstance(value, str) or not _IMAGE_ID_RE.fullmatch(value):
            raise MigrationJournalError(f"invalid image id for {key}")
    return MigrationPlanDocument(
        schema_version=_parse_exact_int(raw["schema_version"], field="schema_version"),
        migration_id=str(raw["migration_id"]),
        target_revision=_parse_sha(raw["target_revision"], field="target_revision"),
        current_state_sha256=_parse_sha256(
            raw["current_state_sha256"], field="current_state_sha256"
        ),
        source_application_revision=_parse_sha(
            raw["source_application_revision"], field="source_application_revision"
        ),
        source_application_image_ids=dict(ids),
        source_database_release_revision=_parse_sha(
            raw["source_database_release_revision"],
            field="source_database_release_revision",
        ),
        source_database_heads=_parse_head_list(
            raw["source_database_heads"], field="source_database_heads"
        ),
        source_migrations_graph_sha256=_parse_sha256(
            raw["source_migrations_graph_sha256"],
            field="source_migrations_graph_sha256",
        ),
        target_database_release_revision=_parse_sha(
            raw["target_database_release_revision"],
            field="target_database_release_revision",
        ),
        target_database_heads=_parse_head_list(
            raw["target_database_heads"], field="target_database_heads"
        ),
        target_migrations_graph_sha256=_parse_sha256(
            raw["target_migrations_graph_sha256"],
            field="target_migrations_graph_sha256",
        ),
        included_revisions=_parse_head_list(
            raw["included_revisions"], field="included_revisions"
        ),
        compatibility_contract_sha256=_parse_sha256(
            raw["compatibility_contract_sha256"],
            field="compatibility_contract_sha256",
        ),
        target_api_image_id=str(raw["target_api_image_id"]),
        backup_root_identity_sha256=_parse_sha256(
            raw["backup_root_identity_sha256"], field="backup_root_identity_sha256"
        ),
        expected_post_migration_state_sha256=_parse_sha256(
            raw["expected_post_migration_state_sha256"],
            field="expected_post_migration_state_sha256",
        ),
        project_name=str(raw["project_name"]),
        compose_files=[str(item) for item in raw["compose_files"]],
        created_at_utc=str(raw["created_at_utc"]),
    )


def write_migration_plan(path: Path, plan: MigrationPlanDocument) -> None:
    if path.exists():
        raise MigrationJournalError(f"refusing to overwrite plan: {path}")
    parse_migration_plan(plan.to_dict())
    atomic_write_json(path, plan.to_dict(), mode=0o600)


def load_migration_plan(mig_dir: Path) -> MigrationPlanDocument:
    return parse_migration_plan(read_json(mig_dir / MIGRATION_PLAN_NAME))


def append_migration_event(mig_dir: Path, status: str, detail: str) -> Path:
    events = mig_dir / MIGRATION_EVENTS_DIRNAME
    events.mkdir(mode=0o700, exist_ok=True)
    existing = sorted(events.glob("*.json"))
    seq = len(existing) + 1
    if existing:
        last = read_json(existing[-1])
        assert_legal_migration_transition(str(last["status"]), status)
    else:
        if status != STATUS_MIGRATION_PLANNED:
            raise MigrationJournalError("first migration event must be planned")
    name = f"{seq:04d}_{status}.json"
    path = events / name
    if path.exists():
        raise MigrationJournalError(f"event collision: {path}")
    payload = {
        "schema_version": MIGRATION_EVENT_SCHEMA_VERSION,
        "sequence": seq,
        "status": status,
        "detail": detail,
        "recorded_at_utc": utc_now(),
    }
    atomic_write_json(path, payload, mode=0o600)
    return path


def append_migration_evidence_event(
    mig_dir: Path, *, status: str, detail: str, evidence: dict[str, Any]
) -> Path:
    events = mig_dir / MIGRATION_EVENTS_DIRNAME
    events.mkdir(mode=0o700, exist_ok=True)
    existing = sorted(events.glob("*.json"))
    seq = len(existing) + 1
    if existing:
        last = read_json(existing[-1])
        assert_legal_migration_transition(str(last["status"]), status)
    name = f"{seq:04d}_{status}.json"
    path = events / name
    if path.exists():
        raise MigrationJournalError(f"event collision: {path}")
    payload = {
        "schema_version": MIGRATION_EVENT_SCHEMA_VERSION,
        "sequence": seq,
        "status": status,
        "detail": detail,
        "evidence": evidence,
        "recorded_at_utc": utc_now(),
    }
    atomic_write_json(path, payload, mode=0o600)
    return path


@dataclass(frozen=True)
class MigrationResultDocument:
    schema_version: int
    migration_id: str
    status: str
    failure_phase: str | None
    result_sha256: str | None
    recorded_at_utc: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "migration_id": self.migration_id,
            "status": self.status,
            "failure_phase": self.failure_phase,
            "result_sha256": self.result_sha256,
            "recorded_at_utc": self.recorded_at_utc,
        }


def write_migration_result(mig_dir: Path, result: MigrationResultDocument) -> None:
    path = mig_dir / MIGRATION_RESULT_NAME
    if path.exists():
        raise MigrationJournalError("refusing to overwrite migration result")
    if result.status not in MIGRATION_TERMINAL_STATUSES:
        raise MigrationJournalError(f"invalid terminal status: {result.status}")
    atomic_write_json(path, result.to_dict(), mode=0o600)


def load_migration_result(mig_dir: Path) -> MigrationResultDocument | None:
    path = mig_dir / MIGRATION_RESULT_NAME
    if not path.is_file():
        return None
    raw = read_json(path)
    return MigrationResultDocument(
        schema_version=_parse_exact_int(raw["schema_version"], field="schema_version"),
        migration_id=str(raw["migration_id"]),
        status=str(raw["status"]),
        failure_phase=raw.get("failure_phase"),
        result_sha256=raw.get("result_sha256"),
        recorded_at_utc=str(raw["recorded_at_utc"]),
    )


def latest_migration_status(mig_dir: Path) -> str | None:
    events = mig_dir / MIGRATION_EVENTS_DIRNAME
    if not events.is_dir():
        return None
    files = sorted(events.glob("*.json"))
    if not files:
        return None
    return str(read_json(files[-1])["status"])


def migration_blocks_mutation(mig_dir: Path) -> bool:
    result = load_migration_result(mig_dir)
    if result is None:
        return True
    if result.status == STATUS_MIGRATION_COMMITTED:
        return False
    if result.status == STATUS_MIGRATION_FAILED:
        return False
    if result.status == STATUS_MIGRATION_REQUIRES_RECOVERY:
        resolution = mig_dir / MIGRATION_RESOLUTION_NAME
        return not resolution.is_file()
    return True


def migration_recovery_dir(mig_dir: Path, recovery_id: str) -> Path:
    if not _MIGRATION_ID_RE.fullmatch(recovery_id):
        raise MigrationJournalError(f"malformed recovery id: {recovery_id!r}")
    root = mig_dir / MIGRATION_RECOVERIES_DIRNAME
    path = root / recovery_id
    if path.resolve().parent != root.resolve():
        raise MigrationJournalError("recovery path escape rejected")
    return path


@dataclass(frozen=True)
class MigrationRecoveryPlanDocument:
    schema_version: int
    recovery_id: str
    migration_id: str
    action: str
    created_at_utc: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "recovery_id": self.recovery_id,
            "migration_id": self.migration_id,
            "action": self.action,
            "created_at_utc": self.created_at_utc,
        }


@dataclass(frozen=True)
class MigrationRecoveryResultDocument:
    schema_version: int
    recovery_id: str
    migration_id: str
    outcome: str
    recorded_at_utc: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "recovery_id": self.recovery_id,
            "migration_id": self.migration_id,
            "outcome": self.outcome,
            "recorded_at_utc": self.recorded_at_utc,
        }


def write_migration_recovery_plan(
    rec_dir: Path, plan: MigrationRecoveryPlanDocument
) -> None:
    rec_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = rec_dir / MIGRATION_PLAN_NAME
    if path.exists():
        raise MigrationJournalError(f"refusing to overwrite recovery plan: {path}")
    atomic_write_json(path, plan.to_dict(), mode=0o600)


def append_migration_recovery_event(rec_dir: Path, status: str, detail: str) -> Path:
    events = rec_dir / MIGRATION_EVENTS_DIRNAME
    events.mkdir(mode=0o700, exist_ok=True)
    existing = sorted(events.glob("*.json"))
    seq = len(existing) + 1
    name = f"{seq:04d}_{status}.json"
    path = events / name
    if path.exists():
        raise MigrationJournalError(f"recovery event collision: {path}")
    payload = {
        "schema_version": MIGRATION_EVENT_SCHEMA_VERSION,
        "sequence": seq,
        "status": status,
        "detail": detail,
        "recorded_at_utc": utc_now(),
    }
    atomic_write_json(path, payload, mode=0o600)
    return path


def write_migration_recovery_result(
    rec_dir: Path, result: MigrationRecoveryResultDocument
) -> None:
    path = rec_dir / MIGRATION_RESULT_NAME
    if path.exists():
        raise MigrationJournalError("refusing to overwrite recovery result")
    atomic_write_json(path, result.to_dict(), mode=0o600)


def write_migration_resolution(
    mig_dir: Path,
    *,
    migration_id: str,
    action: str,
    outcome: str,
    recovery_id: str,
) -> None:
    path = mig_dir / MIGRATION_RESOLUTION_NAME
    if path.exists():
        raise MigrationJournalError("refusing to overwrite migration resolution")
    atomic_write_json(
        path,
        {
            "schema_version": MIGRATION_RESOLUTION_SCHEMA_VERSION,
            "migration_id": migration_id,
            "action": action,
            "outcome": outcome,
            "recovery_id": recovery_id,
            "recorded_at_utc": utc_now(),
        },
        mode=0o600,
    )


def load_migration_resolution(mig_dir: Path) -> dict[str, Any] | None:
    path = mig_dir / MIGRATION_RESOLUTION_NAME
    if not path.is_file():
        return None
    return read_json(path)


def find_unresolved_migrations(deploy_root: Path) -> list[str]:
    root = migrations_dir(deploy_root)
    if not root.is_dir():
        return []
    unresolved: list[str] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.is_symlink():
            continue
        if not _MIGRATION_ID_RE.fullmatch(child.name):
            continue
        if migration_blocks_mutation(child):
            unresolved.append(child.name)
    return unresolved
