"""Exact-revision release prepare (materialize + build + atomic publish)."""

from __future__ import annotations

import os
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
from .environment import overlay_from_compose_files
from .image_build import (
    ImageBuildError,
    build_application_images,
    compose_version,
    docker_version,
)
from .locking import ReleaseLockError, ReleaseRootLock, assert_same_filesystem
from .manifest import (
    ManifestError,
    ReleaseManifest,
    manifest_path,
    write_manifest,
)
from .preflight import PreflightInput, run_preflight
from .redact import redact
from .revision import RevisionError, validate_full_sha
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
                return _idempotent_success(final, rev)

            incomplete = deploy / f".incomplete-{rev}-{uuid.uuid4().hex}"
            incomplete.mkdir(mode=0o750, parents=True)
            assert_same_filesystem(incomplete, releases)

            try:
                source = materialize_source(
                    repo=inp.repo_root,
                    revision=rev,
                    incomplete_dir=incomplete,
                    source_contract_version=prepare_source_contract_version(),
                )
                messages.append(f"OK: source materialized tree={source.tree_oid}")

                overlay = overlay_from_compose_files(inp.compose_files)
                built = build_application_images(
                    source_dir=source.source_dir,
                    env_file=inp.env_file,
                    revision=rev,
                    compose_files=("compose.yaml", overlay),
                )
                for w in built.warnings:
                    messages.append(f"NOTE: {w}")
                messages.append("OK: application images built and inspected")

                if source.archive_path.exists():
                    source.archive_path.unlink()

                now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                manifest = ReleaseManifest(
                    schema_version=1,
                    source_contract_version=prepare_source_contract_version(),
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
                )
                write_manifest(manifest_path(incomplete), manifest)
                _chmod_tree_readonly(incomplete / SOURCE_DIRNAME)
                # Verify complete identity before the release becomes visible.
                pre = verify_prepared_release(
                    incomplete,
                    expected_revision=rev,
                    require_dirname_match=False,
                )
                if not pre.ok:
                    raise PrepareError(pre.messages[0])
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
