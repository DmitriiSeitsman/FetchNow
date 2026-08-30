"""Reviewed runtime-config contract for config-only release transactions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


class ConfigContractError(ValueError):
    """Unsafe, unknown, or malformed config-only change."""


FINGERPRINT_DOMAIN = b"fetchnow:runtime-config:v1\0"
BUILD_FINGERPRINT_DOMAIN = b"fetchnow:build-config:v1\0"


@dataclass(frozen=True)
class RuntimeConfigSpec:
    services: tuple[str, ...]
    kind: str


# Deliberately narrow. Extending this map is a reviewed release-tooling change.
RUNTIME_CONFIG_ALLOWLIST: dict[str, RuntimeConfigSpec] = {
    "FREE_DOWNLOAD_QUOTA_ENABLED": RuntimeConfigSpec(("api",), "boolean"),
}

# These values are baked into immutable images and must never be applied by a
# config-only rollout. They are safe, non-secret values suitable for snapshotting.
BUILD_TIME_CONFIG: dict[str, tuple[str, ...]] = {
    "PUBLIC_MEDIA_FLOW_ENABLED": ("web",),
    "PUBLIC_SEARCH_INDEXING_ENABLED": ("web",),
    "PUBLIC_SITE_URL": ("web",),
}

# Runtime-wired MEDIA/FREE keys are classified for documentation and explicit
# review. Only keys also present in RUNTIME_CONFIG_ALLOWLIST may change.
RUNTIME_WIRING: dict[str, tuple[str, ...]] = {
    "FREE_DOWNLOAD_QUOTA_ENABLED": ("api",),
    "FREE_DOWNLOAD_LIMIT": ("api", "worker"),
    "FREE_DOWNLOAD_WINDOW_SECONDS": ("api", "worker"),
    "FREE_DOWNLOAD_QUOTA_RETENTION_SECONDS": ("api", "worker"),
    "MEDIA_INSPECTION_ENABLED": ("api", "worker"),
    "MEDIA_JOBS_ENABLED": ("api", "worker"),
    "MEDIA_DOWNLOADS_ENABLED": ("api", "worker"),
    "MEDIA_DELIVERY_ENABLED": ("delivery",),
    "MEDIA_BROWSER_DELIVERY_ENABLED": ("api", "worker", "delivery"),
    "MEDIA_MUXING_ENABLED": ("worker",),
}


def _normalize_boolean(value: object, *, key: str) -> str:
    if not isinstance(value, str):
        raise ConfigContractError(f"{key} must be a string boolean")
    normalized = value.strip().lower()
    if normalized not in {"true", "false"}:
        raise ConfigContractError(f"{key} must be true or false")
    return normalized


def normalize_runtime_values(values: Mapping[str, object]) -> dict[str, str]:
    if set(values) != set(RUNTIME_CONFIG_ALLOWLIST):
        raise ConfigContractError("runtime config keys do not match reviewed allowlist")
    normalized: dict[str, str] = {}
    for key in sorted(RUNTIME_CONFIG_ALLOWLIST):
        spec = RUNTIME_CONFIG_ALLOWLIST[key]
        if spec.kind == "boolean":
            normalized[key] = _normalize_boolean(values[key], key=key)
        else:  # pragma: no cover - fail closed if a future kind lacks a parser
            raise ConfigContractError(f"unsupported config kind for {key}")
    return normalized


def normalize_build_values(values: Mapping[str, object]) -> dict[str, str]:
    if set(values) != set(BUILD_TIME_CONFIG):
        raise ConfigContractError("build config keys do not match classification")
    out: dict[str, str] = {}
    for key in sorted(BUILD_TIME_CONFIG):
        value = values[key]
        if not isinstance(value, str) or not value.strip():
            raise ConfigContractError(f"{key} must be a non-empty string")
        if key.startswith("PUBLIC_") and key.endswith("_ENABLED"):
            out[key] = _normalize_boolean(value, key=key)
        else:
            out[key] = value.strip()
    return out


def _fingerprint(domain: bytes, values: dict[str, str]) -> str:
    payload = json.dumps(
        {key: values[key] for key in sorted(values)},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(domain + payload).hexdigest()


def runtime_config_fingerprint(values: Mapping[str, object]) -> str:
    return _fingerprint(FINGERPRINT_DOMAIN, normalize_runtime_values(values))


def build_config_fingerprint(values: Mapping[str, object]) -> str:
    return _fingerprint(BUILD_FINGERPRINT_DOMAIN, normalize_build_values(values))


def build_values_from_env(env: dict[str, str]) -> dict[str, str]:
    missing = sorted(set(BUILD_TIME_CONFIG) - set(env))
    if missing:
        raise ConfigContractError(
            "missing classified build-time key(s): " + ", ".join(missing)
        )
    return normalize_build_values({key: env[key] for key in BUILD_TIME_CONFIG})


def service_environment(compose: dict[str, Any], service: str) -> dict[str, str]:
    services = compose.get("services")
    if not isinstance(services, dict) or not isinstance(services.get(service), dict):
        raise ConfigContractError(f"rendered compose missing service {service}")
    raw = services[service].get("environment")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigContractError(
            f"rendered compose service {service} environment is not an object"
        )
    out: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, (str, int, float, bool)):
            raise ConfigContractError(
                f"rendered compose environment is malformed for {service}"
            )
        if isinstance(value, bool):
            out[key] = "true" if value else "false"
        else:
            out[key] = str(value)
    return out


def runtime_values_from_compose(compose: dict[str, Any]) -> dict[str, str]:
    values: dict[str, object] = {}
    services = compose.get("services")
    if not isinstance(services, dict):
        raise ConfigContractError("rendered compose services must be an object")
    for key, spec in RUNTIME_CONFIG_ALLOWLIST.items():
        receivers: list[str] = []
        observed: set[str] = set()
        for service in services:
            env = service_environment(compose, str(service))
            if key in env:
                receivers.append(str(service))
                observed.add(env[key])
        if tuple(sorted(receivers)) != tuple(sorted(spec.services)):
            raise ConfigContractError(
                f"{key} receiver drift: {sorted(receivers)} != {sorted(spec.services)}"
            )
        if len(observed) != 1:
            raise ConfigContractError(f"{key} has inconsistent rendered values")
        values[key] = observed.pop()
    return normalize_runtime_values(values)


def changed_runtime_keys(
    previous: Mapping[str, object], target: Mapping[str, object]
) -> tuple[str, ...]:
    left = normalize_runtime_values(previous)
    right = normalize_runtime_values(target)
    return tuple(key for key in sorted(left) if left[key] != right[key])


def affected_services(changed_keys: tuple[str, ...]) -> tuple[str, ...]:
    unknown = sorted(set(changed_keys) - set(RUNTIME_CONFIG_ALLOWLIST))
    if unknown:
        raise ConfigContractError(
            "unknown runtime config key(s): " + ", ".join(unknown)
        )
    services = {
        service
        for key in changed_keys
        for service in RUNTIME_CONFIG_ALLOWLIST[key].services
    }
    return tuple(sorted(services))
