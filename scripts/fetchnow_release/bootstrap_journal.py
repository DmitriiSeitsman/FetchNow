"""Initial database bootstrap journal (plan / events / result)."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .c2_constants import SUPPORTED_SOURCE_CONTRACT_VERSIONS
from .c3b3_constants import (
    BOOTSTRAP_EVENT_SCHEMA_VERSION,
    BOOTSTRAP_EVENTS_DIRNAME,
    BOOTSTRAP_LEGAL_TRANSITIONS,
    BOOTSTRAP_OVERRIDES_DIRNAME,
    BOOTSTRAP_PLAN_NAME,
    BOOTSTRAP_PLAN_SCHEMA_VERSION,
    BOOTSTRAP_RESULT_NAME,
    BOOTSTRAP_RESULT_SCHEMA_VERSION,
    BOOTSTRAP_TERMINAL_STATUSES,
    BOOTSTRAPS_DIRNAME,
    STATUS_BOOTSTRAP_COMMITTED,
    STATUS_BOOTSTRAP_PLANNED,
    STATUS_BOOTSTRAP_REQUIRES_RECOVERY,
)
from .deploy_root import DeployRootError, ensure_deploy_layout
from .journal_io import atomic_write_json, read_json

_BOOTSTRAP_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REV_RE = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class BootstrapJournalError(ValueError):
    """Invalid bootstrap journal document or transition."""


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_bootstrap_id() -> str:
    return str(uuid.uuid4())


def bootstraps_dir(deploy_root: Path) -> Path:
    return deploy_root / BOOTSTRAPS_DIRNAME


def bootstrap_dir(deploy_root: Path, bootstrap_id: str) -> Path:
    if not _BOOTSTRAP_ID_RE.fullmatch(bootstrap_id):
        raise BootstrapJournalError(f"malformed bootstrap id: {bootstrap_id!r}")
    path = bootstraps_dir(deploy_root) / bootstrap_id
    if path.resolve().parent != bootstraps_dir(deploy_root).resolve():
        raise BootstrapJournalError("bootstrap path escape rejected")
    return path


def ensure_bootstrap_layout(deploy_root: Path) -> None:
    ensure_deploy_layout(deploy_root)
    root = bootstraps_dir(deploy_root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink():
        raise DeployRootError(f"refusing symlink bootstraps dir: {root}")
    import os

    os.chmod(root, 0o700)


def assert_legal_bootstrap_transition(current: str, nxt: str) -> None:
    allowed = BOOTSTRAP_LEGAL_TRANSITIONS.get(current)
    if allowed is None:
        raise BootstrapJournalError(f"unknown bootstrap status: {current!r}")
    if nxt not in allowed:
        raise BootstrapJournalError(
            f"illegal bootstrap transition {current!r} -> {nxt!r}"
        )


def _parse_exact_int(value: object, *, field: str) -> int:
    if type(value) is not int or isinstance(value, bool):
        raise BootstrapJournalError(f"{field} must be an exact integer")
    return value


def _parse_sha(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _REV_RE.fullmatch(value):
        raise BootstrapJournalError(f"{field} must be a 40-char git SHA")
    return value


def _parse_sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise BootstrapJournalError(f"{field} must be a 64-char hex SHA-256")
    return value


def _parse_head_list(raw: object, *, field: str) -> list[str]:
    if not isinstance(raw, list):
        raise BootstrapJournalError(f"{field} must be a JSON array")
    items = [str(item) for item in raw]
    if tuple(sorted(items)) != tuple(items):
        raise BootstrapJournalError(f"{field} must be sorted unique")
    if len(set(items)) != len(items):
        raise BootstrapJournalError(f"{field} contains duplicates")
    return items


def _parse_bool(value: object, *, field: str) -> bool:
    if type(value) is not bool:
        raise BootstrapJournalError(f"{field} must be a JSON boolean")
    return value


@dataclass(frozen=True)
class BootstrapIdentity:
    """Exact identity a committed bootstrap journal must bind to."""

    project_name: str
    target_revision: str
    target_db_heads: tuple[str, ...]
    release_manifest_sha256: str
    target_api_image_id: str
    compose_overlay: str
    source_contract_version: int


@dataclass(frozen=True)
class BootstrapPlanDocument:
    schema_version: int
    bootstrap_id: str
    project_name: str
    target_revision: str
    target_db_heads: list[str]
    release_manifest_sha256: str
    target_api_image_id: str
    compose_overlay: str
    source_contract_version: int
    pgdata_volume_existed_before: bool
    created_at_utc: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "bootstrap_id": self.bootstrap_id,
            "project_name": self.project_name,
            "target_revision": self.target_revision,
            "target_db_heads": list(self.target_db_heads),
            "release_manifest_sha256": self.release_manifest_sha256,
            "target_api_image_id": self.target_api_image_id,
            "compose_overlay": self.compose_overlay,
            "source_contract_version": self.source_contract_version,
            "pgdata_volume_existed_before": self.pgdata_volume_existed_before,
            "created_at_utc": self.created_at_utc,
        }


def parse_bootstrap_plan(raw: dict[str, Any]) -> BootstrapPlanDocument:
    allowed = frozenset(BootstrapPlanDocument.__dataclass_fields__)
    unknown = set(raw) - allowed
    if unknown:
        raise BootstrapJournalError(f"plan has unknown fields: {sorted(unknown)}")
    missing = allowed - set(raw)
    if missing:
        raise BootstrapJournalError(f"plan missing fields: {sorted(missing)}")
    bootstrap_id = str(raw["bootstrap_id"])
    if not _BOOTSTRAP_ID_RE.fullmatch(bootstrap_id):
        raise BootstrapJournalError("malformed bootstrap_id")
    api_id = str(raw["target_api_image_id"])
    if not _IMAGE_ID_RE.fullmatch(api_id):
        raise BootstrapJournalError("malformed target_api_image_id")
    overlay = str(raw["compose_overlay"])
    if not overlay:
        raise BootstrapJournalError("compose_overlay must be non-empty")
    heads = _parse_head_list(raw["target_db_heads"], field="target_db_heads")
    if not heads:
        raise BootstrapJournalError("target_db_heads must be non-empty")
    schema = _parse_exact_int(raw["schema_version"], field="schema_version")
    if schema != BOOTSTRAP_PLAN_SCHEMA_VERSION:
        raise BootstrapJournalError(f"unsupported plan schema: {schema}")
    contract = _parse_exact_int(
        raw["source_contract_version"], field="source_contract_version"
    )
    if contract not in SUPPORTED_SOURCE_CONTRACT_VERSIONS:
        raise BootstrapJournalError(
            f"unsupported source_contract_version: {contract}"
        )
    return BootstrapPlanDocument(
        schema_version=schema,
        bootstrap_id=bootstrap_id,
        project_name=str(raw["project_name"]),
        target_revision=_parse_sha(raw["target_revision"], field="target_revision"),
        target_db_heads=heads,
        release_manifest_sha256=_parse_sha256(
            raw["release_manifest_sha256"], field="release_manifest_sha256"
        ),
        target_api_image_id=api_id,
        compose_overlay=overlay,
        source_contract_version=contract,
        pgdata_volume_existed_before=_parse_bool(
            raw["pgdata_volume_existed_before"],
            field="pgdata_volume_existed_before",
        ),
        created_at_utc=str(raw["created_at_utc"]),
    )


def write_bootstrap_plan(path: Path, plan: BootstrapPlanDocument) -> None:
    if path.exists():
        raise BootstrapJournalError(f"refusing to overwrite plan: {path}")
    parse_bootstrap_plan(plan.to_dict())
    atomic_write_json(path, plan.to_dict(), mode=0o600)


def load_bootstrap_plan(boot_dir: Path) -> BootstrapPlanDocument:
    return parse_bootstrap_plan(read_json(boot_dir / BOOTSTRAP_PLAN_NAME))


def append_bootstrap_event(boot_dir: Path, status: str, detail: str) -> Path:
    events = boot_dir / BOOTSTRAP_EVENTS_DIRNAME
    events.mkdir(mode=0o700, exist_ok=True)
    existing = sorted(events.glob("*.json"))
    seq = len(existing) + 1
    if existing:
        last = read_json(existing[-1])
        assert_legal_bootstrap_transition(str(last["status"]), status)
    else:
        if status != STATUS_BOOTSTRAP_PLANNED:
            raise BootstrapJournalError("first bootstrap event must be planned")
    name = f"{seq:04d}_{status}.json"
    path = events / name
    if path.exists():
        raise BootstrapJournalError(f"event collision: {path}")
    payload = {
        "schema_version": BOOTSTRAP_EVENT_SCHEMA_VERSION,
        "sequence": seq,
        "status": status,
        "detail": detail,
        "recorded_at_utc": utc_now(),
    }
    atomic_write_json(path, payload, mode=0o600)
    return path


def latest_bootstrap_status(boot_dir: Path) -> str | None:
    events = boot_dir / BOOTSTRAP_EVENTS_DIRNAME
    if not events.is_dir():
        return None
    existing = sorted(p for p in events.glob("*.json") if p.is_file())
    if not existing:
        return None
    return str(read_json(existing[-1]).get("status") or "")


@dataclass(frozen=True)
class BootstrapResultDocument:
    schema_version: int
    bootstrap_id: str
    status: str
    already_initialized: bool
    target_revision: str
    verified_db_heads: list[str]
    current_json_written: bool
    messages: list[str]
    finished_at_utc: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "bootstrap_id": self.bootstrap_id,
            "status": self.status,
            "already_initialized": self.already_initialized,
            "target_revision": self.target_revision,
            "verified_db_heads": list(self.verified_db_heads),
            "current_json_written": self.current_json_written,
            "messages": list(self.messages),
            "finished_at_utc": self.finished_at_utc,
        }


def parse_bootstrap_result(raw: dict[str, Any]) -> BootstrapResultDocument:
    allowed = frozenset(BootstrapResultDocument.__dataclass_fields__)
    unknown = set(raw) - allowed
    if unknown:
        raise BootstrapJournalError(f"result has unknown fields: {sorted(unknown)}")
    missing = allowed - set(raw)
    if missing:
        raise BootstrapJournalError(f"result missing fields: {sorted(missing)}")
    status = str(raw["status"])
    if status not in BOOTSTRAP_TERMINAL_STATUSES | {STATUS_BOOTSTRAP_REQUIRES_RECOVERY}:
        raise BootstrapJournalError(f"invalid result status: {status}")
    written = _parse_bool(raw["current_json_written"], field="current_json_written")
    if written:
        raise BootstrapJournalError("bootstrap result must not write current.json")
    messages = raw["messages"]
    if not isinstance(messages, list) or any(not isinstance(m, str) for m in messages):
        raise BootstrapJournalError("messages must be a JSON array of strings")
    schema = _parse_exact_int(raw["schema_version"], field="schema_version")
    if schema != BOOTSTRAP_RESULT_SCHEMA_VERSION:
        raise BootstrapJournalError(f"unsupported result schema: {schema}")
    bootstrap_id = str(raw["bootstrap_id"])
    if not _BOOTSTRAP_ID_RE.fullmatch(bootstrap_id):
        raise BootstrapJournalError("malformed bootstrap_id")
    return BootstrapResultDocument(
        schema_version=schema,
        bootstrap_id=bootstrap_id,
        status=status,
        already_initialized=_parse_bool(
            raw["already_initialized"], field="already_initialized"
        ),
        target_revision=_parse_sha(raw["target_revision"], field="target_revision"),
        verified_db_heads=_parse_head_list(
            raw["verified_db_heads"], field="verified_db_heads"
        ),
        current_json_written=written,
        messages=[str(item) for item in messages],
        finished_at_utc=str(raw["finished_at_utc"]),
    )


def write_bootstrap_result(boot_dir: Path, result: BootstrapResultDocument) -> None:
    path = boot_dir / BOOTSTRAP_RESULT_NAME
    if path.exists():
        raise BootstrapJournalError("result.json already published")
    parse_bootstrap_result(result.to_dict())
    atomic_write_json(path, result.to_dict(), mode=0o600)


def load_bootstrap_result(boot_dir: Path) -> BootstrapResultDocument | None:
    path = boot_dir / BOOTSTRAP_RESULT_NAME
    if not path.is_file():
        return None
    return parse_bootstrap_result(read_json(path))


def bootstrap_blocks_mutation(boot_dir: Path) -> bool:
    result = load_bootstrap_result(boot_dir)
    if result is None:
        return True
    if result.status in BOOTSTRAP_TERMINAL_STATUSES:
        return False
    return True


def find_unresolved_bootstraps(deploy_root: Path) -> list[str]:
    root = bootstraps_dir(deploy_root)
    if not root.is_dir():
        return []
    unresolved: list[str] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.is_symlink():
            continue
        if not _BOOTSTRAP_ID_RE.fullmatch(child.name):
            continue
        if bootstrap_blocks_mutation(child):
            unresolved.append(child.name)
    return unresolved


def bootstrap_plan_matches_identity(
    plan: BootstrapPlanDocument, identity: BootstrapIdentity
) -> bool:
    return (
        plan.project_name == identity.project_name
        and plan.target_revision == identity.target_revision
        and tuple(plan.target_db_heads) == identity.target_db_heads
        and plan.release_manifest_sha256 == identity.release_manifest_sha256
        and plan.target_api_image_id == identity.target_api_image_id
        and plan.compose_overlay == identity.compose_overlay
        and plan.source_contract_version == identity.source_contract_version
    )


def committed_bootstrap_matches(
    plan: BootstrapPlanDocument,
    result: BootstrapResultDocument,
    identity: BootstrapIdentity,
) -> bool:
    """True only when plan + successful result bind the exact identity."""
    if result.status != STATUS_BOOTSTRAP_COMMITTED:
        return False
    if result.current_json_written:
        return False
    if result.target_revision != identity.target_revision:
        return False
    if tuple(result.verified_db_heads) != identity.target_db_heads:
        return False
    return bootstrap_plan_matches_identity(plan, identity)


def find_matching_committed_bootstrap(
    deploy_root: Path, identity: BootstrapIdentity
) -> BootstrapPlanDocument | None:
    """Return the committed journal bound to ``identity``, or None.

    Revision text alone is not sufficient. A journal for the same
    project+revision with any other identity field mismatch is refused.
    """
    root = bootstraps_dir(deploy_root)
    if not root.is_dir():
        return None
    matches: list[BootstrapPlanDocument] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.is_symlink():
            continue
        if not _BOOTSTRAP_ID_RE.fullmatch(child.name):
            continue
        result = load_bootstrap_result(child)
        if result is None or result.status != STATUS_BOOTSTRAP_COMMITTED:
            continue
        plan = load_bootstrap_plan(child)
        same_project_revision = (
            plan.project_name == identity.project_name
            and plan.target_revision == identity.target_revision
        )
        if not same_project_revision:
            continue
        if not committed_bootstrap_matches(plan, result, identity):
            raise BootstrapJournalError(
                "committed bootstrap journal "
                f"{plan.bootstrap_id} matches project/revision but not the "
                "exact prepared-release identity (manifest hash, API image, "
                "overlay, source contract, target heads, and committed result)"
            )
        matches.append(plan)
    if len(matches) > 1:
        raise BootstrapJournalError(
            "multiple committed bootstrap journals for the same identity"
        )
    return matches[0] if matches else None


def prepare_bootstrap_dir(deploy_root: Path, bootstrap_id: str) -> Path:
    path = bootstrap_dir(deploy_root, bootstrap_id)
    if path.exists():
        raise BootstrapJournalError(f"bootstrap journal already exists: {bootstrap_id}")
    path.mkdir(mode=0o700, parents=False)
    (path / BOOTSTRAP_EVENTS_DIRNAME).mkdir(mode=0o700)
    (path / BOOTSTRAP_OVERRIDES_DIRNAME).mkdir(mode=0o700)
    return path
