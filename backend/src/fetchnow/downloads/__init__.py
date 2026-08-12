"""Durable download execution and private ephemeral artifact storage (PR6).

Feature is disabled by default. Provider format tokens are ephemeral and never
persisted. No public file delivery endpoints are exposed by this package.
"""

from __future__ import annotations

from fetchnow.downloads.artifacts import ArtifactStore, PublishedArtifact
from fetchnow.downloads.errors import (
    DownloadError,
    DownloadErrorCode,
    raise_download_error,
)
from fetchnow.downloads.models import MediaDownloadJob
from fetchnow.downloads.repository import MediaDownloadJobRepository
from fetchnow.downloads.selection import (
    ResolvedDownloadSelection,
    resolve_selection_from_draft,
)
from fetchnow.downloads.service import DownloadJobService, DownloadJobView
from fetchnow.downloads.states import MediaDownloadJobState, assert_transition
from fetchnow.downloads.ytdlp_download_argv import build_ytdlp_download_argv

__all__ = [
    "ArtifactStore",
    "DownloadError",
    "DownloadErrorCode",
    "DownloadJobService",
    "DownloadJobView",
    "MediaDownloadJob",
    "MediaDownloadJobRepository",
    "MediaDownloadJobState",
    "PublishedArtifact",
    "ResolvedDownloadSelection",
    "assert_transition",
    "build_ytdlp_download_argv",
    "raise_download_error",
    "resolve_selection_from_draft",
]
