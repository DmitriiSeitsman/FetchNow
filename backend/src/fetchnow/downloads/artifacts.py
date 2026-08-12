"""Private ephemeral artifact workspace and publication."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fetchnow.downloads.errors import (
    DownloadError,
    DownloadErrorCode,
    raise_download_error,
)

_MANIFEST_SCHEMA = 1
_MANIFEST_KEYS = frozenset(
    {
        "schemaVersion",
        "downloadJobId",
        "fence",
        "attempt",
        "formatOptionId",
        "size",
        "sha256",
        "container",
        "contentType",
        "createdAt",
        "expiresAt",
    }
)
_ALLOWED_CONTAINERS = frozenset({"mp4", "webm", "mkv", "m4a", "mp3", "ogg"})
_CONTENT_TYPES: dict[str, str] = {
    "mp4": "video/mp4",
    "webm": "video/webm",
    "mkv": "video/x-matroska",
    "m4a": "audio/mp4",
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
}
_SHA256_HEX_LEN = 64
_MAX_MANIFEST_BYTES = 4096
# A zero grace would let one worker delete a publication another worker created
# microseconds earlier but has not yet committed as ready, and would delete an
# attempt directory that a peer is actively writing into. Reconciliation is
# eventually consistent with the database, so it must never act on entries that
# are younger than the widest publish→commit window.
MIN_ORPHAN_GRACE_SECONDS = 60


@dataclass(frozen=True, slots=True)
class PublishedArtifact:
    """Opaque published artifact metadata (no filesystem path)."""

    artifact_id: uuid.UUID
    size_bytes: int
    sha256_hex: str
    container: str
    content_type: str


@dataclass(frozen=True, slots=True)
class AttemptWorkspace:
    """Per-attempt directory under the managed root."""

    job_id: uuid.UUID
    attempt: int
    fence: int
    path: Path
    home: Path
    cache: Path
    output: Path


def content_type_for_container(container: str) -> str:
    """Map a bounded container token to a safe content type."""
    key = container.strip().lower()
    if key not in _CONTENT_TYPES:
        raise_download_error(
            DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT,
            internal_reason="UNKNOWN_CONTAINER",
        )
    return _CONTENT_TYPES[key]


class ArtifactStore:
    """Worker-only artifact root with atomic publish and bounded orphan sweep."""

    __slots__ = ("_root", "_max_bytes", "_orphan_grace_seconds")

    def __init__(
        self,
        *,
        root: str,
        max_bytes: int,
        orphan_grace_seconds: int,
    ) -> None:
        self._root = self.validate_root(root)
        self._max_bytes = max_bytes
        if int(orphan_grace_seconds) < MIN_ORPHAN_GRACE_SECONDS:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="ORPHAN_GRACE_TOO_SMALL",
            )
        self._orphan_grace_seconds = int(orphan_grace_seconds)

    @property
    def root(self) -> Path:
        return self._root

    @staticmethod
    def validate_root(path: str) -> Path:
        """Require an absolute normalized 0700 directory owned by this UID.

        Every existing path component must be a real directory: a symlink
        anywhere in the chain (not only the final element) is rejected. An
        existing operator-provided root is never chmodded — unsafe metadata
        fails closed instead of being silently repaired.
        """
        if not isinstance(path, str) or not path.strip():
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="TEMP_ROOT_UNSET",
            )
        raw = path.strip()
        if not os.path.isabs(raw):
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="TEMP_ROOT_NOT_ABSOLUTE",
            )
        # The configured value must already be canonical. Normalizing and
        # accepting would let two different strings name the same root and would
        # silently absorb `..`, `.`, duplicate separators, and trailing
        # separators instead of surfacing a misconfigured deployment.
        normalized = os.path.normpath(raw)
        if raw != normalized or not os.path.isabs(normalized) or normalized == os.sep:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="TEMP_ROOT_NOT_NORMALIZED",
            )
        root = Path(normalized)

        try:
            for component in (*reversed(root.parents), root):
                if component.is_symlink():
                    raise_download_error(
                        DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                        internal_reason="TEMP_ROOT_PATH_COMPONENT_SYMLINK",
                    )

            if not root.exists():
                os.makedirs(root, mode=0o700, exist_ok=False)
                if root.is_symlink():
                    raise_download_error(
                        DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                        internal_reason="TEMP_ROOT_PATH_COMPONENT_SYMLINK",
                    )
                # Only a directory this process just created is mode-repaired,
                # so a permissive umask cannot leave it wider than 0700.
                os.chmod(root, 0o700)

            flags = os.O_RDONLY
            for name in ("O_DIRECTORY", "O_NOFOLLOW"):
                flags |= getattr(os, name, 0)
            fd = os.open(root, flags)
            try:
                st = os.fstat(fd)
            finally:
                os.close(fd)
            if not stat.S_ISDIR(st.st_mode):
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                    internal_reason="TEMP_ROOT_NOT_DIR",
                )
            if stat.S_IMODE(st.st_mode) != 0o700:
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                    internal_reason="TEMP_ROOT_MODE",
                )
            if st.st_uid != os.getuid():
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                    internal_reason="TEMP_ROOT_OWNER",
                )
        except DownloadError:
            raise
        except OSError:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="TEMP_ROOT_STAT_FAILED",
            )
        return root

    def free_bytes(self) -> int:
        """Return free bytes on the artifact filesystem."""
        try:
            usage = shutil.disk_usage(self._root)
        except OSError:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="DISK_USAGE_FAILED",
            )
        return int(usage.free)

    def check_free_space(self, min_free_bytes: int) -> bool:
        """Return True when free disk meets the configured headroom."""
        if min_free_bytes <= 0:
            return True
        try:
            return self.free_bytes() >= min_free_bytes
        except DownloadError:
            return False

    def ensure_min_free(self, min_free_bytes: int) -> None:
        """Fail closed when free disk is below the configured headroom."""
        if self.free_bytes() < min_free_bytes:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="DISK_HEADROOM",
            )

    @staticmethod
    def output_tree_byte_size(output_dir: Path) -> int:
        """Sum regular-file sizes under output_dir without following links."""
        total = 0
        try:
            if not output_dir.exists() or output_dir.is_symlink():
                return 0
            for dirpath, dirnames, filenames in os.walk(
                output_dir, followlinks=False
            ):
                base = Path(dirpath)
                # Never descend through symlink directories.
                dirnames[:] = [
                    name
                    for name in dirnames
                    if not (base / name).is_symlink()
                ]
                for name in filenames:
                    path = base / name
                    if path.is_symlink():
                        continue
                    try:
                        st = path.lstat()
                    except OSError:
                        continue
                    if stat.S_ISREG(st.st_mode):
                        total += int(st.st_size)
        except OSError:
            return total
        return total

    def _resolve_under_root(self, *parts: str) -> Path:
        candidate = self._root.joinpath(*parts)
        try:
            resolved_root = self._root.resolve(strict=True)
            resolved = candidate.resolve(strict=False)
        except OSError:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="PATH_RESOLVE_FAILED",
            )
        try:
            resolved.relative_to(resolved_root)
        except ValueError:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="PATH_ESCAPE",
            )
        return candidate

    def _reject_symlink_path(self, path: Path, *, reason: str) -> None:
        try:
            if path.is_symlink():
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                    internal_reason=reason,
                )
        except DownloadError:
            raise
        except OSError:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="PATH_STAT_FAILED",
            )

    def _mkdir_private(self, path: Path, *, exist_ok: bool = False) -> None:
        self._reject_symlink_path(path, reason="PATH_IS_SYMLINK")
        try:
            if exist_ok:
                os.makedirs(path, mode=0o700, exist_ok=True)
            else:
                os.mkdir(path, 0o700)
            self._reject_symlink_path(path, reason="PATH_IS_SYMLINK")
            os.chmod(path, 0o700)
        except DownloadError:
            raise
        except FileExistsError:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="PATH_EXISTS",
            )
        except OSError:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="MKDIR_FAILED",
            )

    def create_attempt_workspace(
        self,
        *,
        job_id: uuid.UUID,
        attempt: int,
        fence: int,
    ) -> AttemptWorkspace:
        """Create ``root/{job_id}/{attempt}_{fence}/{home,cache,output}/`` (0700)."""
        if attempt < 1 or fence < 0:
            raise_download_error(
                DownloadErrorCode.INTERNAL_ERROR,
                internal_reason="ATTEMPT_IDENTITY_INVALID",
            )
        job_dir = self._resolve_under_root(str(job_id))
        attempt_name = f"{attempt}_{fence}"
        workspace = self._resolve_under_root(str(job_id), attempt_name)
        home = self._resolve_under_root(str(job_id), attempt_name, "home")
        cache = self._resolve_under_root(str(job_id), attempt_name, "cache")
        output = self._resolve_under_root(str(job_id), attempt_name, "output")
        try:
            self._mkdir_private(job_dir, exist_ok=True)
            if workspace.exists():
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                    internal_reason="WORKSPACE_EXISTS_OR_LINK",
                )
            self._mkdir_private(workspace, exist_ok=False)
            self._mkdir_private(home, exist_ok=False)
            self._mkdir_private(cache, exist_ok=False)
            self._mkdir_private(output, exist_ok=False)
        except DownloadError:
            with suppress(Exception):
                if workspace.exists() and not workspace.is_symlink():
                    shutil.rmtree(workspace, ignore_errors=True)
            raise
        return AttemptWorkspace(
            job_id=job_id,
            attempt=attempt,
            fence=fence,
            path=workspace,
            home=home,
            cache=cache,
            output=output,
        )

    def cleanup_attempt_workspace(
        self,
        workspace: AttemptWorkspace,
        *,
        require_gone: bool = False,
    ) -> None:
        """Remove an attempt workspace; never follows links outside the root."""
        path = workspace.path
        try:
            resolved_root = self._root.resolve(strict=True)
            resolved = path.resolve(strict=False)
            resolved.relative_to(resolved_root)
        except (OSError, ValueError):
            if require_gone:
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                    internal_reason="WORKSPACE_CLEANUP_ESCAPE",
                )
            return
        if path.is_symlink():
            if require_gone:
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                    internal_reason="WORKSPACE_CLEANUP_SYMLINK",
                )
            return
        try:
            shutil.rmtree(path, ignore_errors=False)
        except FileNotFoundError:
            return
        except OSError:
            if require_gone:
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                    internal_reason="WORKSPACE_CLEANUP_FAILED",
                )
            return
        if require_gone and path.exists():
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="WORKSPACE_CLEANUP_RESIDUAL",
            )
        # Best-effort: remove empty job directory.
        job_dir = path.parent
        try:
            if (
                job_dir.is_dir()
                and not job_dir.is_symlink()
                and job_dir.parent == self._root
            ):
                try:
                    next(job_dir.iterdir())
                except StopIteration:
                    with suppress(OSError):
                        job_dir.rmdir()
        except OSError:
            return

    def _published_dir(self) -> Path:
        published = self._resolve_under_root("published")
        try:
            self._mkdir_private(published, exist_ok=True)
        except DownloadError:
            raise
        return published

    def _find_single_regular_file(
        self,
        workspace: AttemptWorkspace,
        *,
        expected_container: str,
    ) -> Path:
        """Require exactly one regular file under ``workspace.output`` only."""
        output_dir = workspace.output
        self._reject_symlink_path(output_dir, reason="OUTPUT_DIR_SYMLINK")
        found: list[Path] = []
        try:
            for entry in output_dir.iterdir():
                # Reject every unexpected entry, including hidden names.
                if entry.is_symlink():
                    raise_download_error(
                        DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT,
                        internal_reason="SYMLINK_OUTPUT",
                    )
                try:
                    st = entry.lstat()
                except OSError:
                    raise_download_error(
                        DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT,
                        internal_reason="OUTPUT_STAT_FAILED",
                    )
                if stat.S_ISDIR(st.st_mode):
                    raise_download_error(
                        DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT,
                        internal_reason="NESTED_TREE",
                    )
                if not stat.S_ISREG(st.st_mode):
                    raise_download_error(
                        DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT,
                        internal_reason="NON_REGULAR_OUTPUT",
                    )
                if st.st_nlink != 1:
                    raise_download_error(
                        DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT,
                        internal_reason="HARDLINK_OUTPUT",
                    )
                if st.st_size <= 0:
                    raise_download_error(
                        DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT,
                        internal_reason="EMPTY_ARTIFACT",
                    )
                if st.st_size > self._max_bytes:
                    raise_download_error(
                        DownloadErrorCode.DOWNLOAD_TOO_LARGE,
                        internal_reason="ARTIFACT_TOO_LARGE",
                    )
                suffix = entry.suffix.lstrip(".").lower()
                if suffix != expected_container:
                    raise_download_error(
                        DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT,
                        internal_reason="EXTENSION_MISMATCH",
                    )
                found.append(entry)
        except DownloadError:
            raise
        except OSError:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT,
                internal_reason="OUTPUT_LIST_FAILED",
            )
        if len(found) == 0:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT,
                internal_reason="EMPTY_OUTPUT",
            )
        if len(found) > 1:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT,
                internal_reason="MULTIPLE_OUTPUTS",
            )
        return found[0]

    def _open_output_dirfd(self, output_dir: Path) -> int:
        """Open the controlled output directory itself, never a link to it."""
        flags = os.O_RDONLY
        for name in ("O_DIRECTORY", "O_NOFOLLOW"):
            flags |= getattr(os, name, 0)
        try:
            fd = os.open(output_dir, flags)
        except OSError:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT,
                internal_reason="OUTPUT_DIR_OPEN_FAILED",
            )
        try:
            st = os.fstat(fd)
            if not stat.S_ISDIR(st.st_mode):
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT,
                    internal_reason="OUTPUT_DIR_NOT_DIR",
                )
            if st.st_uid != os.getuid():
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT,
                    internal_reason="OUTPUT_DIR_OWNER",
                )
            if stat.S_IMODE(st.st_mode) != 0o700:
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT,
                    internal_reason="OUTPUT_DIR_MODE",
                )
        except DownloadError:
            os.close(fd)
            raise
        except OSError:
            os.close(fd)
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT,
                internal_reason="OUTPUT_DIR_FSTAT_FAILED",
            )
        return fd

    def _open_validated_regular(self, output_dir: Path, name: str) -> int:
        """Open ``name`` under a validated output dirfd and fstat-validate it.

        No path-based ``chmod`` happens before the open: an entry swapped for a
        symlink between listing and opening fails ``O_NOFOLLOW`` instead of
        letting the caller touch the link target. Mode repair uses ``fchmod`` on
        the already-open descriptor, and identity is re-verified afterwards.
        """
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        dir_fd = self._open_output_dirfd(output_dir)
        try:
            if os.open in os.supports_dir_fd:
                fd = os.open(name, flags, dir_fd=dir_fd)
            else:  # pragma: no cover - platform fallback
                fd = os.open(output_dir / name, flags)
        except OSError:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT,
                internal_reason="OUTPUT_OPEN_FAILED",
            )
        finally:
            os.close(dir_fd)

        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT,
                    internal_reason="OUTPUT_NOT_REGULAR_AFTER_OPEN",
                )
            if before.st_nlink != 1:
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT,
                    internal_reason="HARDLINK_OUTPUT",
                )
            if before.st_uid != os.getuid():
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT,
                    internal_reason="OUTPUT_OWNER",
                )
            if before.st_size <= 0:
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT,
                    internal_reason="EMPTY_ARTIFACT",
                )
            if before.st_size > self._max_bytes:
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_TOO_LARGE,
                    internal_reason="ARTIFACT_TOO_LARGE",
                )

            os.fchmod(fd, 0o600)

            after = os.fstat(fd)
            if (after.st_ino, after.st_dev) != (before.st_ino, before.st_dev):
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT,
                    internal_reason="OUTPUT_IDENTITY_CHANGED",
                )
            if (
                not stat.S_ISREG(after.st_mode)
                or after.st_nlink != 1
                or after.st_uid != os.getuid()
                or after.st_size != before.st_size
            ):
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT,
                    internal_reason="OUTPUT_METADATA_CHANGED",
                )
            if stat.S_IMODE(after.st_mode) != 0o600:
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT,
                    internal_reason="OUTPUT_MODE",
                )
        except DownloadError:
            os.close(fd)
            raise
        except OSError:
            os.close(fd)
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT,
                internal_reason="OUTPUT_FSTAT_FAILED",
            )
        return fd

    def _sha256_fd(self, fd: int, *, max_bytes: int) -> tuple[int, str]:
        digest = hashlib.sha256()
        total = 0
        try:
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise_download_error(
                        DownloadErrorCode.DOWNLOAD_TOO_LARGE,
                        internal_reason="ARTIFACT_TOO_LARGE",
                    )
                digest.update(chunk)
        except DownloadError:
            raise
        except OSError:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT,
                internal_reason="ARTIFACT_READ_FAILED",
            )
        if total <= 0:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT,
                internal_reason="EMPTY_ARTIFACT",
            )
        return total, digest.hexdigest()

    def _fsync_dir(self, directory: Path) -> None:
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    @staticmethod
    def _write_all(fd: int, data: bytes) -> None:
        """Write every byte; ``os.write`` may accept only part of the buffer."""
        view = memoryview(data)
        written = 0
        while written < len(view):
            count = os.write(fd, view[written:])
            if count <= 0:
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                    internal_reason="WRITE_INCOMPLETE",
                )
            written += count

    def _write_file_0600(self, path: Path, data: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        try:
            self._write_all(fd, data)
            os.fsync(fd)
            os.fchmod(fd, 0o600)
            st = os.fstat(fd)
            if st.st_size != len(data):
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                    internal_reason="WRITE_SIZE_MISMATCH",
                )
        finally:
            os.close(fd)

    def _copy_fd_to_path(self, src_fd: int, dest: Path, *, expected_size: int) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        dst_fd = os.open(dest, flags, 0o600)
        try:
            os.lseek(src_fd, 0, os.SEEK_SET)
            copied = 0
            while True:
                chunk = os.read(src_fd, 1024 * 1024)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > self._max_bytes:
                    raise_download_error(
                        DownloadErrorCode.DOWNLOAD_TOO_LARGE,
                        internal_reason="ARTIFACT_TOO_LARGE",
                    )
                self._write_all(dst_fd, chunk)
            os.fsync(dst_fd)
            os.fchmod(dst_fd, 0o600)
            st = os.fstat(dst_fd)
            if copied != expected_size or st.st_size != expected_size:
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT,
                    internal_reason="ARTIFACT_COPY_SIZE_MISMATCH",
                )
        finally:
            os.close(dst_fd)

    def publish(
        self,
        *,
        workspace: AttemptWorkspace,
        expected_container: str,
        job_id: uuid.UUID,
        format_option_id: str,
        expires_at: datetime,
    ) -> PublishedArtifact:
        """Validate output/, write private manifest, atomically publish a directory."""
        container = expected_container.strip().lower()
        if container not in _ALLOWED_CONTAINERS:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT,
                internal_reason="CONTAINER_NOT_ALLOWED",
            )
        source = self._find_single_regular_file(
            workspace, expected_container=container
        )
        src_fd = self._open_validated_regular(workspace.output, source.name)
        try:
            size_bytes, sha256_hex = self._sha256_fd(
                src_fd, max_bytes=self._max_bytes
            )
            content_type = content_type_for_container(container)
            artifact_id = uuid.uuid4()
            published_dir = self._published_dir()
            partial = self._resolve_under_root(
                "published", f".partial_{artifact_id}"
            )
            final = self._resolve_under_root("published", str(artifact_id))
            if partial.exists() or final.exists():
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                    internal_reason="PUBLISH_COLLISION",
                )

            created_at = datetime.now(UTC)
            manifest = {
                "schemaVersion": _MANIFEST_SCHEMA,
                "downloadJobId": str(job_id),
                "fence": workspace.fence,
                "attempt": workspace.attempt,
                "formatOptionId": format_option_id,
                "size": size_bytes,
                "sha256": sha256_hex,
                "container": container,
                "contentType": content_type,
                "createdAt": created_at.isoformat(),
                "expiresAt": expires_at.astimezone(UTC).isoformat(),
            }
            if set(manifest) != _MANIFEST_KEYS:
                raise_download_error(
                    DownloadErrorCode.INTERNAL_ERROR,
                    internal_reason="MANIFEST_KEY_SET",
                )
            payload = json.dumps(
                manifest, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            if len(payload) > _MAX_MANIFEST_BYTES:
                raise_download_error(
                    DownloadErrorCode.INTERNAL_ERROR,
                    internal_reason="MANIFEST_TOO_LARGE",
                )

            self._mkdir_private(partial, exist_ok=False)
            artifact_path = partial / f"artifact.{container}"
            manifest_path = partial / "manifest.json"
            self._copy_fd_to_path(src_fd, artifact_path, expected_size=size_bytes)
            self._write_file_0600(manifest_path, payload)
            os.chmod(partial, 0o700)
            self._fsync_dir(partial)
            os.rename(partial, final)
            self._fsync_dir(published_dir)
        except DownloadError:
            with suppress(Exception):
                if (
                    "partial" in locals()
                    and partial.exists()
                    and not partial.is_symlink()
                ):
                    shutil.rmtree(partial, ignore_errors=True)
            raise
        except OSError:
            with suppress(Exception):
                if (
                    "partial" in locals()
                    and partial.exists()
                    and not partial.is_symlink()
                ):
                    shutil.rmtree(partial, ignore_errors=True)
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="PUBLISH_FAILED",
            )
        finally:
            with suppress(OSError):
                os.close(src_fd)

        return PublishedArtifact(
            artifact_id=artifact_id,
            size_bytes=size_bytes,
            sha256_hex=sha256_hex,
            container=container,
            content_type=content_type,
        )

    def delete_artifact(self, artifact_id: uuid.UUID) -> None:
        """Delete the published artifact directory by opaque id."""
        dest = self._resolve_under_root("published", str(artifact_id))
        try:
            resolved_root = self._root.resolve(strict=True)
            resolved = dest.resolve(strict=False)
            resolved.relative_to(resolved_root)
        except (OSError, ValueError):
            return
        if dest.is_symlink():
            return
        try:
            if dest.is_dir():
                shutil.rmtree(dest, ignore_errors=False)
            elif dest.exists():
                dest.unlink()
        except FileNotFoundError:
            return
        except OSError:
            return
        # Also remove a leftover partial staging dir for the same id.
        partial = self._resolve_under_root("published", f".partial_{artifact_id}")
        try:
            if partial.is_symlink():
                return
            if partial.is_dir():
                shutil.rmtree(partial, ignore_errors=True)
        except OSError:
            return

    def _entry_mtime(self, path: Path) -> float | None:
        try:
            return float(path.lstat().st_mtime)
        except OSError:
            return None

    def _parent_within_root(self, path: Path) -> bool:
        """Check containment using the parent so link targets never widen scope."""
        try:
            resolved_root = self._root.resolve(strict=True)
            resolved_parent = path.parent.resolve(strict=True)
            resolved_parent.relative_to(resolved_root)
        except (OSError, ValueError):
            return False
        return path.name not in {"", os.curdir, os.pardir}

    def _safe_rmtree(self, path: Path) -> bool:
        if not self._parent_within_root(path):
            return False
        # Re-stat immediately before removal: the entry may have been replaced
        # by a symlink since it was listed.
        try:
            st = os.lstat(path)
        except OSError:
            return False
        if stat.S_ISLNK(st.st_mode):
            return False
        try:
            if stat.S_ISDIR(st.st_mode):
                shutil.rmtree(path)
            else:
                os.unlink(path)
            return True
        except OSError:
            return False

    def _remove_symlink_entry(self, path: Path) -> bool:
        """Unlink a symlink entry itself; the target is never opened or followed."""
        if not self._parent_within_root(path):
            return False
        try:
            st = os.lstat(path)
        except OSError:
            return False
        if not stat.S_ISLNK(st.st_mode):
            return False
        try:
            os.unlink(path)
        except OSError:
            return False
        return True

    def _aged_out(self, path: Path, *, now: float, grace: int) -> bool:
        mtime = self._entry_mtime(path)
        return mtime is not None and now - mtime >= grace

    def _parse_manifest(self, manifest_path: Path) -> dict[str, Any] | None:
        try:
            if manifest_path.is_symlink():
                return None
            st = manifest_path.lstat()
            if not stat.S_ISREG(st.st_mode) or st.st_size > _MAX_MANIFEST_BYTES:
                return None
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(manifest_path, flags)
            try:
                raw = os.read(fd, _MAX_MANIFEST_BYTES + 1)
            finally:
                os.close(fd)
            if len(raw) > _MAX_MANIFEST_BYTES:
                return None
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None
        if not isinstance(payload, dict) or set(payload) != _MANIFEST_KEYS:
            return None
        try:
            if payload.get("schemaVersion") != _MANIFEST_SCHEMA:
                return None
            if type(payload["fence"]) is not int or type(payload["attempt"]) is not int:
                return None
            if type(payload["size"]) is not int or isinstance(payload["size"], bool):
                return None
            job_id = uuid.UUID(str(payload["downloadJobId"]))
            fence = int(payload["fence"])
            attempt = int(payload["attempt"])
            size = int(payload["size"])
            sha256 = str(payload["sha256"])
            container = str(payload["container"])
            content_type = str(payload["contentType"])
            format_option_id = str(payload["formatOptionId"])
            created_at = str(payload["createdAt"])
            expires_at = str(payload["expiresAt"])
        except (KeyError, TypeError, ValueError):
            return None
        if fence < 0 or attempt < 1 or size <= 0:
            return None
        if len(sha256) != _SHA256_HEX_LEN or any(
            ch not in "0123456789abcdef" for ch in sha256
        ):
            return None
        if container not in _ALLOWED_CONTAINERS:
            return None
        if content_type != _CONTENT_TYPES.get(container):
            return None
        if not format_option_id or len(format_option_id) > 64:
            return None
        if not created_at or not expires_at:
            return None
        return {
            "downloadJobId": job_id,
            "fence": fence,
            "attempt": attempt,
            "size": size,
            "sha256": sha256,
            "container": container,
            "contentType": content_type,
            "formatOptionId": format_option_id,
        }

    def reconcile(
        self,
        *,
        referenced_artifact_ids: frozenset[uuid.UUID],
        active_attempts: frozenset[tuple[uuid.UUID, int]],
        limit: int = 64,
    ) -> int:
        """Remove bounded stale attempt dirs and unreferenced/invalid publications.

        Keep published dirs whose artifact id is referenced by the DB, or whose
        ``(downloadJobId, fence)`` still maps to an active attempt. Delete
        unreferenced finals and invalid/partial staging dirs only after grace.
        Symlink entries are unlinked themselves after grace, never followed.
        """
        removed = 0
        grace = max(MIN_ORPHAN_GRACE_SECONDS, int(self._orphan_grace_seconds))
        now = time.time()
        limit = max(0, int(limit))

        # Attempt workspaces: root/{job_uuid}/{attempt}_{fence}/
        try:
            for job_entry in self._root.iterdir():
                if removed >= limit:
                    break
                if job_entry.name == "published":
                    continue
                if job_entry.is_symlink():
                    if self._aged_out(
                        job_entry, now=now, grace=grace
                    ) and self._remove_symlink_entry(job_entry):
                        removed += 1
                    continue
                if not job_entry.is_dir():
                    continue
                try:
                    job_uuid = uuid.UUID(job_entry.name)
                except ValueError:
                    continue
                try:
                    for attempt_entry in job_entry.iterdir():
                        if removed >= limit:
                            break
                        if attempt_entry.is_symlink():
                            if self._aged_out(
                                attempt_entry, now=now, grace=grace
                            ) and self._remove_symlink_entry(attempt_entry):
                                removed += 1
                            continue
                        if not attempt_entry.is_dir():
                            continue
                        name = attempt_entry.name
                        if "_" not in name:
                            continue
                        attempt_s, fence_s = name.rsplit("_", 1)
                        try:
                            fence = int(fence_s)
                            int(attempt_s)
                        except ValueError:
                            continue
                        if (job_uuid, fence) in active_attempts:
                            continue
                        if not self._aged_out(attempt_entry, now=now, grace=grace):
                            continue
                        if self._safe_rmtree(attempt_entry):
                            removed += 1
                    # Remove empty job dirs.
                    try:
                        next(job_entry.iterdir())
                    except StopIteration:
                        with suppress(OSError):
                            if not job_entry.is_symlink():
                                job_entry.rmdir()
                    except OSError:
                        pass
                except OSError:
                    continue
        except OSError:
            return removed

        published = self._root / "published"
        if (
            not published.is_dir()
            or published.is_symlink()
            or removed >= limit
        ):
            return removed

        try:
            for entry in published.iterdir():
                if removed >= limit:
                    break
                aged = self._aged_out(entry, now=now, grace=grace)
                if entry.is_symlink():
                    # Never a valid publication; remove the link, not its target.
                    if aged and self._remove_symlink_entry(entry):
                        removed += 1
                    continue
                if not aged:
                    continue

                name = entry.name
                if name.startswith(".partial_"):
                    if self._safe_rmtree(entry):
                        removed += 1
                    continue

                if not entry.is_dir():
                    # Unexpected non-directory litter after grace.
                    if self._safe_rmtree(entry):
                        removed += 1
                    continue

                try:
                    artifact_id = uuid.UUID(name)
                except ValueError:
                    if self._safe_rmtree(entry):
                        removed += 1
                    continue

                manifest = self._parse_manifest(entry / "manifest.json")
                if manifest is None:
                    if self._safe_rmtree(entry):
                        removed += 1
                    continue

                keep = artifact_id in referenced_artifact_ids or (
                    manifest["downloadJobId"],
                    manifest["fence"],
                ) in active_attempts
                if keep:
                    continue
                if self._safe_rmtree(entry):
                    removed += 1
        except OSError:
            return removed
        return removed

    def sweep_orphans(
        self,
        *,
        active_fences: frozenset[tuple[uuid.UUID, int]],
        limit: int = 64,
    ) -> int:
        """Thin wrapper: reconcile with an empty referenced-artifact set."""
        return self.reconcile(
            referenced_artifact_ids=frozenset(),
            active_attempts=active_fences,
            limit=limit,
        )
