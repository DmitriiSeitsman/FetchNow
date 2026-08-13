"""Bounded FD streaming helpers for private artifact delivery."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

from fetchnow.delivery.reader import OpenArtifactHandle
from fetchnow.downloads.errors import DownloadErrorCode, raise_download_error


def _read_chunk(fd: int, offset: int, size: int) -> bytes:
    """Blocking pread of at most ``size`` bytes from ``fd`` at ``offset``."""
    return os.pread(fd, size, offset)


async def iter_fd_range(
    handle: OpenArtifactHandle,
    *,
    start: int,
    length: int,
    chunk_bytes: int,
) -> AsyncIterator[bytes]:
    """Yield fixed-size chunks from an already-open FD; always closes the FD.

    Reads run in a worker thread so the event loop is not blocked. Cancellation
    and generator close both release the FD. Early EOF before the promised
    interval aborts the stream as a storage failure (never a clean completion).
    """
    if chunk_bytes < 1:
        handle.close()
        raise ValueError("chunk_bytes must be positive")
    if start < 0 or length < 0:
        handle.close()
        raise ValueError("invalid range")
    remaining = length
    offset = start
    try:
        while remaining > 0:
            n = min(chunk_bytes, remaining)
            chunk = await asyncio.to_thread(_read_chunk, handle.fd, offset, n)
            if not chunk:
                raise_download_error(
                    DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                    internal_reason="DELIVERY_SHORT_READ",
                )
            offset += len(chunk)
            remaining -= len(chunk)
            yield chunk
    finally:
        handle.close()
