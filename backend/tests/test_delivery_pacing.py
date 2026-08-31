"""Deterministic PRD1E-B3 delivery pacing tests."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from fetchnow.delivery.reader import OpenArtifact, OpenArtifactHandle
from fetchnow.delivery.stream import MonotonicBytePacer, iter_fd_range


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


def _handle(path: Path, payload: bytes) -> OpenArtifactHandle:
    path.write_bytes(payload)
    fd = os.open(path, os.O_RDONLY)
    return OpenArtifactHandle(
        OpenArtifact(
            fd=fd,
            size_bytes=len(payload),
            content_type="video/mp4",
            container="mp4",
            artifact_id=uuid.uuid4(),
            download_job_id=uuid.uuid4(),
            format_option_id="fmt",
            sha256_hex="0" * 64,
        )
    )


@pytest.mark.asyncio
async def test_first_chunk_and_average_are_strictly_paced() -> None:
    clock = FakeClock()
    pacer = MonotonicBytePacer(524_288, clock=clock, sleep=clock.sleep)

    await pacer.pace(65_536)
    assert clock.sleeps == pytest.approx([0.125])

    await pacer.pace(65_536)
    await pacer.pace(32_768)
    assert sum(clock.sleeps) == pytest.approx(0.3125)


@pytest.mark.asyncio
async def test_stall_does_not_accumulate_credit() -> None:
    clock = FakeClock()
    pacer = MonotonicBytePacer(524_288, clock=clock, sleep=clock.sleep)
    await pacer.pace(65_536)

    clock.now += 10.0
    await pacer.pace(65_536)

    assert clock.sleeps[-1] == pytest.approx(0.125)


@pytest.mark.asyncio
async def test_cancellation_during_pacing_sleep_propagates() -> None:
    entered = asyncio.Event()

    async def cancelled_sleep(_delay: float) -> None:
        entered.set()
        raise asyncio.CancelledError

    pacer = MonotonicBytePacer(524_288, clock=lambda: 0.0, sleep=cancelled_sleep)
    with pytest.raises(asyncio.CancelledError):
        await pacer.pace(1)
    assert entered.is_set()


@pytest.mark.parametrize("rate", [0, -1, True, 1.5])
def test_pacer_rejects_non_positive_or_non_integer_rate(rate: object) -> None:
    with pytest.raises(ValueError):
        MonotonicBytePacer(rate)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_unlimited_iterator_bypasses_clock_and_sleep(tmp_path: Path) -> None:
    handle = _handle(tmp_path / "unlimited.mp4", b"abcdefgh")

    def forbidden_clock() -> float:
        raise AssertionError("unlimited path must not construct/use a pacer")

    async def forbidden_sleep(_delay: float) -> None:
        raise AssertionError("unlimited path must not sleep")

    chunks = [
        chunk
        async for chunk in iter_fd_range(
            handle,
            start=0,
            length=8,
            chunk_bytes=4,
            rate_bytes_per_second=None,
            clock=forbidden_clock,
            sleep=forbidden_sleep,
        )
    ]
    assert chunks == [b"abcd", b"efgh"]


@pytest.mark.asyncio
async def test_iterator_paces_partial_final_chunk_without_full_buffering(
    tmp_path: Path,
) -> None:
    payload = b"abcdefghij"
    handle = _handle(tmp_path / "paced.mp4", payload)
    clock = FakeClock()
    chunks: list[bytes] = []

    async for chunk in iter_fd_range(
        handle,
        start=0,
        length=len(payload),
        chunk_bytes=4,
        rate_bytes_per_second=4,
        clock=clock,
        sleep=clock.sleep,
    ):
        chunks.append(chunk)
        assert len(chunks) <= 3

    assert chunks == [b"abcd", b"efgh", b"ij"]
    assert clock.sleeps == pytest.approx([1.0, 1.0, 0.5])
    with pytest.raises(OSError):
        os.fstat(handle.fd)


@pytest.mark.asyncio
async def test_iterator_cancellation_closes_fd(tmp_path: Path) -> None:
    handle = _handle(tmp_path / "cancel.mp4", b"abcdefgh")
    entered = asyncio.Event()
    never = asyncio.Event()

    async def blocking_sleep(_delay: float) -> None:
        entered.set()
        await never.wait()

    task = asyncio.create_task(
        _consume(
            iter_fd_range(
                handle,
                start=0,
                length=8,
                chunk_bytes=4,
                rate_bytes_per_second=4,
                sleep=blocking_sleep,
            )
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(OSError):
        os.fstat(handle.fd)


async def _consume(iterator: AsyncIterator[bytes]) -> None:
    async for _chunk in iterator:
        pass


def test_default_clock_is_monotonic() -> None:
    defaults = MonotonicBytePacer.__init__.__kwdefaults__
    assert defaults is not None
    clock = defaults["clock"]
    assert clock is time.monotonic
