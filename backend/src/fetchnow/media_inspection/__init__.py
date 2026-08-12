"""Media inspection foundation: metadata-only provider extractors.

PR4 inspects metadata for validated VK/Rutube provider URLs. It does not
download media bytes, create jobs, or expose a public download API. The
yt-dlp subprocess adapter is disabled by default until explicitly configured.
"""

from __future__ import annotations

from fetchnow.media_inspection.errors import InspectionError, InspectionErrorKind
from fetchnow.media_inspection.models import MediaFormat, MediaMetadata
from fetchnow.media_inspection.service import MediaInspectionService

__all__ = [
    "InspectionError",
    "InspectionErrorKind",
    "MediaFormat",
    "MediaInspectionService",
    "MediaMetadata",
]
