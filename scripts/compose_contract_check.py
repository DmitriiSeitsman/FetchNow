#!/usr/bin/env python3
"""Validate FetchNow Compose base/dev/staging contracts (PRD1A).

Uses `docker compose config --format json` — no extra Python deps.
Exits non-zero on the first failed invariant.
"""

from __future__ import annotations

import json
import os
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
    for required in ("gateway", "api", "worker", "postgres", "web"):
        _assert(required in services, f"dev: missing service {required}")
    # Local override publishes gateway 8080 and api 8000.
    _assert(_has_host_ports(services["gateway"]), "dev: gateway should publish a port")
    _assert(_has_host_ports(services["api"]), "dev: api debug port expected via override")
    _assert(not _has_host_ports(services["postgres"]), "dev: postgres must not publish")
    _assert(not _has_host_ports(services["worker"]), "dev: worker must not publish")
    _assert(not _has_host_ports(services["web"]), "dev: web must not publish")
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
    print("OK: base compose contract (no host ports)")


def check_staging_config() -> None:
    example = ROOT / ".env.staging.example"
    _assert(example.is_file(), ".env.staging.example must exist")

    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / ".env.staging"
        # Safe local test values derived from the example contract.
        text = example.read_text(encoding="utf-8")
        text = text.replace(
            "replace-with-unique-staging-password",
            "staging-contract-check-only",
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
    for required in ("gateway", "api", "worker", "postgres", "web"):
        _assert(required in services, f"staging: missing service {required}")

    for name in ("api", "worker", "web", "postgres"):
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


def main() -> int:
    check_base_only_config()
    check_dev_config()
    check_staging_config()
    check_staging_missing_password_fails()
    check_isolation_render()
    print("All Compose contract checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
