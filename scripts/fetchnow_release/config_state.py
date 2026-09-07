"""Sanitized active runtime-config state, separate from current.json."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .c3_constants import STATE_DIRNAME
from .config_contract import (
    ConfigContractError,
    build_config_fingerprint,
    legacy_runtime_config_fingerprint_schema1,
    legacy_runtime_config_fingerprint_schema2,
    normalize_build_values,
    normalize_runtime_values,
    runtime_config_fingerprint,
)
from .journal_io import atomic_write_json, read_json
from .revision import validate_full_sha

RUNTIME_CONFIG_STATE_NAME = "runtime-config.json"
RUNTIME_CONFIG_STATE_SCHEMA = 3
LEGACY_RUNTIME_CONFIG_STATE_SCHEMA = 1
LEGACY_RUNTIME_CONFIG_STATE_SCHEMA_2 = 2
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class ConfigStateError(ValueError):
    """Invalid or unsafe active runtime-config state."""


@dataclass(frozen=True)
class RuntimeConfigState:
    schema_version: int
    revision: str
    deployment_id: str
    latest_config_rollout_id: str | None
    runtime_config_fingerprint: str
    runtime_values: dict[str, str]
    build_config_fingerprint: str
    build_values: dict[str, str]
    updated_at_utc: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "deployment_id": self.deployment_id,
            "latest_config_rollout_id": self.latest_config_rollout_id,
            "runtime_config_fingerprint": self.runtime_config_fingerprint,
            "runtime_values": dict(self.runtime_values),
            "build_config_fingerprint": self.build_config_fingerprint,
            "build_values": dict(self.build_values),
            "updated_at_utc": self.updated_at_utc,
        }


def runtime_config_state_path(deploy_root: Path) -> Path:
    return deploy_root / STATE_DIRNAME / RUNTIME_CONFIG_STATE_NAME


def build_runtime_config_state(
    *,
    revision: str,
    deployment_id: str,
    latest_config_rollout_id: str | None,
    runtime_values: Mapping[str, object],
    build_values: Mapping[str, object],
    updated_at_utc: str,
) -> RuntimeConfigState:
    runtime = normalize_runtime_values(runtime_values)
    build = normalize_build_values(build_values)
    state = RuntimeConfigState(
        schema_version=RUNTIME_CONFIG_STATE_SCHEMA,
        revision=validate_full_sha(revision),
        deployment_id=deployment_id,
        latest_config_rollout_id=latest_config_rollout_id,
        runtime_config_fingerprint=runtime_config_fingerprint(runtime),
        runtime_values=runtime,
        build_config_fingerprint=build_config_fingerprint(build),
        build_values=build,
        updated_at_utc=updated_at_utc,
    )
    validate_runtime_config_state(state)
    return state


def validate_runtime_config_state(state: RuntimeConfigState) -> None:
    if state.schema_version not in {
        LEGACY_RUNTIME_CONFIG_STATE_SCHEMA,
        LEGACY_RUNTIME_CONFIG_STATE_SCHEMA_2,
        RUNTIME_CONFIG_STATE_SCHEMA,
    }:
        raise ConfigStateError("unsupported runtime config state schema")
    validate_full_sha(state.revision)
    if not _UUID_RE.fullmatch(state.deployment_id):
        raise ConfigStateError("runtime config deployment_id malformed")
    if state.latest_config_rollout_id is not None and not _UUID_RE.fullmatch(
        state.latest_config_rollout_id
    ):
        raise ConfigStateError("latest_config_rollout_id malformed")
    if not _UTC_RE.fullmatch(state.updated_at_utc):
        raise ConfigStateError("runtime config updated_at_utc malformed")
    if not _HASH_RE.fullmatch(state.runtime_config_fingerprint):
        raise ConfigStateError("runtime config fingerprint malformed")
    if not _HASH_RE.fullmatch(state.build_config_fingerprint):
        raise ConfigStateError("build config fingerprint malformed")
    try:
        if state.schema_version == LEGACY_RUNTIME_CONFIG_STATE_SCHEMA:
            expected_runtime_fingerprint = legacy_runtime_config_fingerprint_schema1(
                state.runtime_values
            )
        elif state.schema_version == LEGACY_RUNTIME_CONFIG_STATE_SCHEMA_2:
            expected_runtime_fingerprint = legacy_runtime_config_fingerprint_schema2(
                state.runtime_values
            )
        else:
            expected_runtime_fingerprint = runtime_config_fingerprint(
                state.runtime_values
            )
    except ConfigContractError as exc:
        raise ConfigStateError("runtime config values violate schema") from exc
    if expected_runtime_fingerprint != state.runtime_config_fingerprint:
        raise ConfigStateError("runtime config fingerprint mismatch")
    if build_config_fingerprint(state.build_values) != state.build_config_fingerprint:
        raise ConfigStateError("build config fingerprint mismatch")


def parse_runtime_config_state(raw: dict[str, Any]) -> RuntimeConfigState:
    required = {
        "schema_version",
        "revision",
        "deployment_id",
        "latest_config_rollout_id",
        "runtime_config_fingerprint",
        "runtime_values",
        "build_config_fingerprint",
        "build_values",
        "updated_at_utc",
    }
    if set(raw) != required:
        raise ConfigStateError("runtime config state keys mismatch")
    if not isinstance(raw["runtime_values"], dict) or not isinstance(
        raw["build_values"], dict
    ):
        raise ConfigStateError("runtime/build values must be objects")
    if type(raw["schema_version"]) is not int:
        raise ConfigStateError("runtime config schema_version must be integer")
    state = RuntimeConfigState(
        schema_version=raw["schema_version"],
        revision=str(raw["revision"]),
        deployment_id=str(raw["deployment_id"]),
        latest_config_rollout_id=(
            None
            if raw["latest_config_rollout_id"] is None
            else str(raw["latest_config_rollout_id"])
        ),
        runtime_config_fingerprint=str(raw["runtime_config_fingerprint"]),
        runtime_values={str(k): str(v) for k, v in raw["runtime_values"].items()},
        build_config_fingerprint=str(raw["build_config_fingerprint"]),
        build_values={str(k): str(v) for k, v in raw["build_values"].items()},
        updated_at_utc=str(raw["updated_at_utc"]),
    )
    validate_runtime_config_state(state)
    return state


def load_runtime_config_state(deploy_root: Path) -> RuntimeConfigState | None:
    path = runtime_config_state_path(deploy_root)
    if not path.exists():
        return None
    if path.is_symlink():
        raise ConfigStateError("refusing symlink runtime-config.json")
    return parse_runtime_config_state(read_json(path))


def write_runtime_config_state(deploy_root: Path, state: RuntimeConfigState) -> None:
    validate_runtime_config_state(state)
    atomic_write_json(
        runtime_config_state_path(deploy_root), state.to_dict(), mode=0o600
    )
