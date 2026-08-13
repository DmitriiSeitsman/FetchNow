"""Create the managed download root on an empty shared volume.

Worker and delivery both depend on this completing successfully so an empty
named volume does not require worker-first startup. Never chmod/chown an
existing operator-provided path; never follow symlinks. Non-root only.

This entrypoint reads only non-secret env (log level and artifact paths). It
must not load application Settings: that would require DATABASE_URL.
"""

from __future__ import annotations

import logging
import os
import sys

from fetchnow.core.logging import configure_logging
from fetchnow.downloads.artifacts import ArtifactStore
from fetchnow.downloads.errors import (
    DownloadError,
    DownloadErrorCode,
    raise_download_error,
)

logger = logging.getLogger("fetchnow.downloads.storage_init")

_ALLOWED_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


def managed_download_root() -> str:
    """Resolve the worker/delivery shared artifact root from process env."""
    root = os.environ.get("MEDIA_DOWNLOAD_TEMP_ROOT", "").strip()
    if root:
        return root
    temp = os.environ.get("TEMP_STORAGE_PATH", "").strip()
    if temp:
        return os.path.join(temp, "downloads")
    raise_download_error(
        DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
        internal_reason="TEMP_ROOT_UNSET",
    )


def ensure_download_root(path: str) -> str:
    """Create the managed root if missing; validate owner/mode otherwise.

    Uses ``ArtifactStore.validate_root`` so the contract is identical to the
    worker path: mkdir 0700 only when absent, never chmod an existing root.
    """
    resolved = ArtifactStore.validate_root(path)
    return str(resolved)


def run() -> None:
    """CLI entry: prepare the shared artifact root and exit."""
    level = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    if level not in _ALLOWED_LOG_LEVELS:
        level = "INFO"
    configure_logging(level)
    try:
        root = managed_download_root()
        ensure_download_root(root)
    except DownloadError:
        logger.error("storage_init_failed")
        sys.exit(1)
    logger.info("storage_init_ready")
