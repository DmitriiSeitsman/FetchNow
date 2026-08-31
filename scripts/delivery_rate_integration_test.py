#!/usr/bin/env python3
"""Container-friendly measured integration for PRD1E-B3 artifact pacing."""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from fetchnow.delivery.reader import OpenArtifact, OpenArtifactHandle
from fetchnow.delivery.stream import iter_fd_range

RATE = 524_288
CHUNK = 65_536
FULL_BYTES = 262_144
RANGE_BYTES = 131_072


@dataclass(frozen=True)
class Measurement:
    label: str
    byte_count: int
    elapsed: float


def _handle(path: Path, *, byte_count: int, container: str) -> OpenArtifactHandle:
    path.write_bytes(b"x" * byte_count)
    return OpenArtifactHandle(
        OpenArtifact(
            fd=os.open(path, os.O_RDONLY),
            size_bytes=byte_count,
            content_type="video/mp4" if container == "mp4" else "video/webm",
            container=container,
            artifact_id=uuid.uuid4(),
            download_job_id=uuid.uuid4(),
            format_option_id=f"integration-{container}",
            sha256_hex="0" * 64,
        )
    )


async def _measure(
    root: Path,
    *,
    label: str,
    byte_count: int,
    start: int,
    length: int,
    container: str,
    rate: int | None,
) -> Measurement:
    handle = _handle(root / f"{label}.bin", byte_count=byte_count, container=container)
    received = 0
    started = time.monotonic()
    async for chunk in iter_fd_range(
        handle,
        start=start,
        length=length,
        chunk_bytes=CHUNK,
        rate_bytes_per_second=rate,
    ):
        received += len(chunk)
    elapsed = time.monotonic() - started
    if received != length:
        raise AssertionError(f"{label}: received {received}, expected {length}")
    return Measurement(label=label, byte_count=received, elapsed=elapsed)


def _assert_near_rate(measurement: Measurement) -> None:
    expected = measurement.byte_count / RATE
    if not expected * 0.80 <= measurement.elapsed <= expected * 2.40:
        raise AssertionError(
            f"{measurement.label}: elapsed={measurement.elapsed:.3f}s "
            f"outside generous expected window around {expected:.3f}s"
        )


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="fetchnow-b3-rate-") as raw:
        root = Path(raw)
        progressive = await _measure(
            root,
            label="progressive-full",
            byte_count=FULL_BYTES,
            start=0,
            length=FULL_BYTES,
            container="mp4",
            rate=RATE,
        )
        muxed = await _measure(
            root,
            label="muxed-full",
            byte_count=FULL_BYTES,
            start=0,
            length=FULL_BYTES,
            container="webm",
            rate=RATE,
        )
        ranged = await _measure(
            root,
            label="progressive-range",
            byte_count=FULL_BYTES,
            start=CHUNK,
            length=RANGE_BYTES,
            container="mp4",
            rate=RATE,
        )
        unlimited = await _measure(
            root,
            label="unlimited-full",
            byte_count=FULL_BYTES,
            start=0,
            length=FULL_BYTES,
            container="mp4",
            rate=None,
        )

    for measurement in (progressive, muxed, ranged):
        _assert_near_rate(measurement)
    if unlimited.elapsed >= min(progressive.elapsed, muxed.elapsed) * 0.40:
        raise AssertionError("unlimited path was not materially faster")

    for measurement in (progressive, muxed, ranged, unlimited):
        throughput = measurement.byte_count / max(measurement.elapsed, 1e-9)
        print(
            f"{measurement.label}: bytes={measurement.byte_count} "
            f"elapsed={measurement.elapsed:.3f}s throughput={throughput:.0f}B/s"
        )
    print("OK: PRD1E-B3 measured artifact pacing")


if __name__ == "__main__":
    asyncio.run(main())
