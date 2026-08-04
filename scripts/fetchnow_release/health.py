"""Read-only Compose + HTTP health gate."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import EXPECTED_SERVICES, OCI_REVISION_LABEL
from .docker_checks import DockerCheckError, require_compose_v2, require_docker_daemon
from .http_health import HttpCheckResult, HttpHealthError, check_endpoint
from .redact import redact
from .revision import RevisionError, validate_full_sha


class HealthError(ValueError):
    """Health gate failure."""


@dataclass(frozen=True)
class HealthInput:
    project_name: str
    env_file: Path
    compose_files: tuple[Path, ...]
    expected_revision: str
    repo_root: Path
    gateway_base_url: str = "http://127.0.0.1:8091"


@dataclass(frozen=True)
class HealthResult:
    ok: bool
    messages: tuple[str, ...]
    http: tuple[HttpCheckResult, ...] = ()


def _compose_argv(inp: HealthInput, *args: str) -> list[str]:
    argv = [
        "docker",
        "compose",
        "--env-file",
        str(inp.env_file),
        "--project-name",
        inp.project_name,
    ]
    for path in inp.compose_files:
        argv.extend(["-f", str(path)])
    argv.extend(args)
    return argv


def _ps_services(inp: HealthInput) -> list[dict[str, Any]]:
    proc = subprocess.run(
        _compose_argv(inp, "ps", "-a", "--format", "json"),
        cwd=str(inp.repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise HealthError(f"compose ps failed: {proc.stderr.strip()}")
    text = proc.stdout.strip()
    if not text:
        return []
    # Compose may emit NDJSON or a JSON array.
    if text.startswith("["):
        data = json.loads(text)
        if not isinstance(data, list):
            raise HealthError("unexpected compose ps JSON")
        return data
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def _inspect(container_id: str) -> dict[str, Any]:
    proc = subprocess.run(
        ["docker", "inspect", container_id],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise HealthError(f"docker inspect failed for {container_id}")
    data = json.loads(proc.stdout)
    if not data:
        raise HealthError(f"empty inspect for {container_id}")
    return data[0]


def run_health(inp: HealthInput) -> HealthResult:
    messages: list[str] = []
    http_results: list[HttpCheckResult] = []
    try:
        rev = validate_full_sha(inp.expected_revision, allow_local=False)
        require_compose_v2()
        require_docker_daemon()

        rows = _ps_services(inp)
        by_service: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            # Compose v2/v5 field names vary: Service / Name / Project
            svc = row.get("Service") or row.get("service")
            if not svc:
                # Fall back from container name project-service-n
                name = str(row.get("Name") or row.get("name") or "")
                parts = name.split("-")
                svc = parts[-2] if len(parts) >= 2 else name
            by_service.setdefault(str(svc), []).append(row)

        for expected in EXPECTED_SERVICES:
            if expected not in by_service:
                raise HealthError(f"missing service container: {expected}")
            if len(by_service[expected]) != 1:
                raise HealthError(
                    f"service {expected} has {len(by_service[expected])} containers; expected 1"
                )

        unknown = sorted(set(by_service) - set(EXPECTED_SERVICES))
        if unknown:
            raise HealthError(f"unknown/orphan services present: {', '.join(unknown)}")

        api_image_id = None
        worker_image_id = None

        for svc_name in EXPECTED_SERVICES:
            row = by_service[svc_name][0]
            state = str(row.get("State") or row.get("state") or "").lower()
            health = str(row.get("Health") or row.get("health") or "").lower()
            cid = str(row.get("ID") or row.get("Id") or row.get("id") or "")
            if state not in {"running"}:
                raise HealthError(f"{svc_name} state is {state!r}, expected running")
            # Worker has healthcheck disabled — only require running.
            if svc_name != "worker":
                if health and health not in {"healthy", ""}:
                    # empty sometimes when health not yet in ps; confirm via inspect
                    pass
                if health in {"unhealthy", "starting"}:
                    raise HealthError(f"{svc_name} health is {health!r}")

            if not cid:
                raise HealthError(f"{svc_name} missing container id")
            info = _inspect(cid)
            labels = (info.get("Config") or {}).get("Labels") or {}
            project_label = labels.get("com.docker.compose.project")
            if project_label != inp.project_name:
                raise HealthError(
                    f"{svc_name} project label {project_label!r} != {inp.project_name!r}"
                )
            image = str(
                (info.get("Config") or {}).get("Image") or info.get("Image") or ""
            )
            # Prefer RepoTags from image inspect via Config.Image which may be ID;
            # use row Image field when available.
            image_ref = str(row.get("Image") or image)
            if svc_name in {"api", "worker"}:
                if f":{rev}" not in image_ref and not image_ref.endswith(f":{rev}"):
                    # Also accept digest-only if label matches — still require tag in Image field
                    if (
                        not image_ref.endswith(rev)
                        and f"fetchnow-api:{rev}" not in image_ref
                    ):
                        raise HealthError(
                            f"{svc_name} image reference {image_ref!r} missing tag :{rev}"
                        )
            elif svc_name == "web":
                if f"fetchnow-web:{rev}" not in image_ref and not image_ref.endswith(
                    f":{rev}"
                ):
                    raise HealthError(f"web image {image_ref!r} missing tag :{rev}")
            elif svc_name == "gateway":
                if (
                    f"fetchnow-gateway:{rev}" not in image_ref
                    and not image_ref.endswith(f":{rev}")
                ):
                    raise HealthError(f"gateway image {image_ref!r} missing tag :{rev}")

            if svc_name in {"api", "worker", "web", "gateway"}:
                oci = labels.get(OCI_REVISION_LABEL)
                if oci != rev:
                    raise HealthError(
                        f"{svc_name} OCI revision label {oci!r} != {rev!r}"
                    )

            image_id = info.get("Image")
            if svc_name == "api":
                api_image_id = image_id
            if svc_name == "worker":
                worker_image_id = image_id

            # Health from inspect when configured. Worker has healthcheck disabled.
            health_obj = (info.get("State") or {}).get("Health")
            hstate = ((health_obj or {}).get("Status") or "").lower()
            if svc_name != "worker":
                if health_obj is None:
                    raise HealthError(f"{svc_name} has no Docker healthcheck status")
                if hstate != "healthy":
                    raise HealthError(f"{svc_name} inspect health is {hstate!r}")
            restarts = int((info.get("RestartCount") or 0))
            status = str((info.get("State") or {}).get("Status") or "").lower()
            if status in {"exited", "dead", "restarting"}:
                raise HealthError(f"{svc_name} container status is {status!r}")
            messages.append(
                f"OK: {svc_name} running health={hstate or 'n/a'} restarts={restarts}"
            )

        if api_image_id and worker_image_id and api_image_id != worker_image_id:
            raise HealthError("api and worker must share the same image ID")

        for path in ("/api/v1/health/live", "/api/v1/health/ready"):
            result = check_endpoint(inp.gateway_base_url, path)
            http_results.append(result)
            if not result.ok:
                raise HealthError(
                    f"HTTP {path} failed after {result.attempts} attempts: {result.detail}"
                )
            messages.append(f"OK: HTTP {path} status=ok attempts={result.attempts}")

        messages.append("OK: staging health gate passed")
        return HealthResult(ok=True, messages=tuple(messages), http=tuple(http_results))
    except (
        HealthError,
        HttpHealthError,
        RevisionError,
        DockerCheckError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        return HealthResult(
            ok=False,
            messages=(f"FAIL: {redact(str(exc))}", *tuple(messages)),
            http=tuple(http_results),
        )
