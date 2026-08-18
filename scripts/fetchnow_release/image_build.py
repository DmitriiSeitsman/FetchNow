"""Build and inspect revision-tagged application images from a source snapshot."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import OCI_REVISION_LABEL
from .c2_constants import APPLICATION_BUILD_SERVICES, BUILD_PROJECT_NAME
from .manifest import ImageRecord
from .redact import redact
from .revision import validate_full_sha

_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# Pass-through Docker client variables only — never host secrets.
_DOCKER_ENV_KEYS = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
    "DOCKER_API_VERSION",
    "DOCKER_BUILDKIT",
    "BUILDKIT_PROGRESS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)


class ImageBuildError(RuntimeError):
    """Image build / inspection failure."""


@dataclass(frozen=True)
class BuiltImages:
    images: tuple[ImageRecord, ...]
    warnings: tuple[str, ...]


def _run(argv: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def _compose_build_env(revision: str) -> dict[str, str]:
    """Minimal process env for compose build (secrets come from --env-file only)."""
    env: dict[str, str] = {}
    for key in _DOCKER_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            env[key] = value
    if "PATH" not in env:
        env["PATH"] = "/usr/bin:/bin:/usr/local/bin"
    env["FETCHNOW_RELEASE_REVISION"] = revision
    env["COMPOSE_PROJECT_NAME"] = BUILD_PROJECT_NAME
    return env


def docker_version() -> str:
    proc = _run(["docker", "version", "--format", "{{.Server.Version}}"], cwd=Path("/"))
    if proc.returncode != 0:
        raise ImageBuildError("docker version failed: " + redact(proc.stderr.strip()))
    return proc.stdout.strip() or "unknown"


def compose_version() -> str:
    proc = _run(["docker", "compose", "version", "--short"], cwd=Path("/"))
    if proc.returncode != 0:
        raise ImageBuildError("compose version failed: " + redact(proc.stderr.strip()))
    return proc.stdout.strip() or "unknown"


def inspect_image(reference: str) -> dict[str, Any]:
    proc = _run(
        ["docker", "image", "inspect", reference, "--format", "{{json .}}"],
        cwd=Path("/"),
    )
    if proc.returncode != 0:
        raise ImageBuildError(
            f"docker image inspect failed for {reference}: "
            + redact(proc.stderr.strip())
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ImageBuildError(f"invalid inspect JSON for {reference}") from exc


def image_exists(reference: str) -> bool:
    proc = _run(
        ["docker", "image", "inspect", reference, "--format", "{{.Id}}"],
        cwd=Path("/"),
    )
    return proc.returncode == 0 and bool(proc.stdout.strip())


def target_image_refs(revision: str) -> tuple[str, ...]:
    rev = validate_full_sha(revision)
    return (
        f"fetchnow-api:{rev}",
        f"fetchnow-web:{rev}",
        f"fetchnow-gateway:{rev}",
    )


def containers_using_image(reference: str) -> list[str]:
    """Return running container names using the image ID behind *reference*.

    Uses immutable image IDs via ``docker ps --filter ancestor=<id>`` and
    confirms with ``docker inspect`` Image fields — not display-text alone.
    """
    if not image_exists(reference):
        return []
    image_id = str(inspect_image(reference).get("Id") or "")
    if not _IMAGE_ID_RE.fullmatch(image_id):
        raise ImageBuildError(f"malformed image ID for {reference}: {image_id!r}")

    proc = _run(
        [
            "docker",
            "ps",
            "--filter",
            f"ancestor={image_id}",
            "--format",
            "{{.ID}} {{.Names}}",
        ],
        cwd=Path("/"),
    )
    if proc.returncode != 0:
        raise ImageBuildError("docker ps failed: " + redact(proc.stderr.strip()))

    names: list[str] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        cid = parts[0]
        cname = parts[1] if len(parts) > 1 else cid
        insp = _run(
            ["docker", "inspect", cid, "--format", "{{json .Image}}"],
            cwd=Path("/"),
        )
        if insp.returncode != 0:
            raise ImageBuildError(
                f"docker inspect failed for container {cid}: "
                + redact(insp.stderr.strip())
            )
        try:
            container_image = json.loads(insp.stdout)
        except json.JSONDecodeError as exc:
            raise ImageBuildError(
                f"invalid container inspect JSON for {cid}"
            ) from exc
        # .Image is the immutable ID string.
        if container_image == image_id or str(container_image) == image_id:
            names.append(cname)
            continue
        # Some engines return truncated IDs; require prefix match only on sha256.
        if (
            isinstance(container_image, str)
            and container_image.startswith("sha256:")
            and image_id.startswith(container_image)
        ):
            names.append(cname)
    return names


def assert_tag_collision_safe(
    revision: str, *, allow_unused_retag: bool = True
) -> list[str]:
    """Refuse overwrite of tags used by running containers.

    Unused unmanaged tags are reported; rebuild is allowed when
    allow_unused_retag is True (default prepare policy).

    Operational assumption: exclusive release-root lock serializes prepares for
    one deploy root; a container could still start using an inspected unused tag
    between check and retarget on a shared Docker host — callers recheck
    immediately before build.
    """
    rev = validate_full_sha(revision)
    warnings: list[str] = []
    for name in target_image_refs(rev):
        if not image_exists(name):
            continue
        users = containers_using_image(name)
        if users:
            raise ImageBuildError(
                f"refusing to overwrite {name}: used by running container(s) "
                f"{', '.join(users)}; finalize/verify matching release instead"
            )
        if allow_unused_retag:
            warnings.append(
                f"unused unmanaged tag {name} exists; rebuild will retarget it "
                "(tag is not deleted; published manifest becomes authority)"
            )
        else:
            raise ImageBuildError(
                f"unmanaged tag {name} exists and retag is not allowed"
            )
    return warnings


def _report_unmanaged_tags(revision: str, cause: str) -> str:
    present = [ref for ref in target_image_refs(revision) if image_exists(ref)]
    if not present:
        return cause
    return (
        f"{cause}; unmanaged revision tags may remain after partial failure: "
        + ", ".join(present)
        + " (not deleted; next prepare will report/retarget if unused)"
    )


def build_application_images(
    *,
    source_dir: Path,
    env_file: Path,
    revision: str,
    compose_files: tuple[str, ...],
) -> BuiltImages:
    rev = validate_full_sha(revision)
    warnings = list(assert_tag_collision_safe(rev, allow_unused_retag=True))
    # Recheck immediately before retarget/build (host-level race window).
    warnings = list(assert_tag_collision_safe(rev, allow_unused_retag=True))

    argv = [
        "docker",
        "compose",
        "--env-file",
        str(env_file),
        "--project-name",
        BUILD_PROJECT_NAME,
    ]
    for rel in compose_files:
        path = source_dir / rel
        if not path.is_file() or path.is_symlink():
            raise ImageBuildError(f"compose file missing or unsafe in snapshot: {rel}")
        argv.extend(["-f", str(path.resolve())])
    argv.extend(["build", *APPLICATION_BUILD_SERVICES])

    env = _compose_build_env(rev)
    proc = subprocess.run(
        argv,
        cwd=str(source_dir),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if proc.returncode != 0:
        raise ImageBuildError(
            _report_unmanaged_tags(
                rev,
                "compose build failed: "
                + redact(proc.stderr.strip() or proc.stdout.strip()),
            )
        )

    records: list[ImageRecord] = []
    platform = "unknown"
    try:
        for svc in APPLICATION_BUILD_SERVICES:
            ref = {
                "api": f"fetchnow-api:{rev}",
                "web": f"fetchnow-web:{rev}",
                "gateway": f"fetchnow-gateway:{rev}",
            }[svc]
            info = inspect_image(ref)
            image_id = str(info.get("Id") or "")
            labels = (info.get("Config") or {}).get("Labels") or {}
            oci = labels.get(OCI_REVISION_LABEL)
            arch = str(info.get("Architecture") or "")
            os_name = str(info.get("Os") or "")
            platform = f"{os_name}/{arch}" if os_name or arch else platform
            if oci != rev:
                raise ImageBuildError(
                    f"{svc} OCI revision label {oci!r} != expected {rev}"
                )
            if not _IMAGE_ID_RE.fullmatch(image_id):
                raise ImageBuildError(f"{svc} malformed sha256 image ID: {image_id!r}")
            records.append(
                ImageRecord(
                    service=svc,
                    reference=ref,
                    image_id=image_id,
                    oci_revision=str(oci),
                    platform=platform,
                )
            )

        # Worker must resolve to the same image as API (shared tag).
        worker_ref = f"fetchnow-api:{rev}"
        worker_id = inspect_image(worker_ref).get("Id")
        api_id = next(r.image_id for r in records if r.service == "api")
        if worker_id != api_id:
            raise ImageBuildError("api and worker image IDs diverge after build")
    except ImageBuildError as exc:
        raise ImageBuildError(_report_unmanaged_tags(rev, str(exc))) from exc

    return BuiltImages(images=tuple(records), warnings=tuple(warnings))
