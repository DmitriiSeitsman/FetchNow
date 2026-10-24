"""Exact-revision release prepare (materialize + build + atomic publish)."""

from __future__ import annotations

import os
import json
import shutil
import stat
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .archive import ArchiveError, materialize_source
from .c2_constants import RELEASE_STATUS_PREPARED, SOURCE_DIRNAME
from .deploy_root import (
    DeployRootError,
    ensure_deploy_layout,
    release_dir,
    validate_deploy_root,
)
from .git_checks import (
    GitCheckError,
    assert_clean_worktree,
    assert_revision_in_origin_main,
)
from .environment import overlay_from_compose_files, recorded_compose_overlay
from .image_build import (
    ImageBuildError,
    build_application_images,
    compose_version,
    docker_version,
)
from .image_bundle import (
    IMPORT_RECEIPT,
    ImageBundleError,
    assert_bundle_images_present,
    load_bundle_images,
    validate_bundle,
)
from .locking import ReleaseLockError, ReleaseRootLock, assert_same_filesystem
from .manifest import (
    ManifestError,
    ReleaseManifest,
    load_manifest,
    manifest_path,
    write_manifest,
)
from .preflight import PreflightInput, run_preflight
from .redact import redact
from .revision import RevisionError, validate_full_sha
from .release_mode import RELEASE_MODE_CANONICAL, parse_release_mode
from .source_contract import prepare_source_contract_version
from .verify_release import ReleaseVerifyError, verify_prepared_release


class PrepareError(RuntimeError):
    """Release prepare failure."""


@dataclass(frozen=True)
class PrepareInput:
    project_name: str
    env_file: Path
    compose_files: tuple[Path, ...]
    expected_revision: str
    repo_root: Path
    deploy_root: Path
    wait_lock: bool = False
    release_mode: str = RELEASE_MODE_CANONICAL
    image_bundle: Path | None = None
    image_bundle_sha256: str | None = None


@dataclass(frozen=True)
class PrepareResult:
    ok: bool
    already_prepared: bool
    messages: tuple[str, ...]
    release_dir: Path | None = None


def _chmod_tree_readonly(root: Path) -> None:
    """Remove write bits; preserve execute bits where previously set.

    Never follows symlinks (chmod would otherwise affect link targets).
    """
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            mode = stat.S_IMODE(path.stat().st_mode)
            if path.is_dir():
                path.chmod((mode & ~0o222) | 0o111)
            else:
                path.chmod(mode & ~0o222)
    if not root.is_symlink():
        mode = stat.S_IMODE(root.stat().st_mode)
        root.chmod((mode & ~0o222) | 0o111)


def _remove_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
        return
    shutil.rmtree(path)


def _safe_repo_identity(repo: Path) -> str:
    return repo.resolve().name


def _idempotent_success(final: Path, revision: str) -> PrepareResult:
    result = verify_prepared_release(final, expected_revision=revision)
    if not result.ok:
        raise PrepareError(result.messages[0])
    return PrepareResult(
        ok=True,
        already_prepared=True,
        messages=(
            "OK: release already prepared",
            f"revision={revision}",
            f"path={final}",
        ),
        release_dir=final,
    )


def prepare_release(inp: PrepareInput) -> PrepareResult:
    messages: list[str] = []
    incomplete: Path | None = None
    try:
        rev = validate_full_sha(inp.expected_revision)
        deploy = validate_deploy_root(inp.deploy_root, repo_root=inp.repo_root)
        contract_version = prepare_source_contract_version()
        release_mode = parse_release_mode(
            inp.release_mode,
            source_contract_version=contract_version,
            required=True,
        )
        if (inp.image_bundle is None) != (inp.image_bundle_sha256 is None):
            raise PrepareError(
                "--image-bundle and --image-bundle-sha256 must be supplied together"
            )
        bundle = None
        overlay: str | None = None
        if inp.image_bundle is not None and inp.image_bundle_sha256 is not None:
            overlay = overlay_from_compose_files(inp.compose_files)
            bundle = validate_bundle(
                inp.image_bundle, expected_sha256=inp.image_bundle_sha256
            )
            if (
                bundle.manifest.revision != rev
                or bundle.manifest.release_mode != release_mode
            ):
                raise PrepareError(
                    "bundle revision/release_mode differs from requested release"
                )
            if (
                recorded_compose_overlay(
                    bundle.manifest.source_contract_version,
                    bundle.manifest.compose_overlay,
                )
                != overlay
            ):
                raise PrepareError(
                    "bundle overlay differs from target; cross-environment image promotion is not implicit"
                )
            contract_version = bundle.manifest.source_contract_version

        pre = run_preflight(
            PreflightInput(
                project_name=inp.project_name,
                env_file=inp.env_file,
                compose_files=inp.compose_files,
                expected_revision=rev,
                repo_root=inp.repo_root,
                deploy_root=deploy,
            )
        )
        if not pre.ok:
            raise PrepareError(pre.messages[0] if pre.messages else "preflight failed")
        messages.extend([m for m in pre.messages if m.startswith("OK:")])

        assert_clean_worktree(inp.repo_root)
        assert_revision_in_origin_main(inp.repo_root, rev)

        with ReleaseRootLock(deploy, wait=inp.wait_lock):
            releases, _locks = ensure_deploy_layout(deploy)
            final = release_dir(deploy, rev)
            if final.exists():
                existing = load_manifest(manifest_path(final))
                if existing.release_mode != release_mode:
                    raise PrepareError(
                        f"release already prepared with release_mode="
                        f"{existing.release_mode!r}; requested {release_mode!r}. "
                        "A rebuild creates a new artifact and must not reuse "
                        "prior native/Trivy PASS evidence."
                    )
                if bundle is not None:
                    if existing.to_dict() != bundle.manifest.to_dict():
                        raise PrepareError(
                            "existing prepared release differs from bundle"
                        )
                    receipt = json.loads(
                        (final / IMPORT_RECEIPT).read_text(encoding="utf-8")
                    )
                    if (
                        not isinstance(receipt, dict)
                        or receipt.get("bundle_sha256") != bundle.sha256
                    ):
                        raise PrepareError(
                            "existing release lacks matching bundle import receipt"
                        )
                    assert_bundle_images_present(bundle)
                return _idempotent_success(final, rev)

            incomplete = deploy / f".incomplete-{rev}-{uuid.uuid4().hex}"
            incomplete.mkdir(mode=0o750, parents=True)
            assert_same_filesystem(incomplete, releases)

            try:
                source = materialize_source(
                    repo=inp.repo_root,
                    revision=rev,
                    incomplete_dir=incomplete,
                    source_contract_version=contract_version,
                )
                messages.append(f"OK: source materialized tree={source.tree_oid}")

                if overlay is None:
                    overlay = overlay_from_compose_files(inp.compose_files)

                if bundle is not None:
                    if (
                        source.tree_oid != bundle.manifest.tree_oid
                        or dict(source.contract_hashes)
                        != bundle.manifest.contract_hashes
                    ):
                        raise PrepareError(
                            "trusted source tree/contract differs from bundled release"
                        )
                    load_bundle_images(bundle, incomplete=incomplete)
                    manifest = bundle.manifest
                    messages.append(
                        "OK: exact saved images imported; no build/pull performed"
                    )
                else:
                    built = build_application_images(
                        source_dir=source.source_dir,
                        env_file=inp.env_file,
                        revision=rev,
                        compose_files=("compose.yaml", overlay),
                        release_mode=release_mode,
                    )
                    for w in built.warnings:
                        messages.append(f"NOTE: {w}")
                    messages.append(
                        f"OK: application images built and inspected "
                        f"(release_mode={release_mode})"
                    )
                    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                    manifest = ReleaseManifest(
                        schema_version=1,
                        source_contract_version=contract_version,
                        revision=rev,
                        tree_oid=source.tree_oid,
                        created_at_utc=now,
                        archive_commit_verified=True,
                        source_repository=_safe_repo_identity(inp.repo_root),
                        compose_version=compose_version(),
                        docker_version=docker_version(),
                        build_platform=(
                            built.images[0].platform if built.images else "unknown"
                        ),
                        images=built.images,
                        contract_hashes=dict(source.contract_hashes),
                        tool_version=__version__,
                        status=RELEASE_STATUS_PREPARED,
                        compose_overlay=overlay,
                        release_mode=release_mode,
                    )

                if source.archive_path.exists():
                    source.archive_path.unlink()

                write_manifest(manifest_path(incomplete), manifest)
                _chmod_tree_readonly(incomplete / SOURCE_DIRNAME)
                # Verify complete identity before the release becomes visible.
                prepared = verify_prepared_release(
                    incomplete,
                    expected_revision=rev,
                    require_dirname_match=False,
                )
                if not prepared.ok:
                    raise PrepareError(prepared.messages[0])
                fd = os.open(str(incomplete), os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)

                os.rename(incomplete, final)
                incomplete = None
                fd = os.open(str(releases), os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)

                verified = verify_prepared_release(final, expected_revision=rev)
                if not verified.ok:
                    # Final path is published; fail closed without silent repair.
                    raise PrepareError(
                        "post-publish verification failed (release left for "
                        "operator inspection; not silently repaired): "
                        + verified.messages[0]
                    )
                messages.append("OK: release published atomically")
                messages.append(f"path={final}")
                return PrepareResult(
                    ok=True,
                    already_prepared=False,
                    messages=tuple(messages),
                    release_dir=final,
                )
            except Exception:
                if incomplete is not None and incomplete.exists():
                    _remove_path(incomplete)
                raise
    except (
        PrepareError,
        ArchiveError,
        ImageBuildError,
        ImageBundleError,
        ManifestError,
        ReleaseLockError,
        DeployRootError,
        GitCheckError,
        RevisionError,
        ReleaseVerifyError,
        OSError,
        ValueError,
    ) as exc:
        return PrepareResult(
            ok=False,
            already_prepared=False,
            messages=(f"FAIL: {redact(str(exc))}",),
            release_dir=None,
        )
