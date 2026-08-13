"""Trusted executable policy for worker-only mux tools (ffmpeg/ffprobe).

Residual risk: classic validate/exec TOCTOU between ``stat`` and spawn.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from fetchnow.downloads.errors import DownloadErrorCode, raise_download_error


def validate_trusted_executable(path: str, *, kind: str) -> str:
    """Require an absolute regular non-symlink executable under operator control."""
    reason_prefix = "EXECUTABLE"
    if kind not in {"ffmpeg", "ffprobe"}:
        raise_download_error(
            DownloadErrorCode.INTERNAL_ERROR,
            internal_reason="EXECUTABLE_KIND",
        )
    if not path or not isinstance(path, str):
        raise_download_error(
            DownloadErrorCode.MUXING_UNAVAILABLE,
            internal_reason=f"{reason_prefix}_UNSET",
        )
    if not os.path.isabs(path):
        raise_download_error(
            DownloadErrorCode.MUXING_UNAVAILABLE,
            internal_reason=f"{reason_prefix}_NOT_ABSOLUTE",
        )
    candidate = Path(path)
    try:
        st = os.lstat(candidate)
        if stat.S_ISLNK(st.st_mode):
            raise_download_error(
                DownloadErrorCode.MUXING_UNAVAILABLE,
                internal_reason=f"{reason_prefix}_IS_SYMLINK",
            )
        if not stat.S_ISREG(st.st_mode):
            raise_download_error(
                DownloadErrorCode.MUXING_UNAVAILABLE,
                internal_reason=f"{reason_prefix}_NOT_REGULAR",
            )
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise_download_error(
                DownloadErrorCode.MUXING_UNAVAILABLE,
                internal_reason=f"{reason_prefix}_WRITABLE",
            )
        if st.st_uid not in {0, os.getuid()}:
            raise_download_error(
                DownloadErrorCode.MUXING_UNAVAILABLE,
                internal_reason=f"{reason_prefix}_OWNER",
            )
        if not os.access(candidate, os.X_OK):
            raise_download_error(
                DownloadErrorCode.MUXING_UNAVAILABLE,
                internal_reason=f"{reason_prefix}_NOT_EXECUTABLE",
            )
    except OSError:
        raise_download_error(
            DownloadErrorCode.MUXING_UNAVAILABLE,
            internal_reason=f"{reason_prefix}_STAT_FAILED",
        )
    name = candidate.name.lower()
    if kind == "ffmpeg" and name != "ffmpeg":
        raise_download_error(
            DownloadErrorCode.MUXING_UNAVAILABLE,
            internal_reason="EXECUTABLE_NAME",
        )
    if kind == "ffprobe" and name != "ffprobe":
        raise_download_error(
            DownloadErrorCode.MUXING_UNAVAILABLE,
            internal_reason="EXECUTABLE_NAME",
        )
    return str(candidate)
