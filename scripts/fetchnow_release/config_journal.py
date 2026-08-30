"""Durable journal for runtime config-only rollout transactions."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .journal import utc_now
from .journal_io import atomic_write_json, read_json

CONFIG_ROLLOUTS_DIRNAME = "config-rollouts"
CONFIG_PLAN_NAME = "plan.json"
CONFIG_RESULT_NAME = "result.json"
CONFIG_EVENTS_DIRNAME = "events"
CONFIG_PLAN_SCHEMA = 1
CONFIG_RESULT_SCHEMA = 1
CONFIG_EVENT_SCHEMA = 1

STATUS_PLANNED = "planned"
STATUS_ACTIVATING = "activating"
STATUS_STABILIZING = "stabilizing"
STATUS_COMMITTED = "committed"
STATUS_ROLLBACK_STARTED = "rollback_started"
STATUS_ROLLED_BACK = "rolled_back"
STATUS_ROLLBACK_FAILED = "rollback_failed"
STATUS_FAILED = "failed"
TERMINAL = {STATUS_COMMITTED, STATUS_ROLLED_BACK, STATUS_ROLLBACK_FAILED, STATUS_FAILED}
TRANSITIONS = {
    STATUS_PLANNED: {STATUS_ACTIVATING, STATUS_FAILED},
    STATUS_ACTIVATING: {STATUS_STABILIZING, STATUS_ROLLBACK_STARTED},
    STATUS_STABILIZING: {STATUS_COMMITTED, STATUS_ROLLBACK_STARTED},
    STATUS_ROLLBACK_STARTED: {STATUS_ROLLED_BACK, STATUS_ROLLBACK_FAILED},
}
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class ConfigJournalError(ValueError):
    """Invalid or unresolved config rollout journal."""


@dataclass(frozen=True)
class ConfigPlan:
    config_rollout_id: str
    expected_revision: str
    previous_runtime_config_fingerprint: str
    target_runtime_config_fingerprint: str
    changed_keys: tuple[str, ...]
    affected_services: tuple[str, ...]
    previous_deployment_id: str
    previous_config_rollout_id: str | None
    created_at_utc: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CONFIG_PLAN_SCHEMA,
            "config_rollout_id": self.config_rollout_id,
            "expected_revision": self.expected_revision,
            "previous_runtime_config_fingerprint": self.previous_runtime_config_fingerprint,
            "target_runtime_config_fingerprint": self.target_runtime_config_fingerprint,
            "changed_keys": list(self.changed_keys),
            "affected_services": list(self.affected_services),
            "previous_deployment_id": self.previous_deployment_id,
            "previous_config_rollout_id": self.previous_config_rollout_id,
            "created_at_utc": self.created_at_utc,
        }


def config_rollout_root(deploy_root: Path) -> Path:
    return deploy_root / CONFIG_ROLLOUTS_DIRNAME


def config_rollout_dir(deploy_root: Path, rollout_id: str) -> Path:
    if not _UUID_RE.fullmatch(rollout_id):
        raise ConfigJournalError("config rollout ID malformed")
    return config_rollout_root(deploy_root) / rollout_id


def new_config_rollout_id() -> str:
    return str(uuid.uuid4())


def write_config_plan(directory: Path, plan: ConfigPlan) -> None:
    path = directory / CONFIG_PLAN_NAME
    if path.exists():
        raise ConfigJournalError("config rollout plan already exists")
    atomic_write_json(path, plan.to_dict(), mode=0o600)


def _event_files(directory: Path) -> list[Path]:
    events = directory / CONFIG_EVENTS_DIRNAME
    return sorted(events.glob("*.json")) if events.is_dir() else []


def latest_status(directory: Path) -> str | None:
    files = _event_files(directory)
    if not files:
        return None
    return str(read_json(files[-1])["status"])


def append_config_event(directory: Path, status: str, detail: str = "") -> None:
    previous = latest_status(directory)
    if previous is None:
        if status != STATUS_PLANNED:
            raise ConfigJournalError("first config rollout event must be planned")
    elif status not in TRANSITIONS.get(previous, set()):
        raise ConfigJournalError(
            f"illegal config rollout transition {previous!r} -> {status!r}"
        )
    events = directory / CONFIG_EVENTS_DIRNAME
    events.mkdir(parents=True, exist_ok=True)
    sequence = len(_event_files(directory)) + 1
    atomic_write_json(
        events / f"{sequence:04d}.json",
        {
            "schema_version": CONFIG_EVENT_SCHEMA,
            "sequence": sequence,
            "status": status,
            "detail": detail,
            "recorded_at_utc": utc_now(),
        },
        mode=0o600,
    )


def write_config_result(
    directory: Path,
    *,
    rollout_id: str,
    status: str,
    health_result: str,
    rollback_status: str | None,
) -> None:
    if status not in TERMINAL:
        raise ConfigJournalError("config result status must be terminal")
    path = directory / CONFIG_RESULT_NAME
    if path.exists():
        raise ConfigJournalError("config rollout result already exists")
    atomic_write_json(
        path,
        {
            "schema_version": CONFIG_RESULT_SCHEMA,
            "config_rollout_id": rollout_id,
            "status": status,
            "health_result": health_result,
            "rollback_status": rollback_status,
            "recorded_at_utc": utc_now(),
        },
        mode=0o600,
    )


def find_unresolved_config_rollouts(deploy_root: Path) -> tuple[str, ...]:
    root = config_rollout_root(deploy_root)
    if not root.is_dir():
        return ()
    unresolved: list[str] = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or not _UUID_RE.fullmatch(directory.name):
            continue
        result = directory / CONFIG_RESULT_NAME
        if not result.is_file():
            unresolved.append(directory.name)
    return tuple(unresolved)
