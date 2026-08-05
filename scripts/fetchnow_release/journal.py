"""Deployment journal, current state, and legal transitions (PRD1C3A)."""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .c3_constants import (
    BOOTSTRAP_CLEANUP_AUDIT_PREFIX,
    BOOTSTRAP_FAILURE_TRANSITIONS,
    CURRENT_SCHEMA_VERSION,
    CURRENT_STATE_NAME,
    DEPLOYMENTS_DIRNAME,
    EVENT_SCHEMA_VERSION,
    EVENTS_DIRNAME,
    LEGAL_TRANSITIONS,
    OVERRIDES_DIRNAME,
    PLAN_NAME,
    PLAN_SCHEMA_VERSION,
    RECOVERIES_DIRNAME,
    RECOVERY_LEGAL_TRANSITIONS,
    RECOVERY_PLAN_SCHEMA_VERSION,
    RECOVERY_SUCCESS_STATUSES,
    RESOLUTION_NAME,
    RESOLUTION_OUTCOME_ACCEPT_TARGET,
    RESOLUTION_OUTCOME_ROLLBACK,
    RESOLUTION_SCHEMA_VERSION,
    RESULT_NAME,
    RESULT_SCHEMA_VERSION,
    STATE_DIRNAME,
    STATUS_BOOTSTRAP_CLEANED,
    STATUS_BOOTSTRAP_CLEANUP_STARTED,
    STATUS_COMMITTED,
    STATUS_FAILED,
    STATUS_PLANNED,
    STATUS_ROLLBACK_FAILED,
    STATUS_ROLLED_BACK,
    TERMINAL_RESULT_STATUSES,
)
from .deploy_root import DeployRootError, ensure_deploy_layout
from .journal_io import atomic_write_json, read_json
from .revision import validate_full_sha

_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_DEPLOYMENT_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class JournalError(ValueError):
    """Invalid journal / current state."""


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_deployment_id() -> str:
    return str(uuid.uuid4())


def new_recovery_id() -> str:
    return str(uuid.uuid4())


def assert_legal_transition(current: str, nxt: str) -> None:
    allowed = LEGAL_TRANSITIONS.get(current)
    if allowed is None:
        raise JournalError(f"unknown journal status: {current!r}")
    if nxt not in allowed:
        raise JournalError(f"illegal journal transition {current!r} -> {nxt!r}")


def state_dir(deploy_root: Path) -> Path:
    return deploy_root / STATE_DIRNAME


def current_path(deploy_root: Path) -> Path:
    return state_dir(deploy_root) / CURRENT_STATE_NAME


def deployments_dir(deploy_root: Path) -> Path:
    return deploy_root / DEPLOYMENTS_DIRNAME


def deployment_dir(deploy_root: Path, deployment_id: str) -> Path:
    if not _DEPLOYMENT_ID_RE.fullmatch(deployment_id):
        raise JournalError(f"malformed deployment id: {deployment_id!r}")
    path = deployments_dir(deploy_root) / deployment_id
    if path.resolve().parent != deployments_dir(deploy_root).resolve():
        raise JournalError("deployment path escape rejected")
    return path


def ensure_rollout_layout(deploy_root: Path) -> None:
    ensure_deploy_layout(deploy_root)
    for d in (state_dir(deploy_root), deployments_dir(deploy_root)):
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        if d.is_symlink():
            raise DeployRootError(f"refusing symlink layout dir: {d}")
        import os

        os.chmod(d, 0o700)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_image_id_map(ids: dict[str, str]) -> dict[str, str]:
    required = {"api", "worker", "web", "gateway"}
    if set(ids) != required:
        raise JournalError(f"image_ids keys {sorted(ids)} != {sorted(required)}")
    for k, v in ids.items():
        if not _IMAGE_ID_RE.fullmatch(v):
            raise JournalError(f"malformed image ID for {k}: {v!r}")
    if ids["api"] != ids["worker"]:
        raise JournalError("api and worker image IDs must match")
    return dict(ids)


@dataclass(frozen=True)
class CurrentState:
    schema_version: int
    revision: str
    release_manifest_sha256: str
    image_ids: dict[str, str]
    updated_at_utc: str
    deployment_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "release_manifest_sha256": self.release_manifest_sha256,
            "image_ids": dict(self.image_ids),
            "updated_at_utc": self.updated_at_utc,
            "deployment_id": self.deployment_id,
        }


def parse_current_state(raw: dict[str, Any]) -> CurrentState:
    try:
        schema = int(raw["schema_version"])
        revision = validate_full_sha(str(raw["revision"]))
        man_hash = str(raw["release_manifest_sha256"])
        updated = str(raw["updated_at_utc"])
        dep_id = str(raw["deployment_id"])
        ids = validate_image_id_map({str(k): str(v) for k, v in raw["image_ids"].items()})
    except (KeyError, TypeError, ValueError) as exc:
        raise JournalError(f"invalid current.json: {exc}") from exc
    if schema != CURRENT_SCHEMA_VERSION:
        raise JournalError(f"unsupported current schema: {schema}")
    if not re.fullmatch(r"^[0-9a-f]{64}$", man_hash):
        raise JournalError("release_manifest_sha256 must be 64 hex chars")
    if not _DEPLOYMENT_ID_RE.fullmatch(dep_id):
        raise JournalError("current.deployment_id malformed")
    return CurrentState(
        schema_version=schema,
        revision=revision,
        release_manifest_sha256=man_hash,
        image_ids=ids,
        updated_at_utc=updated,
        deployment_id=dep_id,
    )


def load_current_state(deploy_root: Path) -> CurrentState | None:
    path = current_path(deploy_root)
    if not path.exists():
        return None
    return parse_current_state(read_json(path))


def write_current_state(deploy_root: Path, state: CurrentState) -> None:
    if state.schema_version != CURRENT_SCHEMA_VERSION:
        raise JournalError("unsupported current schema")
    validate_full_sha(state.revision)
    validate_image_id_map(state.image_ids)
    ensure_rollout_layout(deploy_root)
    atomic_write_json(current_path(deploy_root), state.to_dict(), mode=0o600)


@dataclass(frozen=True)
class PlanDocument:
    schema_version: int
    deployment_id: str
    target_revision: str
    previous_revision: str | None
    bootstrap: bool
    target_image_ids: dict[str, str]
    previous_image_ids: dict[str, str] | None
    release_manifest_sha256: str
    database_heads: tuple[str, ...]
    created_at_utc: str
    project_name: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "deployment_id": self.deployment_id,
            "target_revision": self.target_revision,
            "previous_revision": self.previous_revision,
            "bootstrap": self.bootstrap,
            "target_image_ids": dict(self.target_image_ids),
            "previous_image_ids": (
                None
                if self.previous_image_ids is None
                else dict(self.previous_image_ids)
            ),
            "release_manifest_sha256": self.release_manifest_sha256,
            "database_heads": list(self.database_heads),
            "created_at_utc": self.created_at_utc,
            "project_name": self.project_name,
        }


def parse_plan(raw: dict[str, Any]) -> PlanDocument:
    try:
        schema = int(raw["schema_version"])
        dep_id = str(raw["deployment_id"])
        target = validate_full_sha(str(raw["target_revision"]))
        prev_raw = raw["previous_revision"]
        previous = None if prev_raw is None else validate_full_sha(str(prev_raw))
        bootstrap = bool(raw["bootstrap"])
        target_ids = validate_image_id_map(
            {str(k): str(v) for k, v in raw["target_image_ids"].items()}
        )
        prev_ids_raw = raw["previous_image_ids"]
        prev_ids = (
            None
            if prev_ids_raw is None
            else validate_image_id_map(
                {str(k): str(v) for k, v in prev_ids_raw.items()}
            )
        )
        man_hash = str(raw["release_manifest_sha256"])
        heads = tuple(str(x) for x in raw["database_heads"])
        created = str(raw["created_at_utc"])
        project = str(raw["project_name"])
    except (KeyError, TypeError, ValueError) as exc:
        raise JournalError(f"invalid plan.json: {exc}") from exc
    if schema != PLAN_SCHEMA_VERSION:
        raise JournalError(f"unsupported plan schema: {schema}")
    if not _DEPLOYMENT_ID_RE.fullmatch(dep_id):
        raise JournalError("plan.deployment_id malformed")
    if not re.fullmatch(r"^[0-9a-f]{64}$", man_hash):
        raise JournalError("plan release_manifest_sha256 malformed")
    if bootstrap and previous is not None:
        raise JournalError("bootstrap plan must not have previous_revision")
    if not bootstrap and previous is None:
        raise JournalError("non-bootstrap plan requires previous_revision")
    if not heads:
        raise JournalError("database_heads must be non-empty")
    return PlanDocument(
        schema_version=schema,
        deployment_id=dep_id,
        target_revision=target,
        previous_revision=previous,
        bootstrap=bootstrap,
        target_image_ids=target_ids,
        previous_image_ids=prev_ids,
        release_manifest_sha256=man_hash,
        database_heads=heads,
        created_at_utc=created,
        project_name=project,
    )


def write_plan(dep_dir: Path, plan: PlanDocument) -> None:
    path = dep_dir / PLAN_NAME
    if path.exists():
        raise JournalError("plan.json is immutable and already exists")
    dep_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    (dep_dir / EVENTS_DIRNAME).mkdir(mode=0o700, exist_ok=True)
    (dep_dir / OVERRIDES_DIRNAME).mkdir(mode=0o700, exist_ok=True)
    atomic_write_json(path, plan.to_dict(), mode=0o600)


def load_plan(dep_dir: Path) -> PlanDocument:
    return parse_plan(read_json(dep_dir / PLAN_NAME))


def append_event(
    dep_dir: Path, status: str, detail: str = "", *, legal: dict[str, frozenset[str]] | None = None
) -> int:
    transitions = LEGAL_TRANSITIONS if legal is None else legal
    events = dep_dir / EVENTS_DIRNAME
    events.mkdir(mode=0o700, exist_ok=True)
    existing = sorted(p for p in events.glob("*.json") if p.is_file())
    seq = len(existing) + 1
    if existing:
        last = read_json(existing[-1])
        prev_status = str(last.get("status") or "")
        allowed = transitions.get(prev_status)
        if allowed is None or status not in allowed:
            raise JournalError(f"illegal journal transition {prev_status!r} -> {status!r}")
    elif status != STATUS_PLANNED:
        raise JournalError(
            f"first journal event must be {STATUS_PLANNED!r}, got {status!r}"
        )
    name = f"{seq:04d}_{status}.json"
    payload = {
        "schema_version": EVENT_SCHEMA_VERSION,
        "seq": seq,
        "status": status,
        "at_utc": utc_now(),
        "detail": detail,
    }
    atomic_write_json(events / name, payload, mode=0o600)
    return seq


def append_recovery_event(dep_dir: Path, status: str, detail: str = "") -> int:
    return append_event(dep_dir, status, detail, legal=RECOVERY_LEGAL_TRANSITIONS)


def latest_event_status(dep_dir: Path) -> str | None:
    events = dep_dir / EVENTS_DIRNAME
    if not events.is_dir():
        return None
    files = sorted(p for p in events.glob("*.json") if p.is_file())
    if not files:
        return None
    return str(read_json(files[-1]).get("status") or "")


@dataclass(frozen=True)
class ResultDocument:
    schema_version: int
    deployment_id: str
    status: str
    finished_at_utc: str
    messages: tuple[str, ...]


def parse_result(raw: dict[str, Any]) -> ResultDocument:
    try:
        schema = int(raw["schema_version"])
        dep_id = str(raw["deployment_id"])
        status = str(raw["status"])
        finished = str(raw["finished_at_utc"])
        messages_raw = raw["messages"]
        if not isinstance(messages_raw, list):
            raise TypeError("messages must be a list")
        messages = tuple(str(m) for m in messages_raw)
    except (KeyError, TypeError, ValueError) as exc:
        raise JournalError(f"invalid result.json: {exc}") from exc
    if schema != RESULT_SCHEMA_VERSION:
        raise JournalError(f"unsupported result schema: {schema}")
    if not _DEPLOYMENT_ID_RE.fullmatch(dep_id):
        raise JournalError("result.deployment_id malformed")
    if status not in TERMINAL_RESULT_STATUSES:
        raise JournalError(f"result status not terminal: {status!r}")
    return ResultDocument(
        schema_version=schema,
        deployment_id=dep_id,
        status=status,
        finished_at_utc=finished,
        messages=messages,
    )


def append_bootstrap_failure_event(dep_dir: Path, status: str, detail: str = "") -> int:
    return append_event(dep_dir, status, detail, legal=BOOTSTRAP_FAILURE_TRANSITIONS)


def validate_bootstrap_failed_result(
    plan: PlanDocument, messages: tuple[str, ...]
) -> None:
    if not plan.bootstrap:
        raise JournalError("bootstrap failed result requires bootstrap plan")
    if plan.previous_revision is not None or plan.previous_image_ids is not None:
        raise JournalError("bootstrap failed result must not reference previous release")
    if not any(str(m).startswith(BOOTSTRAP_CLEANUP_AUDIT_PREFIX) for m in messages):
        raise JournalError("bootstrap failed result missing cleanup audit")


def publish_bootstrap_failed_after_cleanup(
    dep_dir: Path,
    *,
    plan: PlanDocument,
    deployment_id: str,
    messages: tuple[str, ...],
    detail: str,
) -> None:
    """Record bootstrap cleanup completion and terminal failed result."""
    validate_bootstrap_failed_result(plan, messages)
    append_bootstrap_failure_event(dep_dir, STATUS_BOOTSTRAP_CLEANED, detail)
    append_bootstrap_failure_event(dep_dir, STATUS_FAILED, detail)
    write_result(
        dep_dir,
        deployment_id=deployment_id,
        status=STATUS_FAILED,
        messages=messages,
    )


def begin_bootstrap_cleanup_event(dep_dir: Path, detail: str = "") -> int:
    return append_bootstrap_failure_event(
        dep_dir, STATUS_BOOTSTRAP_CLEANUP_STARTED, detail or "ownership-verified cleanup"
    )


def write_result(
    dep_dir: Path,
    *,
    deployment_id: str,
    status: str,
    messages: tuple[str, ...],
) -> None:
    if status not in TERMINAL_RESULT_STATUSES:
        raise JournalError(f"result status must be terminal, got {status!r}")
    path = dep_dir / RESULT_NAME
    if path.exists():
        raise JournalError("result.json already published")
    if status == STATUS_FAILED:
        events = dep_dir / EVENTS_DIRNAME
        if events.is_dir():
            statuses = [
                str(read_json(event_path).get("status") or "")
                for event_path in sorted(events.glob("*.json"))
            ]
            if STATUS_BOOTSTRAP_CLEANUP_STARTED in statuses:
                plan_path = dep_dir / PLAN_NAME
                if not plan_path.is_file():
                    raise JournalError("bootstrap failed result requires plan.json")
                validate_bootstrap_failed_result(load_plan(dep_dir), messages)
    payload = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "deployment_id": deployment_id,
        "status": status,
        "finished_at_utc": utc_now(),
        "messages": list(messages),
    }
    atomic_write_json(path, payload, mode=0o600)


def load_result(dep_dir: Path) -> dict[str, Any] | None:
    path = dep_dir / RESULT_NAME
    if not path.exists():
        return None
    return read_json(path)


def result_path(dep_dir: Path) -> Path:
    return dep_dir / RESULT_NAME


def recoveries_dir(dep_dir: Path) -> Path:
    return dep_dir / RECOVERIES_DIRNAME


def recovery_dir(dep_dir: Path, recovery_id: str) -> Path:
    if not _DEPLOYMENT_ID_RE.fullmatch(recovery_id):
        raise JournalError(f"malformed recovery id: {recovery_id!r}")
    path = recoveries_dir(dep_dir) / recovery_id
    if path.resolve().parent != recoveries_dir(dep_dir).resolve():
        raise JournalError("recovery path escape rejected")
    return path


@dataclass(frozen=True)
class RecoveryPlanDocument:
    schema_version: int
    recovery_id: str
    deployment_id: str
    action: str
    created_at_utc: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "recovery_id": self.recovery_id,
            "deployment_id": self.deployment_id,
            "action": self.action,
            "created_at_utc": self.created_at_utc,
        }


def parse_recovery_plan(raw: dict[str, Any]) -> RecoveryPlanDocument:
    try:
        schema = int(raw["schema_version"])
        recovery_id = str(raw["recovery_id"])
        deployment_id = str(raw["deployment_id"])
        action = str(raw["action"])
        created = str(raw["created_at_utc"])
    except (KeyError, TypeError, ValueError) as exc:
        raise JournalError(f"invalid recovery plan.json: {exc}") from exc
    if schema != RECOVERY_PLAN_SCHEMA_VERSION:
        raise JournalError(f"unsupported recovery plan schema: {schema}")
    if not _DEPLOYMENT_ID_RE.fullmatch(recovery_id):
        raise JournalError("recovery_id malformed")
    if not _DEPLOYMENT_ID_RE.fullmatch(deployment_id):
        raise JournalError("recovery plan deployment_id malformed")
    if action not in {"accept-target", "rollback"}:
        raise JournalError(f"invalid recovery action: {action!r}")
    return RecoveryPlanDocument(
        schema_version=schema,
        recovery_id=recovery_id,
        deployment_id=deployment_id,
        action=action,
        created_at_utc=created,
    )


def write_recovery_plan(rec_dir: Path, plan: RecoveryPlanDocument) -> None:
    path = rec_dir / PLAN_NAME
    if path.exists():
        raise JournalError("recovery plan.json is immutable and already exists")
    rec_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    (rec_dir / EVENTS_DIRNAME).mkdir(mode=0o700, exist_ok=True)
    atomic_write_json(path, plan.to_dict(), mode=0o600)


def write_recovery_result(
    rec_dir: Path,
    *,
    recovery_id: str,
    status: str,
    messages: tuple[str, ...],
) -> None:
    if status not in TERMINAL_RESULT_STATUSES:
        raise JournalError(f"recovery result status must be terminal, got {status!r}")
    path = rec_dir / RESULT_NAME
    if path.exists():
        raise JournalError("recovery result.json already published")
    payload = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "deployment_id": recovery_id,
        "status": status,
        "finished_at_utc": utc_now(),
        "messages": list(messages),
    }
    atomic_write_json(path, payload, mode=0o600)


@dataclass(frozen=True)
class ResolutionDocument:
    schema_version: int
    deployment_id: str
    deployment_result_sha256: str
    recovery_id: str
    recovery_result_sha256: str
    final_active_revision: str
    outcome: str
    resolved_at_utc: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "deployment_id": self.deployment_id,
            "deployment_result_sha256": self.deployment_result_sha256,
            "recovery_id": self.recovery_id,
            "recovery_result_sha256": self.recovery_result_sha256,
            "final_active_revision": self.final_active_revision,
            "outcome": self.outcome,
            "resolved_at_utc": self.resolved_at_utc,
        }


def parse_resolution(raw: dict[str, Any]) -> ResolutionDocument:
    try:
        schema = int(raw["schema_version"])
        deployment_id = str(raw["deployment_id"])
        dep_result_hash = str(raw["deployment_result_sha256"])
        recovery_id = str(raw["recovery_id"])
        rec_result_hash = str(raw["recovery_result_sha256"])
        final_rev = validate_full_sha(str(raw["final_active_revision"]))
        outcome = str(raw["outcome"])
        resolved = str(raw["resolved_at_utc"])
    except (KeyError, TypeError, ValueError) as exc:
        raise JournalError(f"invalid resolution.json: {exc}") from exc
    if schema != RESOLUTION_SCHEMA_VERSION:
        raise JournalError(f"unsupported resolution schema: {schema}")
    if not _DEPLOYMENT_ID_RE.fullmatch(deployment_id):
        raise JournalError("resolution deployment_id malformed")
    if not _DEPLOYMENT_ID_RE.fullmatch(recovery_id):
        raise JournalError("resolution recovery_id malformed")
    for label, value in (
        ("deployment_result_sha256", dep_result_hash),
        ("recovery_result_sha256", rec_result_hash),
    ):
        if not re.fullmatch(r"^[0-9a-f]{64}$", value):
            raise JournalError(f"{label} must be 64 hex chars")
    if outcome not in {RESOLUTION_OUTCOME_ACCEPT_TARGET, RESOLUTION_OUTCOME_ROLLBACK}:
        raise JournalError(f"invalid resolution outcome: {outcome!r}")
    return ResolutionDocument(
        schema_version=schema,
        deployment_id=deployment_id,
        deployment_result_sha256=dep_result_hash,
        recovery_id=recovery_id,
        recovery_result_sha256=rec_result_hash,
        final_active_revision=final_rev,
        outcome=outcome,
        resolved_at_utc=resolved,
    )


def load_resolution(dep_dir: Path) -> dict[str, Any] | None:
    path = dep_dir / RESOLUTION_NAME
    if not path.exists():
        return None
    return read_json(path)


def validate_resolution(dep_dir: Path, resolution: ResolutionDocument) -> None:
    dep_result = result_path(dep_dir)
    if not dep_result.is_file():
        raise JournalError("resolution references missing deployment result.json")
    dep_hash = sha256_file(dep_result)
    if dep_hash != resolution.deployment_result_sha256:
        raise JournalError("resolution deployment_result_sha256 mismatch")
    rec_result = recovery_dir(dep_dir, resolution.recovery_id) / RESULT_NAME
    if not rec_result.is_file():
        raise JournalError("resolution references missing recovery result.json")
    rec_hash = sha256_file(rec_result)
    if rec_hash != resolution.recovery_result_sha256:
        raise JournalError("resolution recovery_result_sha256 mismatch")
    rec_plan = recovery_dir(dep_dir, resolution.recovery_id) / PLAN_NAME
    if not rec_plan.is_file():
        raise JournalError("resolution references missing recovery plan.json")
    rec_parsed = parse_recovery_plan(read_json(rec_plan))
    if rec_parsed.recovery_id != resolution.recovery_id:
        raise JournalError("resolution recovery_id mismatch with recovery plan")
    if rec_parsed.deployment_id != resolution.deployment_id:
        raise JournalError("resolution deployment_id mismatch with recovery plan")
    rec_result_doc = parse_result(read_json(rec_result))
    if rec_result_doc.status not in RECOVERY_SUCCESS_STATUSES:
        raise JournalError("resolution recovery result is not successful")


def write_resolution(dep_dir: Path, resolution: ResolutionDocument) -> None:
    path = dep_dir / RESOLUTION_NAME
    if path.exists():
        raise JournalError("resolution.json already published")
    validate_resolution(dep_dir, resolution)
    atomic_write_json(path, resolution.to_dict(), mode=0o600)


def deployment_blocks_rollout(dep_dir: Path) -> bool:
    """Return True when this deployment journal must block new rollouts."""
    result_raw = load_result(dep_dir)
    if result_raw is None:
        return True
    try:
        result = parse_result(result_raw)
    except JournalError:
        return True
    if result.status in {STATUS_COMMITTED, STATUS_ROLLED_BACK, STATUS_FAILED}:
        return False
    if result.status == STATUS_ROLLBACK_FAILED:
        resolution_raw = load_resolution(dep_dir)
        if resolution_raw is None:
            return True
        try:
            resolution = parse_resolution(resolution_raw)
            validate_resolution(dep_dir, resolution)
        except JournalError:
            return True
        return False
    return True


def find_unresolved_deployments(deploy_root: Path) -> list[str]:
    root = deployments_dir(deploy_root)
    if not root.is_dir():
        return []
    unresolved: list[str] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.is_symlink():
            continue
        if not _DEPLOYMENT_ID_RE.fullmatch(child.name):
            continue
        if deployment_blocks_rollout(child):
            unresolved.append(child.name)
    return unresolved
