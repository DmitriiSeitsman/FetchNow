#!/usr/bin/env python3
"""Validate FetchNow Compose base/dev/staging contracts (PRD1A).

Uses `docker compose config --format json` — no extra Python deps.
Exits non-zero on the first failed invariant.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _run_compose(args: list[str], *, env: dict[str, str] | None = None) -> dict[str, Any]:
    cmd = ["docker", "compose", *args, "config", "--format", "json"]
    merged = os.environ.copy()
    if env:
        merged.update(env)
    # Prevent an accidental host .env from leaking into contract checks unless
    # the caller intentionally passes --env-file.
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        env=merged,
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"compose config failed ({' '.join(cmd)}):\n{proc.stderr or proc.stdout}"
        )
    return json.loads(proc.stdout)


def _services(cfg: dict[str, Any]) -> dict[str, Any]:
    return cfg.get("services") or {}


def _assert(cond: bool, message: str) -> None:
    if not cond:
        raise SystemExit(f"FAIL: {message}")


def _published_ports(service: dict[str, Any]) -> list[dict[str, Any]]:
    return list(service.get("ports") or [])


def _has_host_ports(service: dict[str, Any]) -> bool:
    return bool(_published_ports(service))


def _volume_defs(cfg: dict[str, Any]) -> dict[str, Any]:
    return cfg.get("volumes") or {}


def _check_no_container_name(cfg: dict[str, Any], label: str) -> None:
    for name, svc in _services(cfg).items():
        _assert(
            "container_name" not in svc,
            f"{label}: service {name} must not set container_name",
        )


def _check_volumes_project_scoped(cfg: dict[str, Any], label: str) -> None:
    vols = _volume_defs(cfg)
    _assert("pgdata" in vols, f"{label}: missing logical volume key pgdata")
    _assert("tmp" in vols, f"{label}: missing logical volume key tmp")
    for key, spec in vols.items():
        if not isinstance(spec, dict):
            continue
        explicit = spec.get("name")
        # Compose may echo the generated name; reject only when it bypasses
        # project scoping via an explicit custom name that ignores the project.
        # After PRD1A, volume definitions must not carry a YAML `name:` field.
        # Rendered JSON still includes a computed `name` equal to
        # `{project}_{key}`. Accept that; reject names that omit the project
        # prefix (legacy global names).
        project = cfg.get("name") or ""
        expected_prefix = f"{project}_"
        _assert(
            isinstance(explicit, str) and explicit.startswith(expected_prefix),
            f"{label}: volume {key} rendered name {explicit!r} must be "
            f"project-scoped as {expected_prefix}<key>",
        )


def _check_no_bind_mounts(cfg: dict[str, Any], label: str) -> None:
    for name, svc in _services(cfg).items():
        for mount in svc.get("volumes") or []:
            if isinstance(mount, dict):
                mtype = mount.get("type")
                _assert(
                    mtype != "bind",
                    f"{label}: service {name} must not use bind mounts",
                )
                continue
            if not isinstance(mount, str):
                continue
            source = mount.split(":", 1)[0]
            if source.startswith(".") or source.startswith("/"):
                raise SystemExit(
                    f"FAIL: {label}: service {name} bind mount {mount!r}"
                )


def _check_no_reload_command(cfg: dict[str, Any], label: str) -> None:
    banned = ("reload", "watchfiles", "uvicorn --reload", "astro dev", "npm run dev")
    for name, svc in _services(cfg).items():
        command = svc.get("command")
        text = " ".join(command) if isinstance(command, list) else str(command or "")
        lower = text.lower()
        for token in banned:
            _assert(
                token not in lower,
                f"{label}: service {name} command looks like dev reload/watch: {text!r}",
            )


def check_dev_config() -> None:
    cfg = _run_compose(
        [],
        env={"COMPOSE_PROJECT_NAME": "fetchnow"},
    )
    _assert(cfg.get("name") == "fetchnow", "dev: project name must be fetchnow")
    _check_no_container_name(cfg, "dev")
    _check_volumes_project_scoped(cfg, "dev")
    services = _services(cfg)
    for required in ("gateway", "api", "worker", "postgres", "web", "delivery", "storage-init"):
        _assert(required in services, f"dev: missing service {required}")
    # Local override publishes gateway 8080 and api 8000.
    _assert(_has_host_ports(services["gateway"]), "dev: gateway should publish a port")
    _assert(_has_host_ports(services["api"]), "dev: api debug port expected via override")
    _assert(not _has_host_ports(services["postgres"]), "dev: postgres must not publish")
    _assert(not _has_host_ports(services["worker"]), "dev: worker must not publish")
    _assert(not _has_host_ports(services["delivery"]), "dev: delivery must not publish")
    _assert(not _has_host_ports(services["web"]), "dev: web must not publish")
    _assert(
        not _has_host_ports(services["storage-init"]),
        "dev: storage-init must not publish",
    )
    _assert_storage_init_least_privilege(services["storage-init"], "dev")
    _assert(
        services["api"].get("image") == "fetchnow-api:local",
        "dev: api image must default to fetchnow-api:local",
    )
    _assert(
        services["worker"].get("image") == "fetchnow-api:local",
        "dev: worker must share fetchnow-api:local",
    )
    _assert(
        services["delivery"].get("image") == "fetchnow-api:local",
        "dev: delivery must share fetchnow-api:local",
    )
    _assert(
        services["web"].get("image") == "fetchnow-web:local",
        "dev: web image must default to fetchnow-web:local",
    )
    _assert(
        services["gateway"].get("image") == "fetchnow-gateway:local",
        "dev: gateway image must default to fetchnow-gateway:local",
    )
    print("OK: development compose contract")


def check_base_only_config() -> None:
    cfg = _run_compose(
        ["-f", "compose.yaml"],
        env={"COMPOSE_PROJECT_NAME": "fetchnow"},
    )
    _assert(cfg.get("name") == "fetchnow", "base: project name must be fetchnow")
    _check_no_container_name(cfg, "base")
    _check_volumes_project_scoped(cfg, "base")
    for name, svc in _services(cfg).items():
        _assert(
            not _has_host_ports(svc),
            f"base: service {name} must not publish host ports",
        )
    _assert_storage_init_least_privilege(_services(cfg)["storage-init"], "base")
    print("OK: base compose contract (no host ports)")


def check_staging_config() -> None:
    example = ROOT / ".env.staging.example"
    _assert(example.is_file(), ".env.staging.example must exist")
    revision = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / ".env.staging"
        # Safe local test values derived from the example contract.
        text = example.read_text(encoding="utf-8")
        text = text.replace(
            "replace-with-32plus-url-safe-staging-password",
            "staging-contract-check-password-xxxxxxxx",
        )
        text = text.replace(
            "0000000000000000000000000000000000000000",
            revision,
        )
        env_path.write_text(text, encoding="utf-8")

        cfg = _run_compose(
            [
                "--env-file",
                str(env_path),
                "--project-name",
                "fetchnow-staging",
                "-f",
                "compose.yaml",
                "-f",
                "compose.staging.yaml",
            ],
            env={"COMPOSE_PROJECT_NAME": "fetchnow-staging"},
        )

    _assert(cfg.get("name") == "fetchnow-staging", "staging: wrong project name")
    _check_no_container_name(cfg, "staging")
    _check_volumes_project_scoped(cfg, "staging")
    _check_no_bind_mounts(cfg, "staging")
    _check_no_reload_command(cfg, "staging")

    services = _services(cfg)
    for required in ("gateway", "api", "worker", "postgres", "web", "delivery", "storage-init"):
        _assert(required in services, f"staging: missing service {required}")

    for name in ("api", "worker", "web", "postgres", "delivery", "storage-init"):
        _assert(
            not _has_host_ports(services[name]),
            f"staging: {name} must not publish host ports",
        )

    gateway_ports = _published_ports(services["gateway"])
    _assert(len(gateway_ports) == 1, "staging: gateway must publish exactly one port")
    gp = gateway_ports[0]
    published = str(gp.get("published"))
    host_ip = gp.get("host_ip") or gp.get("host_ip".upper())  # noqa: SIM910
    # Compose JSON may place bind address in published ("127.0.0.1:8091") or host_ip.
    published_full = published
    if host_ip:
        published_full = f"{host_ip}:{published}"
    _assert(
        published_full.startswith("127.0.0.1:") or host_ip == "127.0.0.1",
        f"staging: gateway must be loopback-only, got {gp!r}",
    )
    _assert(
        published_full.endswith(":8091") or str(published) == "8091",
        f"staging: default gateway host port must be 8091, got {gp!r}",
    )
    _assert(int(gp.get("target") or 0) == 8080, "staging: gateway target must be 8080")

    _assert(
        services["api"].get("image") == f"fetchnow-api:{revision}",
        "staging: api image must use full SHA tag",
    )
    _assert(
        services["worker"].get("image") == f"fetchnow-api:{revision}",
        "staging: worker must share api image tag",
    )
    _assert(
        services["delivery"].get("image") == f"fetchnow-api:{revision}",
        "staging: delivery must share api image tag",
    )
    _assert(
        services["storage-init"].get("image") == f"fetchnow-api:{revision}",
        "staging: storage-init must share api image tag",
    )
    _assert_storage_init_least_privilege(services["storage-init"], "staging")
    _assert(
        services["web"].get("image") == f"fetchnow-web:{revision}",
        "staging: web image must use full SHA tag",
    )
    _assert(
        services["gateway"].get("image") == f"fetchnow-gateway:{revision}",
        "staging: gateway image must use full SHA tag",
    )
    _assert(
        str(services["postgres"].get("image", "")).startswith("postgres:16.9"),
        "staging: postgres image must remain pinned",
    )

    vols = _volume_defs(cfg)
    for key in ("pgdata", "tmp"):
        name = vols[key].get("name") if isinstance(vols[key], dict) else None
        _assert(
            name == f"fetchnow-staging_{key}",
            f"staging: volume {key} must render as fetchnow-staging_{key}, got {name!r}",
        )

    networks = cfg.get("networks") or {}
    _assert("fetchnow" in networks, "staging: missing fetchnow network")
    print("OK: staging compose contract")


def check_staging_missing_password_fails() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / ".env.staging"
        env_path.write_text(
            "\n".join(
                [
                    "COMPOSE_PROJECT_NAME=fetchnow-staging",
                    "FETCHNOW_RELEASE_REVISION=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "GATEWAY_PORT=127.0.0.1:8091",
                    "APP_ENV=staging",
                    "LOG_LEVEL=INFO",
                    "PUBLIC_SITE_URL=https://staging.example.test",
                    "POSTGRES_DB=fetchnow",
                    "POSTGRES_USER=fetchnow",
                    # POSTGRES_PASSWORD intentionally omitted
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        cmd = [
            "docker",
            "compose",
            "--env-file",
            str(env_path),
            "--project-name",
            "fetchnow-staging",
            "-f",
            "compose.yaml",
            "-f",
            "compose.staging.yaml",
            "config",
        ]
        proc = subprocess.run(
            cmd, cwd=ROOT, capture_output=True, text=True, check=False
        )
        _assert(proc.returncode != 0, "staging: missing password must fail closed")
        blob = (proc.stderr or "") + (proc.stdout or "")
        _assert(
            "POSTGRES_PASSWORD" in blob,
            "staging: failure message must mention POSTGRES_PASSWORD",
        )
    print("OK: staging fail-closed for missing POSTGRES_PASSWORD")


def check_staging_missing_revision_fails() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / ".env.staging"
        env_path.write_text(
            "\n".join(
                [
                    "COMPOSE_PROJECT_NAME=fetchnow-staging",
                    "GATEWAY_PORT=127.0.0.1:8091",
                    "APP_ENV=staging",
                    "PUBLIC_SITE_URL=https://staging.example.test",
                    "POSTGRES_DB=fetchnow",
                    "POSTGRES_USER=fetchnow",
                    "POSTGRES_PASSWORD=staging-contract-check-password-xxxxxxxx",
                    # FETCHNOW_RELEASE_REVISION intentionally omitted
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        cmd = [
            "docker",
            "compose",
            "--env-file",
            str(env_path),
            "--project-name",
            "fetchnow-staging",
            "-f",
            "compose.yaml",
            "-f",
            "compose.staging.yaml",
            "config",
        ]
        proc = subprocess.run(
            cmd, cwd=ROOT, capture_output=True, text=True, check=False
        )
        _assert(proc.returncode != 0, "staging: missing revision must fail closed")
        blob = (proc.stderr or "") + (proc.stdout or "")
        _assert(
            "FETCHNOW_RELEASE_REVISION" in blob,
            "staging: failure must mention FETCHNOW_RELEASE_REVISION",
        )
    print("OK: staging fail-closed for missing FETCHNOW_RELEASE_REVISION")


def check_isolation_render() -> None:
    a = _run_compose(
        ["-f", "compose.yaml"],
        env={"COMPOSE_PROJECT_NAME": "fetchnow-isolation-a"},
    )
    b = _run_compose(
        ["-f", "compose.yaml"],
        env={"COMPOSE_PROJECT_NAME": "fetchnow-isolation-b"},
    )
    a_vols = {k: v.get("name") for k, v in _volume_defs(a).items() if isinstance(v, dict)}
    b_vols = {k: v.get("name") for k, v in _volume_defs(b).items() if isinstance(v, dict)}
    _assert(
        a_vols.get("pgdata") == "fetchnow-isolation-a_pgdata",
        f"isolation-a pgdata unexpected: {a_vols}",
    )
    _assert(
        b_vols.get("pgdata") == "fetchnow-isolation-b_pgdata",
        f"isolation-b pgdata unexpected: {b_vols}",
    )
    _assert(a_vols != b_vols, "isolation: volume maps must differ")
    a_net = (a.get("networks") or {}).get("fetchnow", {})
    b_net = (b.get("networks") or {}).get("fetchnow", {})
    a_name = a_net.get("name") if isinstance(a_net, dict) else None
    b_name = b_net.get("name") if isinstance(b_net, dict) else None
    _assert(a_name != b_name, f"isolation: networks must differ ({a_name} vs {b_name})")
    print("OK: isolation render (volumes/networks differ)")


def check_media_jobs_env_split() -> None:
    """API must not receive yt-dlp/temp roots; worker may; features stay off."""
    cfg = _run_compose(
        ["-f", "compose.yaml"],
        env={"COMPOSE_PROJECT_NAME": "fetchnow"},
    )
    services = _services(cfg)
    api_env = services["api"].get("environment") or {}
    worker_env = services["worker"].get("environment") or {}
    delivery_env = services["delivery"].get("environment") or {}
    _assert(
        "MEDIA_INSPECTION_YTDLP_PATH" not in api_env,
        "base: api must not receive MEDIA_INSPECTION_YTDLP_PATH",
    )
    _assert(
        "MEDIA_DOWNLOAD_TEMP_ROOT" not in api_env,
        "base: api must not receive MEDIA_DOWNLOAD_TEMP_ROOT",
    )
    _assert(
        "MEDIA_DELIVERY_ROOT" not in api_env,
        "base: api must not receive MEDIA_DELIVERY_ROOT",
    )
    _assert(
        "MEDIA_INSPECTION_YTDLP_PATH" not in delivery_env,
        "base: delivery must not receive MEDIA_INSPECTION_YTDLP_PATH",
    )
    _assert(
        "MEDIA_DOWNLOAD_TEMP_ROOT" not in delivery_env,
        "base: delivery must not receive MEDIA_DOWNLOAD_TEMP_ROOT",
    )
    _assert(
        str(api_env.get("MEDIA_JOBS_ENABLED", "false")).lower() in {"false", "0"},
        "base: MEDIA_JOBS_ENABLED must default false on api",
    )
    _assert(
        str(api_env.get("MEDIA_DOWNLOADS_ENABLED", "false")).lower()
        in {"false", "0"},
        "base: MEDIA_DOWNLOADS_ENABLED must default false on api",
    )
    _assert(
        str(delivery_env.get("MEDIA_DELIVERY_ENABLED", "false")).lower()
        in {"false", "0"},
        "base: MEDIA_DELIVERY_ENABLED must default false on delivery",
    )
    _assert(
        str(worker_env.get("MEDIA_JOBS_ENABLED", "false")).lower() in {"false", "0"},
        "base: MEDIA_JOBS_ENABLED must default false on worker",
    )
    _assert(
        str(worker_env.get("MEDIA_INSPECTION_ENABLED", "false")).lower()
        in {"false", "0"},
        "base: MEDIA_INSPECTION_ENABLED must default false on worker",
    )
    _assert(
        str(worker_env.get("MEDIA_DOWNLOADS_ENABLED", "false")).lower()
        in {"false", "0"},
        "base: MEDIA_DOWNLOADS_ENABLED must default false on worker",
    )
    _assert(
        "MEDIA_INSPECTION_YTDLP_PATH" in worker_env,
        "base: worker must declare MEDIA_INSPECTION_YTDLP_PATH for operators",
    )
    _assert(
        "MEDIA_DOWNLOAD_TEMP_ROOT" in worker_env,
        "base: worker must declare MEDIA_DOWNLOAD_TEMP_ROOT for operators",
    )
    path = str(worker_env.get("MEDIA_INSPECTION_YTDLP_PATH") or "")
    _assert(
        path.endswith("/yt-dlp") or path == "/opt/venv/bin/yt-dlp",
        f"base: unexpected worker yt-dlp path {path!r}",
    )

    def _volume_sources(svc: dict[str, Any]) -> list[str]:
        out: list[str] = []
        for mount in svc.get("volumes") or []:
            if isinstance(mount, dict):
                out.append(str(mount.get("source") or ""))
            elif isinstance(mount, str):
                out.append(mount.split(":", 1)[0])
        return out

    def _mount_read_only(svc: dict[str, Any], source: str) -> bool | None:
        for mount in svc.get("volumes") or []:
            if isinstance(mount, dict) and str(mount.get("source") or "") == source:
                return bool(mount.get("read_only"))
            if isinstance(mount, str) and mount.startswith(f"{source}:"):
                return mount.rstrip(":").endswith(":ro") or ":ro:" in f"{mount}:"
        return None

    api_vols = _volume_sources(services["api"])
    gateway_vols = _volume_sources(services["gateway"])
    delivery_vols = _volume_sources(services["delivery"])
    worker_vols = _volume_sources(services["worker"])
    _assert("tmp" not in api_vols, "base: api must not mount artifact tmp volume")
    _assert(
        "tmp" not in gateway_vols,
        "base: gateway must not mount artifact tmp volume",
    )
    _assert("tmp" in delivery_vols, "base: delivery must mount tmp volume")
    _assert("tmp" in worker_vols, "base: worker must mount tmp volume")
    _assert(
        _mount_read_only(services["delivery"], "tmp") is True,
        "base: delivery tmp mount must be read-only",
    )
    _assert(
        _mount_read_only(services["worker"], "tmp") is not True,
        "base: worker tmp mount must remain read-write",
    )
    _assert(
        not _has_host_ports(services["delivery"]),
        "base: delivery must not publish host ports",
    )
    print("OK: media jobs env split (api vs worker)")
    print(
        "OK: delivery isolation (ro volume, no configured yt-dlp path, no host port)"
    )


def _service_env(svc: dict[str, Any]) -> dict[str, str]:
    raw = svc.get("environment") or {}
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


def _assert_storage_init_least_privilege(init: dict[str, Any], label: str) -> None:
    env = _service_env(init)
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
    _assert(not leaked, f"{label}: storage-init must not receive {', '.join(leaked)}")
    _assert(
        str(init.get("network_mode") or "") == "none",
        f"{label}: storage-init must use network_mode none",
    )
    networks = init.get("networks")
    _assert(
        not networks,
        f"{label}: storage-init must not join compose networks",
    )
    user = str(init.get("user") or "")
    _assert(
        user not in {"", "0", "root", "0:0"} and "10001" in user,
        f"{label}: storage-init must run as non-root UID 10001, got {user!r}",
    )
    _assert(
        not _has_host_ports(init),
        f"{label}: storage-init must not publish host ports",
    )


def _depends_on_condition(svc: dict[str, Any], dep: str) -> str | None:
    raw = svc.get("depends_on") or {}
    if isinstance(raw, dict):
        spec = raw.get(dep)
        if isinstance(spec, dict):
            return str(spec.get("condition") or "")
        if spec is not None:
            return "service_started"
    if isinstance(raw, list) and dep in raw:
        return "service_started"
    return None


def check_muxing_and_storage_init() -> None:
    """Muxing stays off; tool paths stay worker-only; empty-volume init has no sleep."""
    disabled = _run_compose(
        ["-f", "compose.yaml"],
        env={"COMPOSE_PROJECT_NAME": "fetchnow"},
    )
    enabled = _run_compose(
        ["-f", "compose.yaml"],
        env={
            "COMPOSE_PROJECT_NAME": "fetchnow",
            "MEDIA_MUXING_ENABLED": "true",
        },
    )
    for label, cfg in (("disabled", disabled), ("enabled", enabled)):
        services = _services(cfg)
        _assert("storage-init" in services, f"{label}: missing storage-init")
        init = services["storage-init"]
        worker = services["worker"]
        api_env = _service_env(services["api"])
        delivery_env = _service_env(services["delivery"])
        worker_env = _service_env(worker)
        init_env = _service_env(init)
        web_args = _web_build_args(services["web"])
        _assert_storage_init_least_privilege(init, label)
        _assert(
            "MEDIA_MUXING_FFMPEG_PATH" not in api_env,
            f"{label}: api_receives_ffmpeg_path must be false",
        )
        _assert(
            "MEDIA_MUXING_FFPROBE_PATH" not in api_env,
            f"{label}: api must not receive MEDIA_MUXING_FFPROBE_PATH",
        )
        _assert(
            "MEDIA_MUXING_FFMPEG_PATH" not in delivery_env,
            f"{label}: delivery_receives_ffmpeg_path must be false",
        )
        _assert(
            "MEDIA_MUXING_FFPROBE_PATH" not in delivery_env,
            f"{label}: delivery must not receive MEDIA_MUXING_FFPROBE_PATH",
        )
        _assert(
            "MEDIA_MUXING_FFMPEG_PATH" not in init_env,
            f"{label}: storage-init must not receive ffmpeg path",
        )
        _assert(
            "PUBLIC_MEDIA_MUXING" not in web_args
            and "MEDIA_MUXING_FFMPEG_PATH" not in web_args,
            f"{label}: web_receives_tool_paths must be false",
        )
        _assert(
            not _has_host_ports(init),
            f"{label}: storage-init must not publish host ports",
        )
        command = init.get("command")
        text = " ".join(command) if isinstance(command, list) else str(command or "")
        _assert("sleep" not in text.lower(), f"{label}: startup_order_uses_sleep")
        _assert(
            _depends_on_condition(worker, "storage-init")
            == "service_completed_successfully",
            f"{label}: worker must wait for storage-init success",
        )
        _assert(
            _depends_on_condition(services["delivery"], "storage-init")
            == "service_completed_successfully",
            f"{label}: delivery must wait for storage-init success",
        )
        _assert(
            _depends_on_condition(worker, "delivery") is None,
            f"{label}: worker must not wait for delivery (either order after init)",
        )
        _assert(
            _depends_on_condition(services["delivery"], "worker") is None,
            f"{label}: delivery must not wait for worker (either order after init)",
        )
        _assert(
            "MEDIA_MUXING_FFMPEG_PATH" in worker_env,
            f"{label}: worker may declare MEDIA_MUXING_FFMPEG_PATH",
        )
        _assert(
            "MEDIA_MUXING_FFPROBE_PATH" in worker_env,
            f"{label}: worker may declare MEDIA_MUXING_FFPROBE_PATH",
        )

    disabled_worker = _service_env(_services(disabled)["worker"])
    enabled_worker = _service_env(_services(enabled)["worker"])
    _assert(
        str(disabled_worker.get("MEDIA_MUXING_ENABLED", "false")).lower()
        in {"false", "0"},
        "disabled: muxing_enabled_by_default must be false",
    )
    _assert(
        str(enabled_worker.get("MEDIA_MUXING_ENABLED", "false")).lower()
        in {"true", "1"},
        "enabled: worker muxing flag must render true when env is set",
    )
    _assert(
        str(
            _service_env(_services(enabled)["api"]).get("MEDIA_MUXING_ENABLED", "")
        ).lower()
        not in {"true", "1"},
        "enabled: api must not receive MEDIA_MUXING_ENABLED=true",
    )
    print("OK: muxing env split and storage-init (disabled and enabled renders)")


def _nginx_config_test(
    image: str, conf: Path, hosts: tuple[str, ...]
) -> subprocess.CompletedProcess[str]:
    add_hosts: list[str] = []
    for host in hosts:
        add_hosts += ["--add-host", f"{host}:127.0.0.1"]
    return subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{conf}:/etc/nginx/nginx.conf:ro",
            *add_hosts,
            image,
            "nginx",
            "-t",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def check_gateway_nginx_config() -> None:
    """Load the shipped nginx.conf with the pinned nginx image.

    A location regex with unquoted braces (``{36}``) parses as a block opener
    and makes the gateway exit at startup, so reading the file is not enough.
    Upstream names are stubbed because nginx resolves them when loading config.
    """
    dockerfile = (ROOT / "deploy" / "nginx" / "Dockerfile").read_text(encoding="utf-8")
    base = re.search(r"(?m)^FROM\s+(nginx:\S+)", dockerfile)
    _assert(base is not None, "gateway: cannot resolve pinned nginx base image")
    assert base is not None
    image = base.group(1)
    conf = ROOT / "deploy" / "nginx" / "nginx.conf"

    full = _nginx_config_test(image, conf, ("api", "web", "delivery"))
    _assert(
        full.returncode == 0,
        f"gateway: nginx -t rejected nginx.conf:\n{full.stderr or full.stdout}",
    )
    print("OK: gateway nginx config loads under the pinned nginx image")

    # Delivery must stay a per-request lookup. A static upstream is resolved when
    # the config loads, so an absent delivery would stop the whole gateway.
    degraded = _nginx_config_test(image, conf, ("api", "web"))
    _assert(
        degraded.returncode == 0,
        "gateway: nginx.conf must load while delivery is unresolvable:\n"
        f"{degraded.stderr or degraded.stdout}",
    )
    print("OK: gateway nginx config loads while delivery is unresolvable")


def _web_build_args(service: dict[str, Any]) -> dict[str, str]:
    build = service.get("build") or {}
    raw = build.get("args") or {}
    if isinstance(raw, list):
        out: dict[str, str] = {}
        for item in raw:
            if isinstance(item, str) and "=" in item:
                key, value = item.split("=", 1)
                out[key] = value
        return out
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    return {}


def check_media_flow_flag() -> None:
    """UI flow stays off unless an operator sets a safe boolean build arg."""
    cfg = _run_compose(
        ["-f", "compose.yaml"],
        env={"COMPOSE_PROJECT_NAME": "fetchnow"},
    )
    args = _web_build_args(_services(cfg)["web"])
    _assert(
        str(args.get("PUBLIC_MEDIA_FLOW_ENABLED", "false")).lower() == "false",
        "base: PUBLIC_MEDIA_FLOW_ENABLED must default false",
    )
    staging = _run_compose(
        ["-f", "compose.yaml", "-f", "compose.staging.yaml"],
        env={
            "COMPOSE_PROJECT_NAME": "fetchnow-staging",
            "POSTGRES_PASSWORD": "staging-contract-check-password-xxxxxxxx",
            "POSTGRES_USER": "fetchnow",
            "POSTGRES_DB": "fetchnow",
            "FETCHNOW_RELEASE_REVISION": "a" * 40,
            "PUBLIC_SITE_URL": "https://staging.example.test",
        },
    )
    staging_args = _web_build_args(_services(staging)["web"])
    _assert(
        str(staging_args.get("PUBLIC_MEDIA_FLOW_ENABLED", "false")).lower()
        == "false",
        "staging: PUBLIC_MEDIA_FLOW_ENABLED must default false",
    )
    print("OK: media flow UI flag defaults false (web build arg)")


def check_gateway_csp() -> None:
    """Interactive HTML CSP is same-origin only and is not applied to delivery."""
    text = (ROOT / "deploy" / "nginx" / "nginx.conf").read_text(encoding="utf-8")
    _assert(
        "connect-src 'self'" in text,
        "gateway: CSP must allow only same-origin connect-src",
    )
    _assert(
        "connect-src *" not in text and "connect-src http:" not in text,
        "gateway: CSP must not broaden connect-src to arbitrary origins",
    )
    _assert(
        "unsafe-eval" not in text,
        "gateway: CSP must not allow unsafe-eval",
    )
    delivery_block = text.split("location ~")[-1]
    _assert(
        "connect-src 'self'" not in delivery_block.split("location /api/")[0]
        if "location /api/" in delivery_block
        else "connect-src 'self'" not in delivery_block,
        "gateway: delivery location must not inherit the interactive connect-src CSP",
    )
    print("OK: gateway CSP is same-origin and does not broaden delivery")


def main() -> int:
    check_base_only_config()
    check_dev_config()
    check_staging_config()
    check_staging_missing_password_fails()
    check_staging_missing_revision_fails()
    check_isolation_render()
    check_media_jobs_env_split()
    check_muxing_and_storage_init()
    check_media_flow_flag()
    check_gateway_nginx_config()
    check_gateway_csp()
    print("All Compose contract checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
