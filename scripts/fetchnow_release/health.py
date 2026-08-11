"""Read-only Compose + HTTP health gate."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import EXPECTED_SERVICES, OCI_REVISION_LABEL
from .current_state import CurrentStateError, CurrentStateV2, load_parsed_current_state
from .deploy_root import DeployRootError, release_dir, validate_deploy_root
from .docker_checks import DockerCheckError, require_compose_v2, require_docker_daemon
from .health_project import HealthProjectError, assert_isolated_tag_health_project
from .http_health import HttpCheckResult, HttpHealthError, check_endpoint
from .image_identity import ImageIdentityError, assert_release_images_present
from .journal import sha256_file
from .journal_io import JournalIOError
from .manifest import ManifestError, load_manifest, manifest_path
from .override import OverrideError, image_ids_from_manifest
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
    # Supplying a deploy root selects managed mode. Image IDs are then derived
    # exclusively from validated schema-v2 current state and its release manifest.
    deploy_root: Path | None = None
    # When set (PRD1C3A image-ID activation), require exact inspect IDs for
    # api/worker/web/gateway instead of tag-string matching alone. This internal
    # field is used by rollout/recovery; managed standalone health must not set it.
    expected_image_ids: dict[str, str] | None = None


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


def _managed_image_ids(inp: HealthInput, revision: str) -> dict[str, str]:
    """Resolve managed health identity without mutating deploy or Docker state."""
    if inp.deploy_root is None:
        raise HealthError("managed health requires deploy root")
    if inp.expected_image_ids is not None:
        raise HealthError("managed health refuses caller-supplied image IDs")

    deploy_root = validate_deploy_root(inp.deploy_root, repo_root=inp.repo_root)
    parsed = load_parsed_current_state(deploy_root)
    if parsed is None:
        raise HealthError("managed current.json is missing")
    if not isinstance(parsed, CurrentStateV2):
        raise HealthError("managed health requires current.json schema_version 2")

    application = parsed.application
    if application.revision != revision:
        raise HealthError("current application revision does not match expected revision")

    release = release_dir(deploy_root, revision)
    if release.is_symlink() or not release.is_dir():
        raise HealthError("managed prepared release is missing or unsafe")
    man_path = manifest_path(release)
    if man_path.is_symlink() or not man_path.is_file():
        raise HealthError("managed release manifest is missing or unsafe")
    if sha256_file(man_path) != application.release_manifest_sha256:
        raise HealthError("current application release-manifest hash mismatch")

    manifest = load_manifest(man_path)
    if manifest.revision != revision:
        raise HealthError("managed release manifest revision mismatch")
    manifest_ids = image_ids_from_manifest(manifest)
    if manifest_ids != application.image_ids:
        raise HealthError("current application image IDs do not match release manifest")

    inspected_ids = assert_release_images_present(manifest)
    if inspected_ids != application.image_ids:
        raise HealthError("local image IDs do not match managed current state")
    return dict(application.image_ids)


def run_health(inp: HealthInput) -> HealthResult:
    messages: list[str] = []
    http_results: list[HttpCheckResult] = []
    try:
        rev = validate_full_sha(inp.expected_revision, allow_local=False)
        require_compose_v2()
        require_docker_daemon()

        expected_ids = inp.expected_image_ids
        if inp.deploy_root is not None:
            expected_ids = _managed_image_ids(inp, rev)
        elif expected_ids is None:
            # Standalone CLI/tag path: never allow protected managed projects to
            # silently fall back to revision-tag matching of Config.Image.
            assert_isolated_tag_health_project(inp.project_name)

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
            service_label = labels.get("com.docker.compose.service")
            if service_label != svc_name:
                raise HealthError(
                    f"{svc_name} service label {service_label!r} != {svc_name!r}"
                )
            image = str(
                (info.get("Config") or {}).get("Image") or info.get("Image") or ""
            )
            # Prefer RepoTags from image inspect via Config.Image which may be ID;
            # use row Image field when available.
            image_ref = str(row.get("Image") or image)
            image_id = str(info.get("Image") or "")

            if svc_name in {"api", "worker", "web", "gateway"}:
                oci = labels.get(OCI_REVISION_LABEL)
                if oci != rev:
                    raise HealthError(
                        f"{svc_name} OCI revision label {oci!r} != {rev!r}"
                    )
                if expected_ids is not None:
                    # Worker shares the API image ID contract.
                    key = "api" if svc_name == "worker" else svc_name
                    want = expected_ids.get(key)
                    if not want:
                        raise HealthError(f"missing expected image ID for {key}")
                    if image_id != want:
                        raise HealthError(
                            f"{svc_name} image ID {image_id!r} != expected {want!r}"
                        )
                else:
                    if svc_name in {"api", "worker"}:
                        if (
                            f":{rev}" not in image_ref
                            and not image_ref.endswith(f":{rev}")
                        ):
                            if (
                                not image_ref.endswith(rev)
                                and f"fetchnow-api:{rev}" not in image_ref
                            ):
                                raise HealthError(
                                    f"{svc_name} image reference {image_ref!r} "
                                    f"missing tag :{rev}"
                                )
                    elif svc_name == "web":
                        if (
                            f"fetchnow-web:{rev}" not in image_ref
                            and not image_ref.endswith(f":{rev}")
                        ):
                            raise HealthError(
                                f"web image {image_ref!r} missing tag :{rev}"
                            )
                    elif svc_name == "gateway":
                        if (
                            f"fetchnow-gateway:{rev}" not in image_ref
                            and not image_ref.endswith(f":{rev}")
                        ):
                            raise HealthError(
                                f"gateway image {image_ref!r} missing tag :{rev}"
                            )

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
            restarts = int(info.get("RestartCount") or 0)
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
        CurrentStateError,
        DeployRootError,
        HealthProjectError,
        HttpHealthError,
        ImageIdentityError,
        JournalIOError,
        ManifestError,
        OverrideError,
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
