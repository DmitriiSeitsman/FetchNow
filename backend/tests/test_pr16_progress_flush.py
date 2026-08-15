"""PR16: final observation flush and force-persist before stage transition."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from fetchnow.downloads.byte_progress import ProgressPersistGate, progress_percent
from fetchnow.downloads.process_download import (
    CoalescingProgressWriter,
    DownloadProcessRunner,
    build_sanitized_env,
)


@pytest.mark.asyncio
async def test_coalescing_writer_flush_drains_final_sample() -> None:
    seen: list[int] = []

    async def persist(value: int) -> None:
        await asyncio.sleep(0)
        seen.append(value)

    writer = CoalescingProgressWriter(persist)
    writer.observe(100)
    writer.observe(250)
    await writer.flush()
    assert seen == [250]
    await writer.aclose()
    writer.observe(999)
    await writer.flush()
    assert seen == [250]


@pytest.mark.asyncio
async def test_final_force_bypasses_throttle_once() -> None:
    writes: list[int] = []
    clock = {"t": 0.0}

    gate = ProgressPersistGate(clock=lambda: clock["t"], min_interval_seconds=10.0)

    async def persist(percent: int) -> bool:
        writes.append(percent)
        return True

    await gate.ingest(
        observed_bytes=1_000,
        expected_bytes=10_000,
        persist=persist,
    )
    assert writes == [10]
    clock["t"] = 0.1
    await gate.ingest(
        observed_bytes=5_000,
        expected_bytes=10_000,
        persist=persist,
    )
    # Throttled — no second write yet.
    assert writes == [10]
    await gate.ingest(
        observed_bytes=9_000,
        expected_bytes=10_000,
        persist=persist,
        force=True,
    )
    assert writes == [10, 90]
    assert gate.persisted_update_count == 2
    assert gate.observed_sample_count == 3


@pytest.mark.asyncio
async def test_force_does_not_regress_or_write_without_denominator() -> None:
    writes: list[int] = []
    gate = ProgressPersistGate()

    async def persist(percent: int) -> bool:
        writes.append(percent)
        return True

    await gate.ingest(
        observed_bytes=5_000,
        expected_bytes=10_000,
        persist=persist,
        force=True,
    )
    assert writes == [50]
    await gate.ingest(
        observed_bytes=2_000,
        expected_bytes=10_000,
        persist=persist,
        force=True,
    )
    assert writes == [50]
    await gate.ingest(
        observed_bytes=9_000,
        expected_bytes=None,
        persist=persist,
        force=True,
    )
    assert writes == [50]


@pytest.mark.asyncio
async def test_ownership_lost_blocks_force_flush() -> None:
    writes: list[int] = []
    gate = ProgressPersistGate()

    async def persist(percent: int) -> bool:
        writes.append(percent)
        return False

    await gate.ingest(
        observed_bytes=1_000,
        expected_bytes=10_000,
        persist=persist,
        force=True,
    )
    assert gate.ownership_lost is True
    await gate.ingest(
        observed_bytes=9_000,
        expected_bytes=10_000,
        persist=persist,
        force=True,
    )
    assert writes == [10]


def test_diagnostic_snapshot_is_aggregate_only() -> None:
    gate = ProgressPersistGate()
    gate.observed_sample_count = 12
    gate.persisted_update_count = 4
    gate.last_persisted_percent = 77
    snap = gate.diagnostic_snapshot(expected_size_known=True)
    blob = repr(snap)
    assert "http" not in blob
    assert "token" not in blob
    assert "/tmp" not in blob
    assert snap == {
        "observed_sample_count": 12,
        "persisted_update_count": 4,
        "max_persisted_percent": 77,
        "expected_size_known": True,
    }


def test_unknown_denominator_never_fakes_percent() -> None:
    assert progress_percent(observed_bytes=100, expected_bytes=None) is None
    assert progress_percent(observed_bytes=100, expected_bytes=0) is None
    assert progress_percent(observed_bytes=100, expected_bytes=-1) is None


@pytest.mark.asyncio
async def test_short_subprocess_final_observe_reaches_persist(tmp_path: Path) -> None:
    """Even a sub-sample-interval exit still delivers a final observation."""

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    artifact = output_dir / "clip.bin"
    seen: list[int] = []

    async def persist(value: int) -> None:
        seen.append(value)

    script = tmp_path / "quick.sh"
    script.write_text(
        f'#!/bin/sh\nprintf "%s" "xxxxxxxxxxxxxxxxxxxx" > "{artifact}"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    runner = DownloadProcessRunner()
    result = await runner.run(
        [str(script)],
        env=build_sanitized_env(home_dir=str(tmp_path), tmp_dir=str(tmp_path)),
        cwd=str(tmp_path),
        timeout_seconds=5,
        stdout_limit_bytes=1024,
        stderr_limit_bytes=1024,
        output_dir=str(output_dir),
        on_observed_bytes=persist,
    )
    assert result.exit_code == 0
    assert seen
    assert max(seen) >= 20
