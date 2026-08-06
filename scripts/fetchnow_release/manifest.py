"""Immutable release.json schema (PRD1C2)."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .c2_constants import (
    APPLICATION_BUILD_SERVICES,
    RELEASE_MANIFEST_NAME,
    RELEASE_SCHEMA_VERSION,
    RELEASE_STATUS_PREPARED,
    SOURCE_CONTRACT_VERSION_V1,
    SUPPORTED_SOURCE_CONTRACT_VERSIONS,
)
from .revision import validate_full_sha

_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class ManifestError(ValueError):
    """Invalid release manifest."""


def parse_source_contract_version(raw: object) -> int:
    """Parse manifest source_contract_version.

    Legacy finalized releases omit the field; that omission defaults to v1.
    Contract version is never inferred from source file presence.
    """
    if raw is None:
        return SOURCE_CONTRACT_VERSION_V1
    if type(raw) is not int or isinstance(raw, bool):
        raise ManifestError("source_contract_version must be integer")
    if raw not in SUPPORTED_SOURCE_CONTRACT_VERSIONS:
        raise ManifestError(f"unsupported source_contract_version: {raw!r}")
    return raw


def validate_source_contract_version(version: int) -> None:
    if version not in SUPPORTED_SOURCE_CONTRACT_VERSIONS:
        raise ManifestError(f"unsupported source_contract_version: {version!r}")


@dataclass(frozen=True)
class ImageRecord:
    service: str
    reference: str
    image_id: str
    oci_revision: str
    platform: str


@dataclass(frozen=True)
class ReleaseManifest:
    schema_version: int
    source_contract_version: int
    revision: str
    tree_oid: str
    created_at_utc: str
    archive_commit_verified: bool
    source_repository: str
    compose_version: str
    docker_version: str
    build_platform: str
    images: tuple[ImageRecord, ...]
    contract_hashes: dict[str, str]
    tool_version: str
    status: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["images"] = [asdict(img) for img in self.images]
        return data


def write_manifest(path: Path, manifest: ReleaseManifest) -> None:
    validate_manifest(manifest)
    tmp = path.with_suffix(".tmp")
    payload = json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def load_manifest(path: Path) -> ReleaseManifest:
    if not path.is_file():
        raise ManifestError(f"manifest not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest JSON invalid: {exc}") from exc
    return parse_manifest(raw)


def parse_manifest(raw: Any) -> ReleaseManifest:
    if not isinstance(raw, dict):
        raise ManifestError("manifest must be a JSON object")
    try:
        schema = int(raw["schema_version"])
        revision = str(raw["revision"])
        tree_oid = str(raw["tree_oid"])
        created = str(raw["created_at_utc"])
        archive_ok = bool(raw["archive_commit_verified"])
        source_repo = str(raw["source_repository"])
        compose_ver = str(raw["compose_version"])
        docker_ver = str(raw["docker_version"])
        platform = str(raw["build_platform"])
        tool_ver = str(raw["tool_version"])
        status = str(raw["status"])
        hashes = raw["contract_hashes"]
        images_raw = raw["images"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ManifestError(f"manifest missing/invalid fields: {exc}") from exc
    source_contract_version = parse_source_contract_version(
        raw.get("source_contract_version")
    )
    if schema != RELEASE_SCHEMA_VERSION:
        raise ManifestError(f"unsupported schema_version: {schema}")
    if status != RELEASE_STATUS_PREPARED:
        raise ManifestError(f"unsupported status: {status!r}")
    if not isinstance(hashes, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in hashes.items()
    ):
        raise ManifestError("contract_hashes must be string map")
    if not isinstance(images_raw, list) or not images_raw:
        raise ManifestError("images must be a non-empty list")
    images: list[ImageRecord] = []
    seen_services: set[str] = set()
    for item in images_raw:
        if not isinstance(item, dict):
            raise ManifestError("image entry must be object")
        try:
            rec = ImageRecord(
                service=str(item["service"]),
                reference=str(item["reference"]),
                image_id=str(item["image_id"]),
                oci_revision=str(item["oci_revision"]),
                platform=str(item["platform"]),
            )
        except KeyError as exc:
            raise ManifestError(f"image entry missing field: {exc}") from exc
        if rec.service in seen_services:
            raise ManifestError(f"duplicate image service: {rec.service}")
        seen_services.add(rec.service)
        images.append(rec)
    manifest = ReleaseManifest(
        schema_version=schema,
        source_contract_version=source_contract_version,
        revision=revision,
        tree_oid=tree_oid,
        created_at_utc=created,
        archive_commit_verified=archive_ok,
        source_repository=source_repo,
        compose_version=compose_ver,
        docker_version=docker_ver,
        build_platform=platform,
        images=tuple(images),
        contract_hashes={str(k): str(v) for k, v in hashes.items()},
        tool_version=tool_ver,
        status=status,
    )
    validate_manifest(manifest)
    return manifest


def validate_manifest(manifest: ReleaseManifest) -> None:
    rev = validate_full_sha(manifest.revision)
    if not _SHA_RE.fullmatch(manifest.tree_oid):
        raise ManifestError("tree_oid must be full lowercase SHA")
    if not manifest.archive_commit_verified:
        raise ManifestError("archive_commit_verified must be true")
    if manifest.status != RELEASE_STATUS_PREPARED:
        raise ManifestError("status must be 'prepared'")
    if manifest.schema_version != RELEASE_SCHEMA_VERSION:
        raise ManifestError("unsupported schema_version")
    validate_source_contract_version(manifest.source_contract_version)
    services = {img.service for img in manifest.images}
    expected = set(APPLICATION_BUILD_SERVICES)
    if services != expected:
        raise ManifestError(f"images services {sorted(services)} != {sorted(expected)}")
    for img in manifest.images:
        if not _IMAGE_ID_RE.fullmatch(img.image_id):
            raise ManifestError(f"malformed Docker image ID: {img.image_id!r}")
        if img.oci_revision != rev:
            raise ManifestError(
                f"{img.service} OCI revision {img.oci_revision!r} != {rev}"
            )
        expected_ref = {
            "api": f"fetchnow-api:{rev}",
            "web": f"fetchnow-web:{rev}",
            "gateway": f"fetchnow-gateway:{rev}",
        }[img.service]
        if img.reference != expected_ref:
            raise ManifestError(
                f"{img.service} reference {img.reference!r} != {expected_ref!r}"
            )


def manifest_path(release_directory: Path) -> Path:
    return release_directory / RELEASE_MANIFEST_NAME
