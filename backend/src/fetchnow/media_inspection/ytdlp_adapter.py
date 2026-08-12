"""Hardened yt-dlp metadata-only adapter (subprocess CLI, not in-process)."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import stat
import tempfile
from pathlib import Path

from fetchnow.core.config import Settings
from fetchnow.media_inspection.errors import (
    InspectionError,
    InspectionErrorKind,
    raise_inspection_error,
)
from fetchnow.media_inspection.models import ExtractedMediaDraft
from fetchnow.media_inspection.process import (
    SubprocessProcessRunner,
    assert_env_has_no_secrets,
    build_sanitized_env,
)
from fetchnow.media_inspection.protocols import (
    InspectionTarget,
    ProcessRunner,
)
from fetchnow.media_inspection.ytdlp_argv import build_ytdlp_metadata_argv
from fetchnow.media_inspection.ytdlp_parse import parse_ytdlp_json

logger = logging.getLogger(__name__)


def validate_ytdlp_executable(path: str) -> str:
    """Require an absolute regular non-symlink executable under operator control.

    Residual risk: classic validate/exec TOCTOU between ``stat`` and spawn.
    """
    if not path or not isinstance(path, str):
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_TOOL_UNAVAILABLE,
            internal_reason="EXECUTABLE_UNSET",
        )
    if not os.path.isabs(path):
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_TOOL_UNAVAILABLE,
            internal_reason="EXECUTABLE_NOT_ABSOLUTE",
        )
    candidate = Path(path)
    try:
        if candidate.is_symlink():
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_TOOL_UNAVAILABLE,
                internal_reason="EXECUTABLE_IS_SYMLINK",
            )
        if not candidate.exists():
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_TOOL_UNAVAILABLE,
                internal_reason="EXECUTABLE_MISSING",
            )
        st = candidate.stat()
        if not stat.S_ISREG(st.st_mode):
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_TOOL_UNAVAILABLE,
                internal_reason="EXECUTABLE_NOT_REGULAR",
            )
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_TOOL_UNAVAILABLE,
                internal_reason="EXECUTABLE_WRITABLE",
            )
        if st.st_uid not in {0, os.getuid()}:
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_TOOL_UNAVAILABLE,
                internal_reason="EXECUTABLE_OWNER",
            )
        if not os.access(candidate, os.X_OK):
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_TOOL_UNAVAILABLE,
                internal_reason="EXECUTABLE_NOT_EXECUTABLE",
            )
    except OSError:
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_TOOL_UNAVAILABLE,
            internal_reason="EXECUTABLE_STAT_FAILED",
        )
    return str(candidate)


def validate_temp_root(path: str) -> str:
    """Require an absolute non-symlink operator-controlled directory."""
    if not path or not isinstance(path, str) or not path.strip():
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_TOOL_UNAVAILABLE,
            internal_reason="TEMP_ROOT_UNSET",
        )
    root = Path(path)
    if not root.is_absolute():
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_TOOL_UNAVAILABLE,
            internal_reason="TEMP_ROOT_NOT_ABSOLUTE",
        )
    try:
        if root.is_symlink():
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_TOOL_UNAVAILABLE,
                internal_reason="TEMP_ROOT_IS_SYMLINK",
            )
        if not root.exists():
            os.makedirs(root, mode=0o700, exist_ok=True)
        if root.is_symlink():
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_TOOL_UNAVAILABLE,
                internal_reason="TEMP_ROOT_IS_SYMLINK",
            )
        st = root.stat()
        if not stat.S_ISDIR(st.st_mode):
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_TOOL_UNAVAILABLE,
                internal_reason="TEMP_ROOT_NOT_DIR",
            )
        if st.st_mode & (stat.S_IWOTH):
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_TOOL_UNAVAILABLE,
                internal_reason="TEMP_ROOT_WORLD_WRITABLE",
            )
        if st.st_uid not in {0, os.getuid()}:
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_TOOL_UNAVAILABLE,
                internal_reason="TEMP_ROOT_OWNER",
            )
    except OSError:
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_TOOL_UNAVAILABLE,
            internal_reason="TEMP_ROOT_STAT_FAILED",
        )
    return str(root)


def _cleanup_workspace(tmp_dir: str, *, require_gone: bool) -> None:
    """Remove the operation workspace. Do not ignore cleanup failures silently."""
    try:
        shutil.rmtree(tmp_dir)
    except FileNotFoundError:
        return
    except OSError:
        if require_gone:
            raise_inspection_error(
                InspectionErrorKind.INTERNAL_INSPECTION_ERROR,
                internal_reason="TEMP_CLEANUP_FAILED",
            )
        logger.warning(
            "media_inspection_cleanup_failed",
            extra={"outcome": "TEMP_CLEANUP_FAILED"},
        )
        return
    if require_gone and os.path.exists(tmp_dir):
        raise_inspection_error(
            InspectionErrorKind.INTERNAL_INSPECTION_ERROR,
            internal_reason="TEMP_CLEANUP_RESIDUAL",
        )


class YtDlpMetadataExtractor:
    """Provider-scoped metadata extractor backed by a fixed yt-dlp argv."""

    def __init__(
        self,
        *,
        settings: Settings,
        allowed_extractor_keys: frozenset[str],
        extractor_id: str = "ytdlp_metadata",
        runner: ProcessRunner | None = None,
        tool_version: str | None = None,
    ) -> None:
        self._settings = settings
        self._allowed_extractor_keys = frozenset(
            k.lower() for k in allowed_extractor_keys
        )
        self._extractor_id = extractor_id
        self._runner = runner or SubprocessProcessRunner()
        self._tool_version = tool_version

    @property
    def extractor_id(self) -> str:
        return self._extractor_id

    async def extract(self, target: InspectionTarget) -> ExtractedMediaDraft:
        settings = self._settings
        if not settings.media_inspection_enabled:
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_TOOL_UNAVAILABLE,
                internal_reason="INSPECTION_DISABLED",
            )
        executable = validate_ytdlp_executable(
            settings.media_inspection_ytdlp_path
        )

        configured_root = settings.media_inspection_temp_root.strip()
        if not configured_root:
            configured_root = os.path.join(
                settings.temp_storage_path, "media_inspection"
            )
        work_root = validate_temp_root(configured_root)

        tmp_dir: str | None = None
        completed_ok = False
        try:
            tmp_dir = tempfile.mkdtemp(prefix="inspect_", dir=work_root)
            os.chmod(tmp_dir, 0o700)
            home_dir = os.path.join(tmp_dir, "home")
            cache_dir = os.path.join(tmp_dir, "cache")
            os.mkdir(home_dir, 0o700)
            os.mkdir(cache_dir, 0o700)

            argv = build_ytdlp_metadata_argv(
                executable=executable,
                url=target.tool_url,
                allowed_extractors=self._allowed_extractor_keys,
                socket_timeout_seconds=int(
                    settings.media_inspection_socket_timeout_seconds
                ),
                cache_dir=cache_dir,
            )
            env = build_sanitized_env(home_dir=home_dir, tmp_dir=tmp_dir)
            assert_env_has_no_secrets(env)

            logger.info(
                "media_inspection_start",
                extra={
                    "provider_id": target.provider_id,
                    "extractor_id": self._extractor_id,
                },
            )

            result = await self._runner.run(
                argv,
                env=env,
                cwd=tmp_dir,
                timeout_seconds=settings.media_inspection_timeout_seconds,
                stdout_limit_bytes=settings.media_inspection_stdout_max_bytes,
                stderr_limit_bytes=settings.media_inspection_stderr_max_bytes,
            )

            if result.timed_out:
                raise_inspection_error(
                    InspectionErrorKind.INSPECTION_TIMEOUT,
                    internal_reason="PROCESS_TIMEOUT",
                )
            if result.signal is not None:
                raise_inspection_error(
                    InspectionErrorKind.INSPECTION_MEDIA_UNAVAILABLE,
                    internal_reason="PROCESS_SIGNAL",
                )
            if result.exit_code != 0:
                raise_inspection_error(
                    InspectionErrorKind.INSPECTION_MEDIA_UNAVAILABLE,
                    internal_reason="PROCESS_NONZERO",
                )

            draft = parse_ytdlp_json(
                result.stdout,
                expected_provider_id=target.provider_id,
                expected_canonical_url=target.canonical_provider_url,
                expected_media_id=target.media_id,
                allowed_extractor_keys=self._allowed_extractor_keys,
                allowed_hostnames=target.allowed_hostnames,
                max_height=settings.media_inspection_max_height,
                max_width=settings.media_inspection_max_width,
                max_bytes=settings.max_source_file_bytes,
                max_duration=settings.max_source_duration_seconds,
                tool_version=self._tool_version,
                max_json_depth=settings.media_inspection_json_max_depth,
                max_json_nodes=settings.media_inspection_json_max_nodes,
                max_format_entries=settings.media_inspection_max_format_entries,
            )
            logger.info(
                "media_inspection_ok",
                extra={
                    "provider_id": target.provider_id,
                    "format_candidates": len(draft.candidates),
                },
            )
            completed_ok = True
            return draft
        except asyncio.CancelledError:
            # Must propagate cancellation; cleanup failure must not replace it.
            raise
        except InspectionError:
            raise
        finally:
            if tmp_dir is not None:
                try:
                    _cleanup_workspace(tmp_dir, require_gone=completed_ok)
                except InspectionError:
                    if completed_ok:
                        # Successful extraction must fail closed on leftover temp.
                        raise
                    logger.warning(
                        "media_inspection_cleanup_failed",
                        extra={"outcome": "TEMP_CLEANUP_FAILED"},
                    )
