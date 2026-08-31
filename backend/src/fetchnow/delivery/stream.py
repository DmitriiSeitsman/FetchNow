"""Bounded FD streaming helpers for private artifact delivery."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable

from fetchnow.delivery.reader import OpenArtifactHandle
from fetchnow.downloads.errors import DownloadErrorCode, raise_download_error

Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]


class MonotonicBytePacer:
    """Strict per-response byte pacing with no free initial burst.

    The virtual finish time is moved forward from the later of its previous
    value and the current monotonic time. Idle/downstream stalls therefore do
    not accumulate credit that could later be emitted as a large burst.
    """

    __slots__ = ("_clock", "_deadline", "_rate", "_sleep")

    def __init__(
        self,
        rate_bytes_per_second: int,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        if (
            type(rate_bytes_per_second) is not int
            or isinstance(rate_bytes_per_second, bool)
            or rate_bytes_per_second <= 0
        ):
            raise ValueError("rate_bytes_per_second must be a positive integer")
        self._rate = rate_bytes_per_second
        self._clock = clock
        self._sleep = sleep
        self._deadline: float | None = None

    async def pace(self, byte_count: int) -> None:
        if (
            type(byte_count) is not int
            or isinstance(byte_count, bool)
            or byte_count < 1
        ):
            raise ValueError("byte_count must be a positive integer")
        now = self._clock()
        baseline = now if self._deadline is None else max(self._deadline, now)
        self._deadline = baseline + (byte_count / self._rate)
        delay = self._deadline - self._clock()
        if delay > 0:
            await self._sleep(delay)


def _read_chunk(fd: int, offset: int, size: int) -> bytes:
    """Blocking pread of at most ``size`` bytes from ``fd`` at ``offset``."""
    return os.pread(fd, size, offset)


async def iter_fd_range(
    handle: OpenArtifactHandle,
    *,
    start: int,
    length: int,
    chunk_bytes: int,
    rate_bytes_per_second: int | None = None,
    clock: Clock = time.monotonic,
    sleep: Sleeper = asyncio.sleep,
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
    pacer = (
        None
        if rate_bytes_per_second is None
        else MonotonicBytePacer(
            rate_bytes_per_second,
            clock=clock,
            sleep=sleep,
        )
    )
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
            if pacer is not None:
                await pacer.pace(len(chunk))
            yield chunk
    finally:
        handle.close()
