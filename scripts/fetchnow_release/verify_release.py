"""Read-only verification of a published release snapshot."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .archive import resolve_tree_oid, verify_required_files
from .c2_constants import APPLICATION_BUILD_SERVICES, SOURCE_DIRNAME
from .image_build import image_exists, inspect_image
from .manifest import ManifestError, load_manifest, manifest_path, validate_manifest
from .redact import redact
from .revision import RevisionError, validate_full_sha


class ReleaseVerifyError(ValueError):
    """Release verification failure."""


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    messages: tuple[str, ...]


def _assert_readonly_tree(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & 0o222:
                raise ReleaseVerifyError(
                    f"release snapshot path is writable (immutability violated): {path}"
                )


def verify_prepared_release(
    release_directory: Path,
    *,
    expected_revision: str,
    repo_root: Path | None = None,
    require_dirname_match: bool = True,
) -> VerifyResult:
    """Validate release snapshot + image identity.

    When *require_dirname_match* is False (pre-publish incomplete directory),
    directory name may differ from the revision (``.incomplete-...``).
    """
    messages: list[str] = []
    try:
        rev = validate_full_sha(expected_revision)
        if require_dirname_match and release_directory.name != rev:
            raise ReleaseVerifyError(
                f"release directory name {release_directory.name!r} != revision {rev}"
            )
        man_path = manifest_path(release_directory)
        manifest = load_manifest(man_path)
        validate_manifest(manifest)
        if manifest.revision != rev:
            raise ReleaseVerifyError("manifest revision mismatch")

        source = release_directory / SOURCE_DIRNAME
        if not source.is_dir():
            raise ReleaseVerifyError("missing source/ directory")
        _assert_readonly_tree(source)
        hashes = verify_required_files(
            source, source_contract_version=manifest.source_contract_version
        )
        if hashes != manifest.contract_hashes:
            raise ReleaseVerifyError("source contract hash drift vs manifest")

        if repo_root is not None:
            tree = resolve_tree_oid(repo_root, rev)
            if tree != manifest.tree_oid:
                raise ReleaseVerifyError(
                    f"Git tree OID drift: repo {tree} != manifest {manifest.tree_oid}"
                )

        for svc in APPLICATION_BUILD_SERVICES:
            rec = next(i for i in manifest.images if i.service == svc)
            if not image_exists(rec.reference):
                raise ReleaseVerifyError(f"missing image tag {rec.reference}")
            info = inspect_image(rec.reference)
            if info.get("Id") != rec.image_id:
                raise ReleaseVerifyError(
                    f"image ID drift for {rec.reference}: "
                    f"docker {info.get('Id')} != manifest {rec.image_id}"
                )
            labels = (info.get("Config") or {}).get("Labels") or {}
            if labels.get("org.opencontainers.image.revision") != rev:
                raise ReleaseVerifyError(f"OCI label drift for {rec.reference}")

        api = next(i for i in manifest.images if i.service == "api")
        # Worker shares api image tag/ID.
        worker_info = inspect_image(f"fetchnow-api:{rev}")
        if worker_info.get("Id") != api.image_id:
            raise ReleaseVerifyError("api/worker image identity mismatch")

        messages.append("OK: release verification passed")
        messages.append(f"revision={rev}")
        messages.append(f"path={release_directory}")
        return VerifyResult(ok=True, messages=tuple(messages))
    except (
        ReleaseVerifyError,
        ManifestError,
        RevisionError,
        OSError,
        ValueError,
        StopIteration,
    ) as exc:
        return VerifyResult(ok=False, messages=(f"FAIL: {redact(str(exc))}",))
