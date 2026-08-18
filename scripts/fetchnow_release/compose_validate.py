"""Validate rendered Compose JSON for staging release identity / ports."""

from __future__ import annotations

from typing import Any

from . import (
    DEFAULT_GATEWAY_LOOPBACK,
    DEFAULT_GATEWAY_PORT,
    EXPECTED_SERVICES,
    POSTGRES_IMAGE_PREFIX,
)
from .bootstrap_project import BootstrapProjectError, assert_bootstrap_project
from .deploy_plan_project import DeployPlanProjectError, assert_deploy_plan_project
from .environment import real_identity
from .revision import validate_full_sha
from .migration_project import MigrationProjectError, assert_migration_project
from .rollout_project import RolloutProjectError, assert_rollout_project


class ComposeContractError(ValueError):
    """Rendered Compose contract failure."""


OPTIONAL_ONESHOT_SERVICES = frozenset({"storage-init"})


def _services(cfg: dict[str, Any]) -> dict[str, Any]:
    return cfg.get("services") or {}


def _published_ports(service: dict[str, Any]) -> list[dict[str, Any]]:
    return list(service.get("ports") or [])


def _service_environment(service: dict[str, Any]) -> dict[str, str]:
    raw = service.get("environment") or {}
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    if isinstance(raw, list):
        out: dict[str, str] = {}
        for item in raw:
            if isinstance(item, str) and "=" in item:
                key, value = item.split("=", 1)
                out[key] = value
        return out
    return {}


def _validate_storage_init(service: dict[str, Any], *, expected_api: str) -> None:
    """Optional PR9 oneshot: same API image, no secrets, no network, no ports."""
    image = service.get("image") or ""
    if image != expected_api:
        raise ComposeContractError(
            f"storage-init image {image!r} != {expected_api!r}"
        )
    if _published_ports(service):
        raise ComposeContractError("storage-init must not publish host ports")
    restart = str(service.get("restart") or "no")
    if restart not in {"no", "n", "false", ""}:
        raise ComposeContractError("storage-init must not restart")
    command = service.get("command")
    text = " ".join(command) if isinstance(command, list) else str(command or "")
    if "fetchnow-storage-init" not in text:
        raise ComposeContractError("storage-init command must be fetchnow-storage-init")
    if "sleep" in text.lower():
        raise ComposeContractError("storage-init command must not sleep")
    env = _service_environment(service)
    forbidden = (
        "DATABASE_URL",
        "POSTGRES_PASSWORD",
        "POSTGRES_USER",
        "POSTGRES_DB",
        "MEDIA_INSPECTION_YTDLP_PATH",
        "MEDIA_MUXING_FFMPEG_PATH",
        "MEDIA_MUXING_FFPROBE_PATH",
    )
    leaked = [key for key in forbidden if key in env]
    if leaked:
        raise ComposeContractError(
            f"storage-init must not receive {', '.join(leaked)}"
        )
    network_mode = str(service.get("network_mode") or "")
    networks = service.get("networks")
    if network_mode != "none":
        raise ComposeContractError("storage-init must use network_mode none")
    if networks:
        raise ComposeContractError("storage-init must not join compose networks")
    user = str(service.get("user") or "")
    if not user or user in {"0", "root", "0:0"}:
        raise ComposeContractError("storage-init must not run as root")


def validate_staging_rendered(
    cfg: dict[str, Any],
    *,
    expected_revision: str,
    expected_project: str,
) -> None:
    rev = validate_full_sha(expected_revision)
    if cfg.get("name") != expected_project:
        raise ComposeContractError(
            f"rendered project name {cfg.get('name')!r} != {expected_project!r}"
        )
    services = _services(cfg)
    missing = [s for s in EXPECTED_SERVICES if s not in services]
    if missing:
        raise ComposeContractError(f"missing services: {', '.join(missing)}")
    unknown = sorted(set(services) - set(EXPECTED_SERVICES) - OPTIONAL_ONESHOT_SERVICES)
    if unknown:
        raise ComposeContractError(
            f"unknown services in rendered compose: {', '.join(unknown)}"
        )

    if "storage-init" in services:
        _validate_storage_init(
            services["storage-init"], expected_api=f"fetchnow-api:{rev}"
        )

    for name in ("api", "worker", "delivery", "web", "postgres"):
        if _published_ports(services[name]):
            raise ComposeContractError(f"{name} must not publish host ports")

    gateway_ports = _published_ports(services["gateway"])
    if len(gateway_ports) != 1:
        raise ComposeContractError("gateway must publish exactly one host port")
    gp = gateway_ports[0]
    host_ip = gp.get("host_ip") or ""
    published = str(gp.get("published") or "")
    if host_ip and host_ip != DEFAULT_GATEWAY_LOOPBACK:
        raise ComposeContractError(
            f"gateway must bind loopback, got host_ip={host_ip!r}"
        )
    expected_gateway_port = DEFAULT_GATEWAY_PORT
    identity = real_identity(expected_project)
    if identity is None:
        validated = False
        for checker in (
            assert_rollout_project,
            assert_deploy_plan_project,
            assert_migration_project,
            assert_bootstrap_project,
        ):
            try:
                checker(expected_project)
                validated = True
                break
            except (
                RolloutProjectError,
                DeployPlanProjectError,
                MigrationProjectError,
                BootstrapProjectError,
            ):
                continue
        if not validated:
            raise ComposeContractError(
                f"refusing unsafe project name {expected_project!r}; "
                "expected fetchnow-rollout-test-*, fetchnow-deploy-plan-test-*, "
                "fetchnow-migration-test-*, or fetchnow-bootstrap-test-*"
            )
        expected_gateway_port = int(published)
    else:
        for svc_name in ("api", "worker", "delivery"):
            rendered_env = _service_environment(services[svc_name])
            if rendered_env.get("APP_ENV") != identity.app_env:
                raise ComposeContractError(
                    f"{svc_name} APP_ENV must be {identity.app_env!r} for "
                    f"{identity.project}, got {rendered_env.get('APP_ENV')!r}"
                )
    if published not in {
        str(expected_gateway_port),
        f"{DEFAULT_GATEWAY_LOOPBACK}:{expected_gateway_port}",
    }:
        # Compose may split host_ip and published.
        if not (
            host_ip == DEFAULT_GATEWAY_LOOPBACK
            and published == str(expected_gateway_port)
        ):
            raise ComposeContractError(
                f"gateway must publish {DEFAULT_GATEWAY_LOOPBACK}:{expected_gateway_port}, got {gp!r}"
            )
    if int(gp.get("target") or 0) != 8080:
        raise ComposeContractError("gateway target port must be 8080")

    api_image = services["api"].get("image") or ""
    worker_image = services["worker"].get("image") or ""
    delivery_image = services["delivery"].get("image") or ""
    web_image = services["web"].get("image") or ""
    gateway_image = services["gateway"].get("image") or ""
    postgres_image = services["postgres"].get("image") or ""

    expected_api = f"fetchnow-api:{rev}"
    expected_web = f"fetchnow-web:{rev}"
    expected_gateway = f"fetchnow-gateway:{rev}"
    if api_image != expected_api:
        raise ComposeContractError(f"api image {api_image!r} != {expected_api!r}")
    if worker_image != expected_api:
        raise ComposeContractError(f"worker image {worker_image!r} != {expected_api!r}")
    if delivery_image != expected_api:
        raise ComposeContractError(
            f"delivery image {delivery_image!r} != {expected_api!r}"
        )
    if web_image != expected_web:
        raise ComposeContractError(f"web image {web_image!r} != {expected_web!r}")
    if gateway_image != expected_gateway:
        raise ComposeContractError(
            f"gateway image {gateway_image!r} != {expected_gateway!r}"
        )
    if not str(postgres_image).startswith(POSTGRES_IMAGE_PREFIX):
        raise ComposeContractError(
            f"postgres image must remain pinned {POSTGRES_IMAGE_PREFIX}-*, got {postgres_image!r}"
        )

    # No bind mounts / reload commands.
    banned = ("reload", "watchfiles", "uvicorn --reload", "astro dev", "npm run dev")
    for name, svc in services.items():
        for mount in svc.get("volumes") or []:
            if isinstance(mount, dict) and mount.get("type") == "bind":
                raise ComposeContractError(f"{name} must not use bind mounts")
            if isinstance(mount, str) and (
                mount.startswith(".") or mount.startswith("/")
            ):
                # Named volumes look like "pgdata:/path" — allow volume names without slash in source.
                source = mount.split(":", 1)[0]
                if source.startswith(".") or source.startswith("/"):
                    raise ComposeContractError(
                        f"{name} bind mount not allowed: {mount!r}"
                    )
        command = svc.get("command")
        text = " ".join(command) if isinstance(command, list) else str(command or "")
        lower = text.lower()
        for token in banned:
            if token in lower:
                raise ComposeContractError(
                    f"{name} command looks like dev reload: {text!r}"
                )

    vols = cfg.get("volumes") or {}
    for key in ("pgdata", "tmp"):
        if key not in vols:
            raise ComposeContractError(f"missing volume key {key}")
        spec = vols[key]
        if isinstance(spec, dict):
            rendered = spec.get("name") or ""
            if not str(rendered).startswith(f"{expected_project}_"):
                raise ComposeContractError(
                    f"volume {key} must be project-scoped, got {rendered!r}"
                )
