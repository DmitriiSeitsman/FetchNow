"""Inspect immutable images before activation / rollback."""

from __future__ import annotations

from . import OCI_REVISION_LABEL
from .image_build import ImageBuildError, inspect_image
from .manifest import ReleaseManifest
from .override import image_ids_from_manifest
from .release_mode import image_reference_for_service
from .revision import validate_full_sha


class ImageIdentityError(RuntimeError):
    """Immutable image identity failure."""


def assert_release_images_present(manifest: ReleaseManifest) -> dict[str, str]:
    """Inspect every target ID; require OCI revision + api/worker equality.

    Returns the application image_ids map (api/worker/web/gateway). Media
    sidecar identities are verified when present but are not folded into the
    returned map (keeps current.json / journal contracts stable).
    """
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

    media_services = {
        img.service: img
        for img in manifest.images
        if img.service in {"media-executor", "media-net-executor", "egress-proxy"}
    }
    if media_services and set(media_services) != {
        "media-executor",
        "media-net-executor",
        "egress-proxy",
    }:
        raise ImageIdentityError(
            f"partial media image set: {sorted(media_services)}"
        )
    for svc, rec in media_services.items():
        try:
            info = inspect_image(rec.image_id)
        except ImageBuildError as exc:
            raise ImageIdentityError(str(exc)) from exc
        if str(info.get("Id") or "") != rec.image_id:
            raise ImageIdentityError(f"{svc} inspect Id drift")
        labels = (info.get("Config") or {}).get("Labels") or {}
        if labels.get(OCI_REVISION_LABEL) != rev:
            raise ImageIdentityError(f"{svc} OCI revision mismatch")
        expected_ref = image_reference_for_service(svc, rev)
        if rec.reference != expected_ref or rec.oci_revision != rev:
            raise ImageIdentityError(
                f"{svc} reference/revision mismatch vs {expected_ref!r}"
            )
    return ids
