"""Canonical lowercase UUID4 parsing for browser-grant routes."""

from __future__ import annotations

import re
import uuid

from fetchnow.downloads.errors import DownloadErrorCode, raise_download_error

# Exact lowercase canonical UUID4 as emitted by uuid.uuid4().
CANONICAL_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

# Nginx/location fragment (no anchors): same grammar.
NGINX_CANONICAL_UUID4 = (
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)


def parse_canonical_uuid4(raw: str) -> uuid.UUID:
    """Parse a canonical lowercase UUID4 or raise catalog-safe not-found.

    Never includes the raw value in the exception message or repr path that
    reaches public clients. Does not touch the database.
    """
    if not isinstance(raw, str) or CANONICAL_UUID4_RE.fullmatch(raw) is None:
        raise_download_error(
            DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND,
            internal_reason="GRANT_ID_SHAPE",
        )
    try:
        value = uuid.UUID(raw)
    except ValueError:
        raise_download_error(
            DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND,
            internal_reason="GRANT_ID_PARSE",
        )
    if value.version != 4 or str(value) != raw:
        raise_download_error(
            DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND,
            internal_reason="GRANT_ID_NONCANONICAL",
        )
    return value
