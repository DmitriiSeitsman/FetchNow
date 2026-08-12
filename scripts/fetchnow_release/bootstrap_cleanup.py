"""Bootstrap failure cleanup using deployment ownership labels."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .c3_constants import (
    RUNTIME_APPLICATION_SERVICES,
    LABEL_DEPLOYMENT_ID,
    LABEL_RELEASE_REVISION,
)
from .redact import redact

COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_SERVICE_LABEL = "com.docker.compose.service"


@dataclass(frozen=True)
class RemovedBootstrapContainer:
    container_id: str
    service: str
    labels: dict[str, str]


class BootstrapCleanupError(RuntimeError):
    """Bootstrap-owned container cleanup failure."""


def _compose_ps_json(
    *,
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
    cwd: Path,
) -> list[dict[str, object]]:
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
    argv.extend(["ps", "--format", "json"])
    proc = subprocess.run(
        argv, cwd=str(cwd), capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise BootstrapCleanupError(
            "compose ps failed: "
            + redact(proc.stderr.strip() or proc.stdout.strip() or "unknown")
        )
    rows: list[dict[str, object]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BootstrapCleanupError(f"invalid compose ps JSON: {exc}") from exc
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


def _inspect_labels(container_id: str) -> dict[str, str]:
    proc = subprocess.run(
        [
            "docker",
            "inspect",
            container_id,
            "--format",
            "{{json .Config.Labels}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise BootstrapCleanupError(
            "docker inspect failed: "
            + redact(proc.stderr.strip() or proc.stdout.strip() or container_id)
        )
    try:
        raw = json.loads(proc.stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        raise BootstrapCleanupError(f"invalid inspect labels JSON: {exc}") from exc
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def discover_owned_bootstrap_containers(
    *,
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
    cwd: Path,
    deployment_id: str,
    release_revision: str,
) -> list[RemovedBootstrapContainer]:
    """Return owned application containers for this bootstrap transaction."""
    owned: list[RemovedBootstrapContainer] = []
    for row in _compose_ps_json(
        project_name=project_name,
        env_file=env_file,
        compose_files=compose_files,
        cwd=cwd,
    ):
        svc = str(row.get("Service") or row.get("service") or "")
        if svc not in RUNTIME_APPLICATION_SERVICES:
            continue
        cid = str(row.get("ID") or row.get("Id") or "").strip()
        if not cid:
            continue
        labels = _inspect_labels(cid)
        if labels.get(COMPOSE_PROJECT_LABEL) != project_name:
            continue
        if labels.get(COMPOSE_SERVICE_LABEL) != svc:
            continue
        if labels.get(LABEL_DEPLOYMENT_ID) != deployment_id:
            continue
        if labels.get(LABEL_RELEASE_REVISION) != release_revision:
            continue
        owned.append(
            RemovedBootstrapContainer(
                container_id=cid, service=svc, labels=dict(labels)
            )
        )
    return owned


def remove_owned_bootstrap_containers(
    *,
    project_name: str,
    env_file: Path,
    compose_files: tuple[Path, ...],
    cwd: Path,
    deployment_id: str,
    release_revision: str,
    expected_container_ids: list[str] | None = None,
) -> tuple[RemovedBootstrapContainer, ...]:
    """Remove only containers labeled for this bootstrap deployment."""
    discovered = discover_owned_bootstrap_containers(
        project_name=project_name,
        env_file=env_file,
        compose_files=compose_files,
        cwd=cwd,
        deployment_id=deployment_id,
        release_revision=release_revision,
    )
    if expected_container_ids is not None:
        expected = set(expected_container_ids)
        discovered_ids = {item.container_id for item in discovered}
        if discovered_ids != expected:
            raise BootstrapCleanupError(
                "bootstrap container ownership drift between discovery passes "
                f"(expected {sorted(expected)}, got {sorted(discovered_ids)})"
            )
    removed: list[RemovedBootstrapContainer] = []
    for item in discovered:
        proc = subprocess.run(
            ["docker", "rm", "-f", item.container_id],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise BootstrapCleanupError(
                "failed to remove owned bootstrap container "
                f"{item.container_id}: "
                + redact(proc.stderr.strip() or proc.stdout.strip() or "unknown")
            )
        removed.append(item)
    return tuple(removed)
