"""Sanitized diagnostic evidence for the disposable media release matrix.

Pure helpers stay free of Docker so offline unit tests can cover loss of
``RolloutResult.messages``, authority checks, and cleanup ordering without a
native host. Docker collection is injected by the harness.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .diagnostics import format_rollout_failure_diagnostics
from .redact import redact

MATRIX_CASE_NAMES: tuple[str, ...] = (
    "source-only-off",
    "off-to-on-rollback",
    "same-source-activation",
    "active-idempotency",
    "on-to-on-rollback",
    "post-current-publication-rollback",
    "on-to-on-commit",
    "accept-target-media-health",
    "accept-target-rejects-stopped-media",
    # F1–F4 corrective native cases (2026-10-14).
    "f1a-bootstrap-media-on-partial-fail-cleanup",
    "f1b-bootstrap-media-on-cleanup-failure-unresolved",
    "f2-disk-fast-growth-fail",
    "f2-disk-fast-tree-fail",
    "f2-disk-staged-inputs-success",
    "f2-disk-prestart-refuse",
    "f2-disk-long-writer-kill-reap",
    "f2-disk-neighbor-continues",
    "f3-config-rollout-refuses-media-on",
    "f4-invalid-on-budget-health-fail",
)

_SAFE_MEDIA_KEYS = frozenset(
    {"activation", "release_mode", "revision", "updated_at_utc", "slice_unit"}
)


@dataclass(frozen=True)
class RolloutLike:
    """Minimal rollout result surface for offline tests and the harness."""

    ok: bool
    already_active: bool
    messages: tuple[str, ...]
    deployment_id: str | None = None
    status: str | None = None


def redact_messages(messages: Sequence[str]) -> list[str]:
    out: list[str] = []
    for message in messages:
        try:
            out.append(redact(str(message)))
        except Exception:  # noqa: BLE001
            out.append("<redacted>")
    return out


def rollout_result_snapshot(result: RolloutLike | Any) -> dict[str, Any]:
    """Safe fields from a rollout result; never raises."""
    try:
        return {
            "ok": bool(getattr(result, "ok", False)),
            "already_active": bool(getattr(result, "already_active", False)),
            "status": getattr(result, "status", None),
            "deployment_id": getattr(result, "deployment_id", None),
            "messages": redact_messages(tuple(getattr(result, "messages", ()) or ())),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "already_active": False,
            "status": None,
            "deployment_id": None,
            "messages": (),
            "snapshot_error": type(exc).__name__,
        }


def prepare_result_snapshot(
    *,
    requested_revision: str,
    env_revision: str | None,
    result: Any,
) -> dict[str, Any]:
    """Sanitized prepare evidence retained before assertion/cleanup."""
    try:
        release_dir = getattr(result, "release_dir", None)
        release_name = None
        if release_dir is not None:
            release_name = Path(release_dir).name
        return {
            "stage": "prepare",
            "ok": bool(getattr(result, "ok", False)),
            "already_prepared": bool(getattr(result, "already_prepared", False)),
            "requested_revision": str(requested_revision),
            "env_revision": None if env_revision is None else str(env_revision),
            "messages": redact_messages(tuple(getattr(result, "messages", ()) or ())),
            "release_dir_name": release_name,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "stage": "prepare",
            "ok": False,
            "already_prepared": False,
            "requested_revision": str(requested_revision),
            "env_revision": None if env_revision is None else str(env_revision),
            "messages": (),
            "release_dir_name": None,
            "snapshot_error": type(exc).__name__,
        }


def media_authority_snapshot(
    state: Any | None, *, present: bool | None = None
) -> dict[str, Any]:
    """Safe media authority fields only."""
    if present is False or state is None:
        return {"exists": False}
    try:
        payload = {
            "exists": True,
            "activation": bool(getattr(state, "activation")),
            "release_mode": str(getattr(state, "release_mode")),
            "revision": str(getattr(state, "revision")),
            "updated_at_utc": str(getattr(state, "updated_at_utc", "")),
            "slice_unit": str(getattr(state, "slice_unit", "")),
        }
        return {key: payload[key] for key in ("exists", *_SAFE_MEDIA_KEYS)}
    except Exception as exc:  # noqa: BLE001
        return {"exists": True, "snapshot_error": type(exc).__name__}


def current_authority_snapshot(
    *,
    exists: bool,
    revision: str | None = None,
    deployment_id: str | None = None,
    schema_version: int | None = None,
) -> dict[str, Any]:
    if not exists:
        return {"exists": False}
    return {
        "exists": True,
        "revision": revision,
        "deployment_id": deployment_id,
        "schema_version": schema_version,
    }


def safe_load_media_authority(
    loader: Callable[[], Any | None],
) -> dict[str, Any]:
    """Load media authority without replacing a primary failure."""
    try:
        state = loader()
    except Exception as exc:  # noqa: BLE001
        return {
            "exists": None,
            "load_error": type(exc).__name__,
            "load_detail": redact(str(exc)),
        }
    return media_authority_snapshot(state)


def safe_load_current_authority(
    loader: Callable[[], Mapping[str, Any] | None],
) -> dict[str, Any]:
    try:
        raw = loader()
    except Exception as exc:  # noqa: BLE001
        return {
            "exists": None,
            "load_error": type(exc).__name__,
            "load_detail": redact(str(exc)),
        }
    if raw is None:
        return current_authority_snapshot(exists=False)
    try:
        application = raw.get("application") if isinstance(raw, Mapping) else None
        if isinstance(application, Mapping):
            return current_authority_snapshot(
                exists=True,
                revision=str(application.get("revision") or "") or None,
                deployment_id=str(application.get("deployment_id") or "") or None,
                schema_version=int(raw["schema_version"])
                if "schema_version" in raw
                else None,
            )
        return current_authority_snapshot(
            exists=True,
            revision=str(raw.get("revision") or "") or None,
            deployment_id=str(raw.get("deployment_id") or "") or None,
            schema_version=int(raw["schema_version"])
            if "schema_version" in raw
            else None,
        )
    except Exception as exc:  # noqa: BLE001
        return {"exists": True, "snapshot_error": type(exc).__name__}


def evaluate_source_only_off(
    result: RolloutLike | Any,
    *,
    media: Mapping[str, Any],
) -> dict[str, Any]:
    """Separate rollout success from flags-OFF / authority checks."""
    rollout = rollout_result_snapshot(result)
    authority_present = media.get("exists") is True
    activation = media.get("activation")
    flags_off_ok = authority_present and activation is False
    authority_ok = authority_present
    failures: list[str] = []
    if not rollout["ok"]:
        failures.append("rollout_failed")
    if media.get("exists") is False:
        failures.append("media_authority_missing")
    elif media.get("exists") is None:
        failures.append("media_authority_unreadable")
    elif activation is not False:
        failures.append("media_activation_not_off")
    ok = bool(rollout["ok"] and flags_off_ok)
    return {
        "stage": "source-only-off",
        "ok": ok,
        "rollout": rollout,
        "media_authority": dict(media),
        "checks": {
            "rollout_ok": bool(rollout["ok"]),
            "media_authority_present": authority_ok,
            "flags_off": flags_off_ok,
        },
        "failures": failures,
    }


def merge_primary_and_diagnostic_errors(
    *,
    primary: Mapping[str, Any],
    diagnostic_error: str | None,
) -> dict[str, Any]:
    """Keep the original rollout failure when diagnostics themselves fail."""
    merged = dict(primary)
    if diagnostic_error:
        merged["diagnostic_error"] = diagnostic_error
        # Never flip a failed primary to ok because of diagnostic noise.
        if not merged.get("ok", False):
            merged["ok"] = False
    return merged


def bounded_container_rows(
    inspect_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Keep only service / image / running / exit / health — never Env."""
    out: list[dict[str, Any]] = []
    for row in inspect_rows:
        try:
            labels = (row.get("Config") or {}).get("Labels") or {}
            state = row.get("State") or {}
            health = state.get("Health") or {}
            out.append(
                {
                    "service": labels.get("com.docker.compose.service"),
                    "image_id": row.get("Image"),
                    "running": bool(state.get("Running")),
                    "exit_code": state.get("ExitCode"),
                    "health": health.get("Status"),
                }
            )
        except Exception:  # noqa: BLE001
            out.append({"service": None, "snapshot_error": "inspect_row"})
    return out


def sanitized_deployment_journal(
    *,
    deploy_root: Path,
    deployment_id: str | None,
) -> dict[str, Any]:
    """Bounded journal extract: phases, result status, redacted messages."""
    if not deployment_id:
        return {"deployment_id": None}
    dep_dir = deploy_root / "deployments" / deployment_id
    payload: dict[str, Any] = {
        "deployment_id": deployment_id,
        "exists": dep_dir.is_dir(),
    }
    try:
        payload["diagnostics_text"] = format_rollout_failure_diagnostics(
            status=None,
            messages=(),
            deployment_id=deployment_id,
            deploy_root=deploy_root,
        )
    except Exception as exc:  # noqa: BLE001
        payload["diagnostics_text_error"] = type(exc).__name__
    result_path = dep_dir / "result.json"
    if result_path.is_file():
        try:
            raw = json.loads(result_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                payload["result"] = {
                    "status": raw.get("status"),
                    "messages": redact_messages(
                        tuple(raw.get("messages") or ())
                        if isinstance(raw.get("messages"), list)
                        else ()
                    ),
                }
        except Exception as exc:  # noqa: BLE001
            payload["result_error"] = type(exc).__name__
    events_dir = dep_dir / "events"
    if events_dir.is_dir():
        phases: list[str] = []
        for path in sorted(events_dir.glob("*.json")):
            name = path.name
            if "_" in name and name.endswith(".json"):
                phases.append(name.split("_", 1)[1][: -len(".json")])
        payload["phases"] = phases
        if phases:
            payload["failed_phase"] = phases[-1]
    return payload


def mark_remaining_cases_not_run(
    *,
    planned: Sequence[str],
    recorded: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Append NOT_RUN for planned cases not yet recorded (never PASS)."""
    seen = {
        str(item.get("name"))
        for item in recorded
        if item.get("name") not in {None, "execution", "scoped-cleanup"}
        and not str(item.get("name", "")).startswith("prepare-")
    }
    extras: list[dict[str, Any]] = []
    for name in planned:
        if name in seen:
            continue
        extras.append({"name": name, "status": "NOT_RUN"})
    return extras


def write_json_evidence(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), indent=2) + "\n", encoding="utf-8")
