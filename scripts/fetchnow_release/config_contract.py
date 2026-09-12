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
    minimum: int | None = None
    maximum: int | None = None


# Deliberately narrow. Extending this map is a reviewed release-tooling change.
RUNTIME_CONFIG_ALLOWLIST: dict[str, RuntimeConfigSpec] = {
    "PREMIUM_TEST_CHECKOUT_VISIBLE": RuntimeConfigSpec(("api",), "boolean"),
    "FREE_DOWNLOAD_QUOTA_ENABLED": RuntimeConfigSpec(("api",), "boolean"),
    "FREE_DOWNLOAD_QUOTA_READY_COMPATIBILITY_MODE": RuntimeConfigSpec(
        ("api", "delivery", "worker"), "boolean"
    ),
    "FREE_DELIVERY_RATE_LIMIT_ENABLED": RuntimeConfigSpec(
        ("api", "delivery"), "boolean"
    ),
    "FREE_DELIVERY_RATE_BYTES_PER_SECOND": RuntimeConfigSpec(
        ("api", "delivery"),
        "integer",
        minimum=262_144,
        maximum=67_108_864,
    ),
}

# Schema-1 runtime-config.json used this exact reviewed set. Keep its parser and
# fingerprint path frozen so existing state/evidence remains readable without
# silently treating absent B3 keys as values.
LEGACY_RUNTIME_CONFIG_SCHEMA1: dict[str, RuntimeConfigSpec] = {
    "FREE_DOWNLOAD_QUOTA_ENABLED": RuntimeConfigSpec(("api",), "boolean"),
}

# Schema 2 added the delivery-rate pair. Keep its exact parser/fingerprint so
# an accepted deployment can be upgraded to the current schema fail-closed.
LEGACY_RUNTIME_CONFIG_SCHEMA2: dict[str, RuntimeConfigSpec] = {
    "FREE_DOWNLOAD_QUOTA_ENABLED": RuntimeConfigSpec(("api",), "boolean"),
    "FREE_DELIVERY_RATE_LIMIT_ENABLED": RuntimeConfigSpec(
        ("api", "delivery"), "boolean"
    ),
    "FREE_DELIVERY_RATE_BYTES_PER_SECOND": RuntimeConfigSpec(
        ("api", "delivery"), "integer", minimum=262_144, maximum=67_108_864
    ),
}

# Schema 3 added checkout visibility. Keep it frozen when schema 4 adds the
# successful-delivery compatibility switch.
LEGACY_RUNTIME_CONFIG_SCHEMA3: dict[str, RuntimeConfigSpec] = {
    "PREMIUM_TEST_CHECKOUT_VISIBLE": RuntimeConfigSpec(("api",), "boolean"),
    "FREE_DOWNLOAD_QUOTA_ENABLED": RuntimeConfigSpec(("api",), "boolean"),
    "FREE_DELIVERY_RATE_LIMIT_ENABLED": RuntimeConfigSpec(
        ("api", "delivery"), "boolean"
    ),
    "FREE_DELIVERY_RATE_BYTES_PER_SECOND": RuntimeConfigSpec(
        ("api", "delivery"), "integer", minimum=262_144, maximum=67_108_864
    ),
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
    "PREMIUM_TEST_CHECKOUT_VISIBLE": ("api",),
    "FREE_DOWNLOAD_QUOTA_ENABLED": ("api",),
    "FREE_DOWNLOAD_QUOTA_READY_COMPATIBILITY_MODE": (
        "api",
        "delivery",
        "worker",
    ),
    "FREE_DELIVERY_RATE_LIMIT_ENABLED": ("api", "delivery"),
    "FREE_DELIVERY_RATE_BYTES_PER_SECOND": ("api", "delivery"),
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


def _normalize_integer(
    value: object,
    *,
    key: str,
    minimum: int | None,
    maximum: int | None,
) -> str:
    if not isinstance(value, str):
        raise ConfigContractError(f"{key} must be a string integer")
    raw = value.strip()
    if not raw or not raw.isascii() or not raw.isdigit():
        raise ConfigContractError(f"{key} must be an unsigned decimal integer")
    if len(raw) > 1 and raw.startswith("0"):
        raise ConfigContractError(f"{key} must use canonical decimal form")
    parsed = int(raw)
    if minimum is not None and parsed < minimum:
        raise ConfigContractError(f"{key} is below the reviewed minimum")
    if maximum is not None and parsed > maximum:
        raise ConfigContractError(f"{key} exceeds the reviewed maximum")
    return str(parsed)


def _normalize_runtime_values_for_specs(
    values: Mapping[str, object],
    *,
    specs: Mapping[str, RuntimeConfigSpec],
) -> dict[str, str]:
    if set(values) != set(specs):
        raise ConfigContractError("runtime config keys do not match reviewed allowlist")
    normalized: dict[str, str] = {}
    for key in sorted(specs):
        spec = specs[key]
        if spec.kind == "boolean":
            normalized[key] = _normalize_boolean(values[key], key=key)
        elif spec.kind == "integer":
            normalized[key] = _normalize_integer(
                values[key],
                key=key,
                minimum=spec.minimum,
                maximum=spec.maximum,
            )
        else:  # pragma: no cover - fail closed if a future kind lacks a parser
            raise ConfigContractError(f"unsupported config kind for {key}")
    return normalized


def normalize_runtime_values(values: Mapping[str, object]) -> dict[str, str]:
    return _normalize_runtime_values_for_specs(
        values,
        specs=RUNTIME_CONFIG_ALLOWLIST,
    )


def normalize_legacy_runtime_values_schema1(
    values: Mapping[str, object],
) -> dict[str, str]:
    return _normalize_runtime_values_for_specs(
        values,
        specs=LEGACY_RUNTIME_CONFIG_SCHEMA1,
    )


def normalize_legacy_runtime_values_schema2(
    values: Mapping[str, object],
) -> dict[str, str]:
    return _normalize_runtime_values_for_specs(
        values,
        specs=LEGACY_RUNTIME_CONFIG_SCHEMA2,
    )


def normalize_legacy_runtime_values_schema3(
    values: Mapping[str, object],
) -> dict[str, str]:
    return _normalize_runtime_values_for_specs(
        values,
        specs=LEGACY_RUNTIME_CONFIG_SCHEMA3,
    )


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


def legacy_runtime_config_fingerprint_schema1(
    values: Mapping[str, object],
) -> str:
    return _fingerprint(
        FINGERPRINT_DOMAIN,
        normalize_legacy_runtime_values_schema1(values),
    )


def legacy_runtime_config_fingerprint_schema2(
    values: Mapping[str, object],
) -> str:
    return _fingerprint(
        FINGERPRINT_DOMAIN,
        normalize_legacy_runtime_values_schema2(values),
    )


def legacy_runtime_config_fingerprint_schema3(
    values: Mapping[str, object],
) -> str:
    return _fingerprint(
        FINGERPRINT_DOMAIN,
        normalize_legacy_runtime_values_schema3(values),
    )


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
