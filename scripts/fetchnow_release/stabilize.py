"""Service wait + stabilization policy for PRD1C3A rollouts."""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .c3_constants import (
    APPLICATION_SERVICES,
    SERVICE_HEALTH_DEADLINE_SECONDS,
    SERVICE_HEALTH_POLL_SECONDS,
    STABILIZE_CONSECUTIVE_SUCCESSES,
    STABILIZE_INTERVAL_SECONDS,
    STABILIZE_WINDOW_SECONDS,
)
from .health import HealthInput, HealthResult, run_health
from .redact import redact


class StabilizeError(RuntimeError):
    """Health / stabilization failure."""


@dataclass(frozen=True)
class StabilizationPolicy:
    consecutive_successes: int = STABILIZE_CONSECUTIVE_SUCCESSES
    interval_seconds: float = STABILIZE_INTERVAL_SECONDS
    window_seconds: float = STABILIZE_WINDOW_SECONDS
    service_deadline_seconds: float = SERVICE_HEALTH_DEADLINE_SECONDS
    service_poll_seconds: float = SERVICE_HEALTH_POLL_SECONDS


def _compose_ps(
    *,
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
    cwd: Path,
) -> list[dict]:
    argv = [
        "docker",
        "compose",
        "--env-file",
        str(env_file),
        "--project-name",
        project_name,
    ]
    for path in compose_files:
        argv.extend(["-f", str(path)])
    argv.extend(["ps", "-a", "--format", "json"])
    proc = subprocess.run(
        argv, cwd=str(cwd), capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise StabilizeError("compose ps failed: " + redact(proc.stderr.strip()))
    text = proc.stdout.strip()
    if not text:
        return []
    if text.startswith("["):
        data = json.loads(text)
        return data if isinstance(data, list) else []
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _inspect_restart(cid: str) -> int:
    proc = subprocess.run(
        ["docker", "inspect", cid, "--format", "{{.RestartCount}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise StabilizeError("docker inspect restart failed")
    return int(proc.stdout.strip() or "0")


def wait_services_healthy(
    *,
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
    cwd: Path,
    services: tuple[str, ...],
    policy: StabilizationPolicy,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Wait until listed services are running (and healthy when applicable)."""
    deadline = clock() + policy.service_deadline_seconds
    need = set(services)
    while clock() < deadline:
        rows = _compose_ps(
            project_name=project_name,
            env_file=env_file,
            compose_files=compose_files,
            cwd=cwd,
        )
        by: dict[str, dict] = {}
        for row in rows:
            svc = str(row.get("Service") or row.get("service") or "")
            if svc in need:
                by[svc] = row
        ok = True
        for svc in need:
            row = by.get(svc)
            if not row:
                ok = False
                break
            state = str(row.get("State") or "").lower()
            if state != "running":
                ok = False
                break
            if svc != "worker":
                health = str(row.get("Health") or "").lower()
                if health and health != "healthy":
                    ok = False
                    break
        if ok and need <= set(by):
            return
        sleeper(policy.service_poll_seconds)
    raise StabilizeError(
        f"timed out waiting for services {sorted(need)} to become healthy"
    )


def collect_restart_counts(
    *,
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
    cwd: Path,
) -> dict[str, int]:
    rows = _compose_ps(
        project_name=project_name,
        env_file=env_file,
        compose_files=compose_files,
        cwd=cwd,
    )
    out: dict[str, int] = {}
    for row in rows:
        svc = str(row.get("Service") or row.get("service") or "")
        if svc not in APPLICATION_SERVICES and svc != "postgres":
            continue
        cid = str(row.get("ID") or row.get("Id") or "")
        if not cid:
            continue
        out[svc] = _inspect_restart(cid)
    return out


def stabilize_full_health(
    health_input: HealthInput,
    *,
    policy: StabilizationPolicy | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> HealthResult:
    """Require consecutive successful PRD1C1 health gates with stable restarts."""
    pol = policy or StabilizationPolicy()
    baseline = collect_restart_counts(
        project_name=health_input.project_name,
        env_file=health_input.env_file,
        compose_files=health_input.compose_files,
        cwd=health_input.repo_root,
    )
    successes = 0
    window_end = clock() + pol.window_seconds
    last: HealthResult | None = None
    while clock() < window_end or successes < pol.consecutive_successes:
        last = run_health(health_input)
        if not last.ok:
            successes = 0
            sleeper(pol.interval_seconds)
            if clock() >= window_end and successes < pol.consecutive_successes:
                break
            continue
        current = collect_restart_counts(
            project_name=health_input.project_name,
            env_file=health_input.env_file,
            compose_files=health_input.compose_files,
            cwd=health_input.repo_root,
        )
        for svc, base in baseline.items():
            if current.get(svc, base) > base:
                raise StabilizeError(
                    f"restart counter increased for {svc} during stabilization"
                )
        successes += 1
        if successes >= pol.consecutive_successes:
            return last
        sleeper(pol.interval_seconds)
    detail = last.messages[0] if last and last.messages else "health gate failed"
    raise StabilizeError(
        f"stabilization failed after window "
        f"({successes}/{pol.consecutive_successes} consecutive): {detail}"
    )
