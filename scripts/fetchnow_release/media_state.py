"""Durable non-secret media activation authority beside current.json."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .c3_constants import STATE_DIRNAME
from .journal_io import atomic_write_json, read_json
from .release_mode import RELEASE_MODE_CANONICAL, RELEASE_MODE_MEDIA, parse_release_mode
from .resource_budget import ResourceBudgetError, parse_resource_budget
from .revision import validate_full_sha

MEDIA_STATE_NAME = "media-activation.json"
MEDIA_STATE_SCHEMA = 1
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_EXECUTOR_SETTING_KEYS = frozenset(
    {
        "MEDIA_EXECUTOR_ENABLED",
        "MEDIA_EXECUTOR_SOCKET",
        "MEDIA_EXECUTOR_WORK_ROOT",
        "MEDIA_NET_EXECUTOR_ENABLED",
        "MEDIA_NET_EXECUTOR_SOCKET",
        "MEDIA_NET_EXECUTOR_WORK_ROOT",
    }
)
# Never persist deny lists with host topology into this state file if they
# could include operational secrets; we store only coverage digests / flags.
_FORBIDDEN_STATE_KEYS = frozenset(
    {
        "POSTGRES_PASSWORD",
        "SECRET",
        "TOKEN",
        "PASSWORD",
        "EGRESS_DENY_CIDRS",
        "EGRESS_DENY_IPS",
    }
)


class MediaStateError(ValueError):
    """Invalid media-activation state document."""


@dataclass(frozen=True)
class MediaActivationState:
    schema_version: int
    activation: bool
    release_mode: str
    revision: str
    media_image_ids: dict[str, str]
    overlays: tuple[str, ...]
    profiles: tuple[str, ...]
    executor_settings: dict[str, str]
    updated_at_utc: str
    resource_budget: dict[str, Any] | None = None
    slice_unit: str = "fetchnow-media-executor.slice"
    denied_host: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "activation": self.activation,
            "release_mode": self.release_mode,
            "revision": self.revision,
            "media_image_ids": dict(self.media_image_ids),
            "overlays": list(self.overlays),
            "profiles": list(self.profiles),
            "executor_settings": dict(self.executor_settings),
            "updated_at_utc": self.updated_at_utc,
            "resource_budget": self.resource_budget,
            "slice_unit": self.slice_unit,
            "denied_host": self.denied_host,
        }


def media_state_path(deploy_root: Path) -> Path:
    return deploy_root / str(STATE_DIRNAME) / MEDIA_STATE_NAME


def validate_executor_settings(settings: dict[str, str]) -> dict[str, str]:
    if set(settings) - _EXECUTOR_SETTING_KEYS:
        raise MediaStateError(
            f"executor_settings has unknown keys: "
            f"{sorted(set(settings) - _EXECUTOR_SETTING_KEYS)}"
        )
    for key, value in settings.items():
        if not isinstance(value, str):
            raise MediaStateError(f"{key} must be a string")
        upper = key.upper()
        for banned in _FORBIDDEN_STATE_KEYS:
            if banned in upper or banned in value.upper():
                raise MediaStateError("refusing secret-like media state content")
        if key.endswith("_ENABLED"):
            if value not in {"true", "false"}:
                raise MediaStateError(f"{key} must be true or false")
        elif (
            key.endswith(("_SOCKET", "_WORK_ROOT"))
            and value
            and not value.startswith("/")
        ):
            raise MediaStateError(f"{key} must be absolute when set")
    return dict(settings)


def parse_media_activation_state(raw: object) -> MediaActivationState:
    if not isinstance(raw, dict):
        raise MediaStateError("media state must be an object")
    try:
        schema = int(raw["schema_version"])
        activation = bool(raw["activation"])
        if type(raw["activation"]) is not bool:
            raise MediaStateError("activation must be boolean")
        release_mode = parse_release_mode(
            raw["release_mode"],
            source_contract_version=4,
            required=True,
        )
        revision = validate_full_sha(str(raw["revision"]))
        images_raw = raw["media_image_ids"]
        overlays = tuple(str(x) for x in raw["overlays"])
        profiles = tuple(str(x) for x in raw["profiles"])
        settings = validate_executor_settings(
            {str(k): str(v) for k, v in raw["executor_settings"].items()}
        )
        updated = str(raw["updated_at_utc"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MediaStateError(f"media state invalid: {exc}") from exc
    if schema != MEDIA_STATE_SCHEMA:
        raise MediaStateError(f"unsupported media state schema: {schema}")
    if not isinstance(images_raw, dict):
        raise MediaStateError("media_image_ids must be object")
    images: dict[str, str] = {}
    for key, value in images_raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise MediaStateError("media_image_ids must be string map")
        if not _IMAGE_ID_RE.fullmatch(value):
            raise MediaStateError(f"malformed media image id for {key}")
        images[key] = value
    budget_raw = raw.get("resource_budget")
    if activation:
        if release_mode != RELEASE_MODE_MEDIA:
            raise MediaStateError("activation requires release_mode=media")
        required = {"media-executor", "media-net-executor", "egress-proxy"}
        if set(images) != required:
            raise MediaStateError(
                f"media_image_ids keys {sorted(images)} != {sorted(required)}"
            )
        if settings.get("MEDIA_EXECUTOR_ENABLED") != "true":
            raise MediaStateError("activation requires MEDIA_EXECUTOR_ENABLED=true")
        if settings.get("MEDIA_NET_EXECUTOR_ENABLED") != "true":
            raise MediaStateError("activation requires MEDIA_NET_EXECUTOR_ENABLED=true")
        if budget_raw is None:
            raise MediaStateError("activation requires resource_budget")
        try:
            budget = parse_resource_budget(budget_raw)
        except ResourceBudgetError as exc:
            raise MediaStateError(
                f"activation resource_budget invalid: {exc}"
            ) from exc
        budget_stored: dict[str, Any] | None = budget.to_dict()
    else:
        if release_mode not in {RELEASE_MODE_CANONICAL, RELEASE_MODE_MEDIA}:
            raise MediaStateError("invalid release_mode for inactive state")
        if settings.get("MEDIA_EXECUTOR_ENABLED", "false") != "false":
            raise MediaStateError("inactive state must keep executor flags false")
        if settings.get("MEDIA_NET_EXECUTOR_ENABLED", "false") != "false":
            raise MediaStateError("inactive state must keep net executor flags false")
        # OFF/legacy may omit budget; if present it must still parse.
        if budget_raw is None:
            budget_stored = None
        else:
            try:
                budget_stored = parse_resource_budget(budget_raw).to_dict()
            except ResourceBudgetError as exc:
                raise MediaStateError(
                    f"inactive resource_budget invalid: {exc}"
                ) from exc
    return MediaActivationState(
        schema_version=schema,
        activation=activation,
        release_mode=release_mode,
        revision=revision,
        media_image_ids=images,
        overlays=overlays,
        profiles=profiles,
        executor_settings=settings,
        updated_at_utc=updated,
        resource_budget=budget_stored,
        slice_unit=str(raw.get("slice_unit") or "fetchnow-media-executor.slice"),
        denied_host=str(raw.get("denied_host") or ""),
    )


def load_media_activation_state(deploy_root: Path) -> MediaActivationState | None:
    path = media_state_path(deploy_root)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise MediaStateError(f"unsafe media state path: {path}")
    return parse_media_activation_state(read_json(path))


def write_media_activation_state(
    deploy_root: Path, state: MediaActivationState
) -> Path:
    path = media_state_path(deploy_root)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parsed = parse_media_activation_state(state.to_dict())
    atomic_write_json(path, parsed.to_dict(), mode=0o600)
    return path


def inactive_media_state(*, revision: str, updated_at_utc: str) -> MediaActivationState:
    return MediaActivationState(
        schema_version=MEDIA_STATE_SCHEMA,
        activation=False,
        release_mode=RELEASE_MODE_CANONICAL,
        revision=revision,
        media_image_ids={},
        overlays=(),
        profiles=(),
        executor_settings={
            "MEDIA_EXECUTOR_ENABLED": "false",
            "MEDIA_NET_EXECUTOR_ENABLED": "false",
        },
        updated_at_utc=updated_at_utc,
    )
