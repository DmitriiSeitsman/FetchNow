"""Inspect immutable images before activation / rollback."""

from __future__ import annotations

from . import OCI_REVISION_LABEL
from .image_build import ImageBuildError, inspect_image
from .override import image_ids_from_manifest
from .manifest import ReleaseManifest
from .revision import validate_full_sha


class ImageIdentityError(RuntimeError):
    """Immutable image identity failure."""


def assert_release_images_present(manifest: ReleaseManifest) -> dict[str, str]:
    """Inspect every target ID; require OCI revision + api/worker equality."""
    rev = validate_full_sha(manifest.revision)
    ids = image_ids_from_manifest(manifest)
    for svc, image_id in ids.items():
        if svc == "worker":
            continue
        try:
            info = inspect_image(image_id)
        except ImageBuildError as exc:
            raise ImageIdentityError(str(exc)) from exc
        got_id = str(info.get("Id") or "")
        if got_id != image_id:
            raise ImageIdentityError(
                f"{svc} inspect Id {got_id!r} != manifest {image_id!r}"
            )
        labels = (info.get("Config") or {}).get("Labels") or {}
        oci = labels.get(OCI_REVISION_LABEL)
        if oci != rev:
            raise ImageIdentityError(
                f"{svc} OCI revision {oci!r} != expected {rev!r}"
            )
    # Worker shares API ID — confirm tag/ID still resolves identically.
    api_id = ids["api"]
    try:
        worker_info = inspect_image(api_id)
    except ImageBuildError as exc:
        raise ImageIdentityError(str(exc)) from exc
    if str(worker_info.get("Id") or "") != api_id:
        raise ImageIdentityError("api/worker image identity diverged")
    if ids["worker"] != api_id:
        raise ImageIdentityError("worker image ID must equal api image ID")
    return ids
