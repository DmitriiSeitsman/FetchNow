"""Save/load exact release images, without builds, pulls or stack activation."""

from __future__ import annotations

import json
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import MIN_FREE_BYTES, OCI_REVISION_LABEL
from .bounded_subprocess import BoundedSubprocessError, run_bounded
from .deploy_root import release_dir, validate_deploy_root
from .image_archive import (
    IMAGE_ID,
    MAX_ARCHIVE_BYTES,
    MAX_METADATA_BYTES,
    PLATFORM,
    SHA256,
    parse_json,
    sha256_file,
    validate_image_archive,
)
from .image_build import ImageBuildError, inspect_image
from .locking import ReleaseRootLock
from .manifest import ReleaseManifest, load_manifest, manifest_path
from .redact import redact
from .release_mode import build_services_for_mode
from .revision import validate_full_sha
from .roots import validate_operator_root
from .verify_release import verify_prepared_release

SCHEMA = "fetchnow-image-bundle/v1"
BUNDLE_FILE = "image-bundle.json"
IMPORT_RECEIPT = "image-bundle-import.json"
IMAGE_IO_TIMEOUT = 900.0


class ImageBundleError(ValueError):
    """Transport failure; no fallback to rebuilding images is permitted."""


@dataclass(frozen=True)
class ImageBundle:
    directory: Path
    sha256: str
    manifest: ReleaseManifest
    release_manifest_sha256: str
    images: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class BundleResult:
    ok: bool
    messages: tuple[str, ...]


def _json_file(path: Path) -> dict[str, Any]:
    if path.stat().st_size > MAX_METADATA_BYTES:
        raise ImageBundleError("oversized bundle metadata")
    value = parse_json(path.read_bytes())
    if not isinstance(value, dict):
        raise ImageBundleError("bundle metadata must be an object")
    return value


def _write_new_json(path: Path, value: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as fh:
        json.dump(value, fh, sort_keys=True, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())


def _docker(
    argv: list[str], *, mutating: bool = False, long_running: bool = False
) -> str:
    try:
        result = run_bounded(
            argv,
            cwd=Path("/"),
            timeout=IMAGE_IO_TIMEOUT if mutating or long_running else 20.0,
        )
    except (BoundedSubprocessError, OSError) as exc:
        note = (
            " Docker mutation outcome uncertain; inspect the daemon, do not retry automatically."
            if mutating
            else ""
        )
        raise ImageBundleError("image transport command failed." + note) from exc
    if result.returncode != 0 or result.timed_out or result.cancelled:
        raise ImageBundleError(
            "image transport command failed: "
            + redact(result.stderr_text.strip())
            + (
                "; loaded images/tags may remain; no automatic rollback"
                if mutating
                else ""
            )
        )
    return result.stdout_text


def _assert_image(image_id: str, revision: str | None) -> None:
    info = inspect_image(image_id)
    if (
        not isinstance(info, dict)
        or info.get("Id") != image_id
        or f"{info.get('Os')}/{info.get('Architecture')}" != PLATFORM
    ):
        raise ImageBundleError("loaded/exported image ID or platform mismatch")
    config = info.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    if revision is not None and (
        not isinstance(labels, dict) or labels.get(OCI_REVISION_LABEL) != revision
    ):
        raise ImageBundleError("loaded/exported OCI revision mismatch")


def _postgres_identity(value: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key in (
        "config_image_id",
        "registry_index_digest",
        "platform_manifest_digest_linux_amd64",
    ):
        digest = value.get(key)
        if not isinstance(digest, str) or not IMAGE_ID.fullmatch(digest):
            raise ImageBundleError(
                "explicit PostgreSQL config/index/platform manifest identities required"
            )
        result[key] = digest
    if (
        len(set(result.values())) != 3
        or value.get("platform") != PLATFORM
        or value.get("candidate_only") is not True
    ):
        raise ImageBundleError(
            "PostgreSQL candidate identity kinds/platform are inconsistent"
        )
    return result


def validate_bundle(directory: Path, *, expected_sha256: str) -> ImageBundle:
    """Validate every file/archive before making any Docker mutation."""
    if not SHA256.fullmatch(expected_sha256):
        raise ImageBundleError("expected bundle SHA-256 must be full lowercase hex")
    if directory.is_symlink() or not directory.is_dir():
        raise ImageBundleError("bundle must be a real directory")
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file():
            raise ImageBundleError("bundle contains non-regular files")
    metadata_path = directory / BUNDLE_FILE
    if sha256_file(metadata_path) != expected_sha256:
        raise ImageBundleError("bundle manifest checksum mismatch")
    data = _json_file(metadata_path)
    if data.get("schema") != SCHEMA or data.get("platform") != PLATFORM:
        raise ImageBundleError("unsupported bundle schema/platform")
    release_hash = data.get("release_manifest_sha256")
    if not isinstance(release_hash, str) or not SHA256.fullmatch(release_hash):
        raise ImageBundleError("release manifest checksum missing")
    release_path = directory / "release.json"
    if sha256_file(release_path) != release_hash:
        raise ImageBundleError("release manifest checksum mismatch")
    # Reuse the canonical release schema without accepting duplicate JSON keys.
    from .manifest import parse_manifest

    manifest = parse_manifest(_json_file(release_path))
    if manifest.build_platform != PLATFORM or any(
        img.platform != PLATFORM for img in manifest.images
    ):
        raise ImageBundleError("release manifest platform differs from bundle")
    for key, expected in (
        ("revision", manifest.revision),
        ("tree_oid", manifest.tree_oid),
        ("release_mode", manifest.release_mode),
        ("compose_overlay", manifest.compose_overlay),
    ):
        if data.get(key) != expected:
            raise ImageBundleError("bundle/release metadata mismatch: " + key)
    rows = data.get("images")
    services = set(build_services_for_mode(manifest.release_mode)) | {"postgres"}
    if not isinstance(rows, list) or len(rows) != len(services):
        raise ImageBundleError("incomplete bundle image set")
    seen: set[str] = set()
    filenames = {BUNDLE_FILE, "release.json"}
    by_service = {img.service: img for img in manifest.images}
    for row in rows:
        if not isinstance(row, dict):
            raise ImageBundleError("malformed image entry")
        svc = row.get("service")
        if not isinstance(svc, str) or svc not in services or svc in seen:
            raise ImageBundleError("duplicate or unexpected image service")
        seen.add(svc)
        name = svc + ".tar"
        if row.get("archive") != name or row.get("platform") != PLATFORM:
            raise ImageBundleError("unexpected archive path/platform")
        image_id = row.get("image_id")
        checksum = row.get("sha256")
        size = row.get("size_bytes")
        if not isinstance(image_id, str) or not IMAGE_ID.fullmatch(image_id):
            raise ImageBundleError("malformed bundle Image ID")
        if not isinstance(checksum, str) or not SHA256.fullmatch(checksum):
            raise ImageBundleError("malformed archive SHA-256")
        if type(size) is not int or not 0 < size <= MAX_ARCHIVE_BYTES:
            raise ImageBundleError("invalid archive size")
        reference = None if svc == "postgres" else by_service[svc].reference
        if row.get("reference") != reference:
            raise ImageBundleError("bundle image reference mismatch")
        if svc != "postgres" and image_id != by_service[svc].image_id:
            raise ImageBundleError("bundle Image ID differs from release")
        if svc == "postgres":
            _postgres_identity(
                {
                    "config_image_id": image_id,
                    "registry_index_digest": row.get("registry_index_digest"),
                    "platform_manifest_digest_linux_amd64": row.get(
                        "platform_manifest_digest_linux_amd64"
                    ),
                    "platform": row["platform"],
                    "candidate_only": True,
                }
            )
        archive = directory / name
        if archive.stat().st_size != size or sha256_file(archive) != checksum:
            raise ImageBundleError("archive checksum/size mismatch: " + svc)
        validate_image_archive(
            archive,
            image_id=image_id,
            revision=None if svc == "postgres" else manifest.revision,
        )
        filenames.add(name)
    if seen != services or {p.name for p in directory.iterdir()} != filenames:
        raise ImageBundleError("bundle missing files or contains extra files")
    return ImageBundle(
        directory.resolve(), expected_sha256, manifest, release_hash, tuple(rows)
    )


def export_image_bundle(
    *,
    deploy_root: Path,
    repo_root: Path,
    revision: str,
    output: Path,
    postgres_identity_json: Path,
    wait_lock: bool = False,
) -> BundleResult:
    """Export existing exact IDs; publish bundle metadata only after validation."""
    try:
        rev = validate_full_sha(revision)
        pg = _postgres_identity(_json_file(postgres_identity_json))
        postgres_image_id = pg["config_image_id"]
        deploy = validate_deploy_root(deploy_root, repo_root=repo_root)
        dest = validate_operator_root(output, repo_root=repo_root, label="image bundle")
        if dest.exists() or output.is_symlink() or dest.is_relative_to(deploy):
            raise ImageBundleError(
                "export destination must be new and outside deploy root"
            )
        with ReleaseRootLock(deploy, wait=wait_lock):
            release = release_dir(deploy, rev)
            checked = verify_prepared_release(
                release, expected_revision=rev, repo_root=repo_root
            )
            if not checked.ok:
                raise ImageBundleError(checked.messages[0])
            manifest = load_manifest(manifest_path(release))
            if manifest.build_platform != PLATFORM or any(
                img.platform != PLATFORM for img in manifest.images
            ):
                raise ImageBundleError("only linux/amd64 releases may be exported")
            _assert_image(postgres_image_id, None)
            dest.mkdir(mode=0o700)
            release_bytes = manifest_path(release).read_bytes()
            with (dest / "release.json").open("xb") as fh:
                fh.write(release_bytes)
                fh.flush()
                os.fsync(fh.fileno())
            images: list[dict[str, Any]] = []
            records: list[tuple[str, str, str | None]] = [
                (img.service, img.image_id, img.reference) for img in manifest.images
            ]
            records.append(("postgres", postgres_image_id, None))
            for svc, image_id, reference in records:
                _assert_image(image_id, None if svc == "postgres" else rev)
                path = dest / (svc + ".tar")
                _docker(
                    [
                        "docker",
                        "image",
                        "save",
                        "--platform",
                        PLATFORM,
                        "--output",
                        str(path),
                        image_id,
                    ],
                    long_running=True,
                )
                validate_image_archive(
                    path, image_id=image_id, revision=None if svc == "postgres" else rev
                )
                images.append(
                    {
                        "service": svc,
                        "reference": reference,
                        "image_id": image_id,
                        "platform": PLATFORM,
                        "archive": path.name,
                        "sha256": sha256_file(path),
                        "size_bytes": path.stat().st_size,
                    }
                )
                if svc == "postgres":
                    images[-1].update(
                        {
                            key: val
                            for key, val in pg.items()
                            if key != "config_image_id"
                        }
                    )
                with path.open("rb") as fh:
                    os.fsync(fh.fileno())
                _assert_image(image_id, None if svc == "postgres" else rev)
            checked = verify_prepared_release(
                release, expected_revision=rev, repo_root=repo_root
            )
            if not checked.ok:
                raise ImageBundleError(checked.messages[0])
            _write_new_json(
                dest / BUNDLE_FILE,
                {
                    "schema": SCHEMA,
                    "platform": PLATFORM,
                    "revision": rev,
                    "tree_oid": manifest.tree_oid,
                    "release_mode": manifest.release_mode,
                    "compose_overlay": manifest.compose_overlay,
                    "release_manifest_sha256": sha256_file(dest / "release.json"),
                    "images": images,
                },
            )
            digest = sha256_file(dest / BUNDLE_FILE)
            validate_bundle(dest, expected_sha256=digest)
            fd = os.open(dest, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            return BundleResult(
                True,
                (
                    "OK: exact image bundle exported (not a security approval)",
                    f"bundle={dest}",
                    f"bundle_sha256={digest}",
                ),
            )
    except (ValueError, OSError, ImageBuildError) as exc:
        return BundleResult(
            False,
            (
                "FAIL: " + redact(str(exc)),
                "Partial export files, if any, are retained; do not retry or delete unrelated resources.",
            ),
        )


def _inventory() -> tuple[dict[str, str], set[str]]:
    payload = _docker(
        ["docker", "image", "ls", "--all", "--no-trunc", "--format", "{{json .}}"]
    )
    tags: dict[str, str] = {}
    ids: set[str] = set()
    for line in payload.splitlines():
        row = parse_json(line.encode())
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("ID"), str)
            or not IMAGE_ID.fullmatch(row["ID"])
        ):
            raise ImageBundleError("invalid Docker image inventory")
        ids.add(row["ID"])
        repo, tag = row.get("Repository"), row.get("Tag")
        if not isinstance(repo, str) or not isinstance(tag, str):
            raise ImageBundleError("invalid Docker tag inventory")
        if repo != "<none>" and tag != "<none>":
            ref = repo + ":" + tag
            if ref in tags and tags[ref] != row["ID"]:
                raise ImageBundleError("conflicting Docker tag inventory")
            tags[ref] = row["ID"]
    return tags, ids


def _check_tags(bundle: ImageBundle, tags: dict[str, str]) -> None:
    for img in bundle.manifest.images:
        if img.reference in tags and tags[img.reference] != img.image_id:
            raise ImageBundleError(
                "refusing to retarget existing image tag: " + img.reference
            )


def assert_bundle_images_present(bundle: ImageBundle) -> None:
    for row in bundle.images:
        _assert_image(
            row["image_id"],
            None if row["service"] == "postgres" else bundle.manifest.revision,
        )
    for img in bundle.manifest.images:
        info = inspect_image(img.reference)
        if not isinstance(info, dict) or info.get("Id") != img.image_id:
            raise ImageBundleError("imported tag/ID mismatch")


def load_bundle_images(bundle: ImageBundle, *, incomplete: Path) -> dict[str, Any]:
    """Stage private verified bytes, load untagged IDs, then tag apps only."""
    host = _docker(["docker", "info", "--format", "{{.OSType}}/{{.Architecture}}"])
    if host.strip() not in {"linux/amd64", "linux/x86_64"}:
        raise ImageBundleError("image import requires a native linux/amd64 Docker host")
    if sha256_file(bundle.directory / BUNDLE_FILE) != bundle.sha256:
        raise ImageBundleError("bundle manifest changed before import")
    tags, present = _inventory()
    _check_tags(bundle, tags)
    total = sum(row["size_bytes"] for row in bundle.images)
    if shutil.disk_usage(incomplete).free < total + MIN_FREE_BYTES:
        raise ImageBundleError("insufficient free space for private image staging")
    stage = incomplete / "image-staging"
    stage.mkdir(mode=0o700)
    # Copy and verify ALL payloads before the first Docker load. No extraction
    # and no loading directly from a mutable operator-supplied directory.
    for row in bundle.images:
        src, dst = bundle.directory / row["archive"], stage / row["archive"]
        fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source, dst.open("xb") as target:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ImageBundleError("image archive is no longer a regular file")
            remaining = row["size_bytes"]
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ImageBundleError("archive truncated while staging")
                target.write(chunk)
                remaining -= len(chunk)
            if source.read(1):
                raise ImageBundleError("archive grew while staging")
            target.flush()
            os.fsync(target.fileno())
        if dst.stat().st_size != row["size_bytes"] or sha256_file(dst) != row["sha256"]:
            raise ImageBundleError("archive changed while staging")
        dst.chmod(0o400)
    for row in bundle.images:
        if row["image_id"] not in present:
            _docker(
                [
                    "docker",
                    "image",
                    "load",
                    "--platform",
                    PLATFORM,
                    "--input",
                    str(stage / row["archive"]),
                ],
                mutating=True,
            )
        _assert_image(
            row["image_id"],
            None if row["service"] == "postgres" else bundle.manifest.revision,
        )
    for img in bundle.manifest.images:
        current, _ = _inventory()
        _check_tags(bundle, current)
        if img.reference not in current:
            _docker(
                ["docker", "image", "tag", img.image_id, img.reference], mutating=True
            )
    assert_bundle_images_present(bundle)
    # Never tag postgres, touch containers, or publish current/media authority.
    receipt = {
        "schema": "fetchnow-image-bundle-import/v1",
        "bundle_sha256": bundle.sha256,
        "revision": bundle.manifest.revision,
        "platform": PLATFORM,
        "image_ids": {row["service"]: row["image_id"] for row in bundle.images},
        "postgres_candidate_only": True,
        "postgres_registry_identity": {
            key: next(row for row in bundle.images if row["service"] == "postgres")[key]
            for key in ("registry_index_digest", "platform_manifest_digest_linux_amd64")
        },
        "security_approval_applied": False,
    }
    _write_new_json(incomplete / IMPORT_RECEIPT, receipt)
    shutil.rmtree(stage)
    return receipt
