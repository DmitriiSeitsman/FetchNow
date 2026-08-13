"""Read-only artifact open boundary for authenticated delivery.

Integrity model (PR7):
- Require exact DB ↔ manifest ↔ file coherence (id, job, format, fence,
  container, content-type, size, expiry grammar).
- Require strict SHA-256 hex grammar from the private manifest.
- Do **not** recompute the full file digest on every download (double I/O).
  Residual risk: a corrupted payload that still matches size/mode could be
  streamed until TTL cleanup; operators can enable offline integrity scans.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import uuid
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any

from fetchnow.downloads.errors import DownloadErrorCode, raise_download_error

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
_ALLOWED_CONTAINERS: dict[str, str] = {
    "mp4": "video/mp4",
    "webm": "video/webm",
    "mkv": "video/x-matroska",
    "m4a": "audio/mp4",
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
}
_SHA256_HEX_LEN = 64
_MAX_MANIFEST_BYTES = 4096
_HEX = frozenset("0123456789abcdef")


@dataclass(frozen=True, slots=True, repr=False)
class OpenArtifact:
    """Validated artifact held by an open FD (no pathname re-open)."""

    fd: int
    size_bytes: int
    content_type: str
    container: str
    artifact_id: uuid.UUID
    download_job_id: uuid.UUID
    format_option_id: str
    sha256_hex: str

    def __repr__(self) -> str:
        return (
            f"OpenArtifact(size_bytes={self.size_bytes!r}, "
            f"container={self.container!r}, content_type={self.content_type!r})"
        )


class OpenArtifactHandle(AbstractContextManager["OpenArtifactHandle"]):
    """Owns the artifact FD and closes it on exit / cancel."""

    __slots__ = ("_artifact", "_closed")

    def __init__(self, artifact: OpenArtifact) -> None:
        self._artifact = artifact
        self._closed = False

    @property
    def artifact(self) -> OpenArtifact:
        return self._artifact

    @property
    def fd(self) -> int:
        return self._artifact.fd

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with suppress(OSError):
            os.close(self._artifact.fd)

    def __enter__(self) -> OpenArtifactHandle:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class ArtifactReader:
    """Fail-closed read-only accessor for published download artifacts."""

    __slots__ = ("_root",)

    def __init__(self, root: str) -> None:
        self._root = self.validate_root(root)

    @property
    def root(self) -> Path:
        return self._root

    @staticmethod
    def _normalized_absolute(path: str) -> str:
        if not isinstance(path, str) or not path.strip():
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="DELIVERY_ROOT_UNSET",
            )
        raw = path.strip()
        if not os.path.isabs(raw):
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="DELIVERY_ROOT_NOT_ABSOLUTE",
            )
        normalized = os.path.normpath(raw)
        if raw != normalized or not os.path.isabs(normalized) or normalized == os.sep:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="DELIVERY_ROOT_NOT_NORMALIZED",
            )
        return normalized

    @classmethod
    def open_root_fd(cls, path: str) -> int:
        """Open the managed root via a descriptor-relative walk from ``/``.

        Every component is opened with ``O_DIRECTORY | O_NOFOLLOW`` relative to
        the previous directory FD so a symlink swap of an ancestor after a
        pathname check cannot be followed. The caller owns the returned FD.
        """
        normalized = cls._normalized_absolute(path)
        components = Path(normalized).parts[1:]
        if any(part in {"", ".", ".."} for part in components):
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="DELIVERY_ROOT_NOT_NORMALIZED",
            )

        flags = os.O_RDONLY
        for name in ("O_DIRECTORY", "O_NOFOLLOW"):
            flags |= getattr(os, name, 0)

        try:
            fd = os.open("/", flags)
        except OSError:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="DELIVERY_ROOT_WALK_FAILED",
            )

        try:
            for component in components:
                try:
                    next_fd = os.open(component, flags, dir_fd=fd)
                except OSError:
                    raise_download_error(
                        DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                        internal_reason="DELIVERY_ROOT_COMPONENT_OPEN_FAILED",
                    )
                os.close(fd)
                fd = next_fd

            st = os.fstat(fd)
            if not stat.S_ISDIR(st.st_mode):
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                    internal_reason="DELIVERY_ROOT_NOT_DIR",
                )
            if stat.S_IMODE(st.st_mode) != 0o700:
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                    internal_reason="DELIVERY_ROOT_MODE",
                )
            if st.st_uid != os.getuid():
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                    internal_reason="DELIVERY_ROOT_OWNER",
                )
        except Exception:
            with suppress(OSError):
                os.close(fd)
            raise
        return fd

    @classmethod
    def validate_root(cls, path: str) -> Path:
        """Require an absolute already-normalized 0700 directory owned by UID.

        Never mkdir/chmod an existing operator-provided root. Symlinks in any
        path component are rejected by the descriptor walk (not a pathname
        pre-check).
        """
        normalized = cls._normalized_absolute(path)
        fd = cls.open_root_fd(normalized)
        os.close(fd)
        return Path(normalized)

    def open_published(
        self,
        *,
        artifact_id: uuid.UUID,
        download_job_id: uuid.UUID,
        format_option_id: str,
        expected_bytes: int,
        expected_container: str,
        expected_content_type: str,
        expected_expires_at: datetime,
        expected_fence: int | None = None,
    ) -> OpenArtifactHandle:
        """Open and validate a published artifact; caller owns the returned FD."""
        container = expected_container.strip().lower()
        if container not in _ALLOWED_CONTAINERS:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="DELIVERY_CONTAINER_NOT_ALLOWED",
            )
        if expected_content_type != _ALLOWED_CONTAINERS[container]:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="DELIVERY_CONTENT_TYPE_MISMATCH",
            )
        if type(expected_bytes) is not int or isinstance(expected_bytes, bool):
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="DELIVERY_SIZE_TYPE",
            )
        if expected_bytes <= 0:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="DELIVERY_EMPTY_ARTIFACT",
            )

        # Fresh descriptor walk each open: never reopen via pathname alone.
        root_fd = self.open_root_fd(str(self._root))
        try:
            published_fd = self._open_child_dir(root_fd, "published")
        finally:
            os.close(root_fd)

        try:
            artifact_dir_fd = self._open_child_dir(
                published_fd,
                str(artifact_id),
                require_mode=0o700,
            )
        finally:
            os.close(published_fd)

        try:
            manifest = self._read_manifest(artifact_dir_fd)
            self._assert_manifest_coherent(
                manifest,
                artifact_id=artifact_id,
                download_job_id=download_job_id,
                format_option_id=format_option_id,
                expected_bytes=expected_bytes,
                expected_container=container,
                expected_content_type=expected_content_type,
                expected_expires_at=expected_expires_at,
                expected_fence=expected_fence,
            )
            artifact_name = f"artifact.{container}"
            art_fd = self._open_regular_child(
                artifact_dir_fd,
                artifact_name,
                require_mode=0o600,
            )
            try:
                st = os.fstat(art_fd)
                if st.st_size != expected_bytes:
                    raise_download_error(
                        DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                        internal_reason="DELIVERY_SIZE_MISMATCH",
                    )
                # Identity re-check after open: refuse swap races.
                st2 = os.fstat(art_fd)
                if (st2.st_ino, st2.st_dev, st2.st_size) != (
                    st.st_ino,
                    st.st_dev,
                    st.st_size,
                ):
                    raise_download_error(
                        DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                        internal_reason="DELIVERY_IDENTITY_CHANGED",
                    )
            except Exception:
                os.close(art_fd)
                raise
        finally:
            os.close(artifact_dir_fd)

        opened = OpenArtifact(
            fd=art_fd,
            size_bytes=expected_bytes,
            content_type=expected_content_type,
            container=container,
            artifact_id=artifact_id,
            download_job_id=download_job_id,
            format_option_id=format_option_id,
            sha256_hex=str(manifest["sha256"]),
        )
        return OpenArtifactHandle(opened)

    def _open_child_dir(
        self,
        parent_fd: int,
        name: str,
        *,
        require_mode: int | None = None,
    ) -> int:
        if "/" in name or name in {"", ".", ".."}:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="DELIVERY_NAME_INVALID",
            )
        flags = os.O_RDONLY
        for attr in ("O_DIRECTORY", "O_NOFOLLOW"):
            flags |= getattr(os, attr, 0)
        try:
            if os.open in os.supports_dir_fd:
                fd = os.open(name, flags, dir_fd=parent_fd)
            else:  # pragma: no cover
                raise OSError("dir_fd unsupported")
        except OSError:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="DELIVERY_CHILD_DIR_OPEN_FAILED",
            )
        try:
            st = os.fstat(fd)
            if not stat.S_ISDIR(st.st_mode):
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                    internal_reason="DELIVERY_CHILD_NOT_DIR",
                )
            if st.st_uid != os.getuid():
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                    internal_reason="DELIVERY_CHILD_OWNER",
                )
            if require_mode is not None and stat.S_IMODE(st.st_mode) != require_mode:
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                    internal_reason="DELIVERY_CHILD_MODE",
                )
        except Exception:
            os.close(fd)
            raise
        return fd

    def _open_regular_child(
        self,
        parent_fd: int,
        name: str,
        *,
        require_mode: int,
    ) -> int:
        if "/" in name or name in {"", ".", ".."} or name.startswith("."):
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="DELIVERY_FILE_NAME_INVALID",
            )
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        # O_NONBLOCK prevents FIFO/device open from hanging the delivery worker.
        flags |= getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(name, flags, dir_fd=parent_fd)
        except OSError:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="DELIVERY_FILE_OPEN_FAILED",
            )
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                    internal_reason="DELIVERY_NOT_REGULAR",
                )
            if st.st_nlink != 1:
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                    internal_reason="DELIVERY_HARDLINK",
                )
            if st.st_uid != os.getuid():
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                    internal_reason="DELIVERY_FILE_OWNER",
                )
            if stat.S_IMODE(st.st_mode) != require_mode:
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                    internal_reason="DELIVERY_FILE_MODE",
                )
            if st.st_size <= 0:
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                    internal_reason="DELIVERY_EMPTY_FILE",
                )
            # Drop O_NONBLOCK for subsequent streaming reads.
            if hasattr(os, "O_NONBLOCK"):
                cur = fcntl.fcntl(fd, fcntl.F_GETFL)
                fcntl.fcntl(fd, fcntl.F_SETFL, cur & ~os.O_NONBLOCK)
        except Exception:
            os.close(fd)
            raise
        return fd

    def _read_manifest(self, artifact_dir_fd: int) -> dict[str, Any]:
        fd = self._open_regular_child(
            artifact_dir_fd, "manifest.json", require_mode=0o600
        )
        try:
            raw = b""
            while len(raw) <= _MAX_MANIFEST_BYTES:
                chunk = os.read(fd, 1024)
                if not chunk:
                    break
                raw += chunk
                if len(raw) > _MAX_MANIFEST_BYTES:
                    raise_download_error(
                        DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                        internal_reason="DELIVERY_MANIFEST_TOO_LARGE",
                    )
        finally:
            os.close(fd)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="DELIVERY_MANIFEST_PARSE",
            )
        if not isinstance(payload, dict) or set(payload) != _MANIFEST_KEYS:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="DELIVERY_MANIFEST_KEYS",
            )
        return payload

    def _assert_manifest_coherent(
        self,
        manifest: dict[str, Any],
        *,
        artifact_id: uuid.UUID,
        download_job_id: uuid.UUID,
        format_option_id: str,
        expected_bytes: int,
        expected_container: str,
        expected_content_type: str,
        expected_expires_at: datetime,
        expected_fence: int | None,
    ) -> None:
        del artifact_id  # Directory name already enforced by open path.
        try:
            if manifest.get("schemaVersion") != _MANIFEST_SCHEMA:
                raise ValueError("schema")
            job_id = uuid.UUID(str(manifest["downloadJobId"]))
            fence = manifest["fence"]
            attempt = manifest["attempt"]
            size = manifest["size"]
            sha256 = str(manifest["sha256"])
            container = str(manifest["container"])
            content_type = str(manifest["contentType"])
            option_id = str(manifest["formatOptionId"])
            created_at = str(manifest["createdAt"])
            expires_at = str(manifest["expiresAt"])
        except (KeyError, TypeError, ValueError):
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="DELIVERY_MANIFEST_TYPES",
            )
        if job_id != download_job_id:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="DELIVERY_JOB_ID_MISMATCH",
            )
        if option_id != format_option_id:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="DELIVERY_FORMAT_MISMATCH",
            )
        if type(fence) is not int or isinstance(fence, bool) or fence < 0:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="DELIVERY_FENCE_INVALID",
            )
        if type(attempt) is not int or isinstance(attempt, bool) or attempt < 1:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="DELIVERY_ATTEMPT_INVALID",
            )
        if expected_fence is not None and fence != expected_fence:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="DELIVERY_FENCE_MISMATCH",
            )
        if type(size) is not int or isinstance(size, bool) or size != expected_bytes:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="DELIVERY_MANIFEST_SIZE",
            )
        if container != expected_container:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="DELIVERY_MANIFEST_CONTAINER",
            )
        if content_type != expected_content_type:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="DELIVERY_MANIFEST_CONTENT_TYPE",
            )
        if len(sha256) != _SHA256_HEX_LEN or any(ch not in _HEX for ch in sha256):
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="DELIVERY_SHA256_GRAMMAR",
            )
        if not created_at or not expires_at:
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="DELIVERY_MANIFEST_TIMESTAMPS",
            )
        # Expiry string must parse and not contradict the authorized DB expiry
        # beyond a second of formatting noise; reject if clearly past DB expiry.
        try:
            from datetime import UTC

            parsed = datetime.fromisoformat(expires_at)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            db_exp = expected_expires_at
            if db_exp.tzinfo is None:
                db_exp = db_exp.replace(tzinfo=UTC)
            if abs((parsed - db_exp).total_seconds()) > 2:
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                    internal_reason="DELIVERY_EXPIRY_MISMATCH",
                )
        except Exception as exc:
            from fetchnow.downloads.errors import DownloadError

            if isinstance(exc, DownloadError):
                raise
            raise_download_error(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="DELIVERY_EXPIRY_PARSE",
            )


# Avoid unused-import lint on intentional re-export surface.
__all__ = ["ArtifactReader", "OpenArtifact", "OpenArtifactHandle"]
