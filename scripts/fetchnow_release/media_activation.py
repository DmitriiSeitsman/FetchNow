"""Explicit media activation plan helpers (overlays, profiles, settings)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .c2_constants import SOURCE_DIRNAME
from .journal_io import atomic_write_text
from .manifest import ReleaseManifest
from .media_state import validate_executor_settings
from .release_mode import (
    MEDIA_BUILD_SERVICES,
    RELEASE_MODE_MEDIA,
    ReleaseModeError,
    parse_release_mode,
)
from .resource_budget import parse_resource_budget

MEDIA_OVERLAYS = (
    "compose.media-executor.yaml",
    "compose.media-net-executor.yaml",
)
MEDIA_PROFILES = ("media-executor", "media-net-executor")
# Start order: proxy first, then offline executor, then net executor.
# Worker is never started in this phase (avoids READY with flags ON early).
MEDIA_SIDECAR_START_ORDER = (
    "egress-proxy",
    "media-executor",
    "media-net-executor",
)

DEFAULT_EXECUTOR_SOCKET = "/run/fetchnow-executor/ctrl/worker.sock"
DEFAULT_EXECUTOR_WORK_ROOT = "/var/lib/fetchnow/executor"
DEFAULT_NET_SOCKET = "/run/fetchnow-media-net/ctrl/worker.sock"
DEFAULT_NET_WORK_ROOT = "/var/lib/fetchnow/media-net"


class MediaActivationError(ValueError):
    """Invalid media activation request."""


@dataclass(frozen=True)
class MediaActivationPlan:
    activation: bool
    release_mode: str
    overlays: tuple[str, ...]
    profiles: tuple[str, ...]
    media_image_ids: dict[str, str]
    executor_settings: dict[str, str]
    required_deny_cidrs: tuple[str, ...]
    required_deny_ips: tuple[str, ...]
    resource_budget: dict[str, object] | None = None
    slice_unit: str = "fetchnow-media-executor.slice"
    denied_host: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "activation": self.activation,
            "release_mode": self.release_mode,
            "overlays": list(self.overlays),
            "profiles": list(self.profiles),
            "media_image_ids": dict(self.media_image_ids),
            "executor_settings": dict(self.executor_settings),
            "required_deny_cidrs": list(self.required_deny_cidrs),
            "required_deny_ips": list(self.required_deny_ips),
            "resource_budget": self.resource_budget,
            "slice_unit": self.slice_unit,
            "denied_host": self.denied_host,
        }


def media_image_ids_from_manifest(manifest: ReleaseManifest) -> dict[str, str]:
    by_svc = {img.service: img.image_id for img in manifest.images}
    missing = [svc for svc in MEDIA_BUILD_SERVICES if svc not in by_svc]
    if missing:
        raise MediaActivationError(
            f"manifest missing media images: {missing}; prepare with release_mode=media"
        )
    return {svc: by_svc[svc] for svc in MEDIA_BUILD_SERVICES}


def build_source_only_plan(*, release_mode: str) -> MediaActivationPlan:
    mode = parse_release_mode(release_mode, source_contract_version=4, required=True)
    return MediaActivationPlan(
        activation=False,
        release_mode=mode,
        overlays=(),
        profiles=(),
        media_image_ids={},
        executor_settings={
            "MEDIA_EXECUTOR_ENABLED": "false",
            "MEDIA_NET_EXECUTOR_ENABLED": "false",
        },
        required_deny_cidrs=(),
        required_deny_ips=(),
    )


def build_activation_plan(
    *,
    manifest: ReleaseManifest,
    executor_socket: str = DEFAULT_EXECUTOR_SOCKET,
    executor_work_root: str = DEFAULT_EXECUTOR_WORK_ROOT,
    net_socket: str = DEFAULT_NET_SOCKET,
    net_work_root: str = DEFAULT_NET_WORK_ROOT,
    required_deny_cidrs: tuple[str, ...],
    required_deny_ips: tuple[str, ...],
) -> MediaActivationPlan:
    try:
        mode = parse_release_mode(
            manifest.release_mode,
            source_contract_version=manifest.source_contract_version,
            required=True,
        )
    except ReleaseModeError as exc:
        raise MediaActivationError(str(exc)) from exc
    if mode != RELEASE_MODE_MEDIA:
        raise MediaActivationError(
            "media activation requires a media-mode prepared release"
        )
    settings = validate_executor_settings(
        {
            "MEDIA_EXECUTOR_ENABLED": "true",
            "MEDIA_EXECUTOR_SOCKET": executor_socket,
            "MEDIA_EXECUTOR_WORK_ROOT": executor_work_root,
            "MEDIA_NET_EXECUTOR_ENABLED": "true",
            "MEDIA_NET_EXECUTOR_SOCKET": net_socket,
            "MEDIA_NET_EXECUTOR_WORK_ROOT": net_work_root,
        }
    )
    return MediaActivationPlan(
        activation=True,
        release_mode=mode,
        overlays=MEDIA_OVERLAYS,
        profiles=MEDIA_PROFILES,
        media_image_ids=media_image_ids_from_manifest(manifest),
        executor_settings=settings,
        required_deny_cidrs=tuple(required_deny_cidrs),
        required_deny_ips=tuple(required_deny_ips),
    )


def compose_files_for_media(
    release_directory: Path,
    *,
    base_compose: tuple[Path, ...],
    activation: bool,
) -> tuple[Path, ...]:
    """Extend base compose files with media overlays when activating."""
    if not activation:
        return base_compose
    source = release_directory / SOURCE_DIRNAME
    extra: list[Path] = []
    for rel in MEDIA_OVERLAYS:
        path = source / rel
        if path.is_symlink() or not path.is_file():
            raise MediaActivationError(f"media overlay missing or unsafe: {rel}")
        extra.append(path.resolve())
    # Generated overrides live outside the verified source snapshot. Keep them
    # last: otherwise the overlays' developer tags replace immutable IDs.
    source = source.resolve()
    source_files = tuple(p for p in base_compose if p.resolve().is_relative_to(source))
    overrides = tuple(p for p in base_compose if not p.resolve().is_relative_to(source))
    return tuple(dict.fromkeys((*source_files, *extra, *overrides)))


def assert_executor_flags_off(env: dict[str, str]) -> None:
    """Source-only / config paths must not silently enable executors."""
    for key in ("MEDIA_EXECUTOR_ENABLED", "MEDIA_NET_EXECUTOR_ENABLED"):
        raw = env.get(key, "false").strip().lower()
        if raw in {"", "0", "false", "no", "off"}:
            continue
        if raw in {"1", "true", "yes", "on"}:
            raise MediaActivationError(
                f"{key} is enabled but media_activation was not requested; "
                "refusing hidden enablement"
            )
        raise MediaActivationError(f"{key} has non-boolean value: {raw!r}")


def write_media_settings_override(directory: Path, plan: MediaActivationPlan) -> Path:
    """Explicit transaction settings; JSON is a YAML subset, no env mutation."""
    services: dict[str, object] = {
        "worker": {"environment": validate_executor_settings(plan.executor_settings)}
    }
    if plan.activation:
        budget = parse_resource_budget(plan.resource_budget)
        for service, memory in budget.per_container_memory_bytes.items():
            config: dict[str, object] = {
                "mem_limit": memory,
                "cgroup_parent": plan.slice_unit,
                "deploy": {"resources": {"limits": {"memory": memory}}},
            }
            if service != "egress-proxy":
                config["environment"] = {
                    "MEDIA_EXECUTOR_MAX_JOBS": str(budget.concurrency)
                }
            services[service] = config
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "media-settings.json"
    atomic_write_text(
        path, json.dumps({"services": services}, sort_keys=True) + "\n", mode=0o600
    )
    return path


def compose_for_media_plan(
    release_directory: Path,
    *,
    base_compose: tuple[Path, ...],
    plan: MediaActivationPlan,
) -> tuple[Path, ...]:
    files = compose_files_for_media(
        release_directory,
        base_compose=tuple(p for p in base_compose if p.name != "media-settings.json"),
        activation=plan.activation,
    )
    # Caller supplies a transaction-owned immutable image override last.
    if not files or files[-1].resolve().is_relative_to(
        (release_directory / SOURCE_DIRNAME).resolve()
    ):
        raise MediaActivationError(
            "media settings require a transaction-owned image override"
        )
    settings = write_media_settings_override(files[-1].parent, plan)
    return (*files[:-1], settings, files[-1])
