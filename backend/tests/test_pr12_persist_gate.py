"""Deterministic PR12 persist-gate tests with an injected monotonic clock."""

from __future__ import annotations

import pytest

from fetchnow.downloads.byte_progress import ProgressPersistGate


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


@pytest.mark.asyncio
async def test_throttled_sample_is_not_marked_persisted() -> None:
    clock = _Clock()
    gate = ProgressPersistGate(clock=clock)
    writes: list[int] = []

    async def persist(percent: int) -> bool:
        writes.append(percent)
        return True

    await gate.ingest(observed_bytes=0, expected_bytes=100, persist=persist)
    assert writes == [0]
    assert gate.last_persisted_percent == 0
    clock.t = 0.1
    await gate.ingest(observed_bytes=1, expected_bytes=100, persist=persist)
    assert writes == [0]
    assert gate.max_observed_percent == 1
    throttled_value_marked_persisted = gate.last_persisted_percent == 1
    assert throttled_value_marked_persisted is False
    assert gate.last_persisted_percent == 0


@pytest.mark.asyncio
async def test_continuously_changing_samples_persist_coalesced_value() -> None:
    clock = _Clock()
    gate = ProgressPersistGate(clock=clock)
    writes: list[int] = []

    async def persist(percent: int) -> bool:
        writes.append(percent)
        return True

    for index in range(6):
        clock.t = index * 0.1
        await gate.ingest(
            observed_bytes=index,
            expected_bytes=100,
            persist=persist,
        )
    assert writes[0] == 0
    assert 1 not in writes
    assert 2 not in writes
    continuously_changing_samples_persist = writes[-1] == 5 and len(writes) >= 2
    assert continuously_changing_samples_persist is True
    assert gate.last_persisted_percent == 5
    assert gate.max_observed_percent == 5


@pytest.mark.asyncio
async def test_failed_write_is_not_marked_persisted_and_does_not_tight_loop() -> None:
    clock = _Clock()
    gate = ProgressPersistGate(clock=clock)
    writes: list[int] = []

    async def boom(percent: int) -> bool:
        writes.append(percent)
        raise RuntimeError("transient")

    with pytest.raises(RuntimeError, match="transient"):
        await gate.ingest(observed_bytes=0, expected_bytes=100, persist=boom)
    failed_write_marked_persisted = gate.last_persisted_percent is not None
    assert failed_write_marked_persisted is False
    clock.t = 0.2
    await gate.ingest(observed_bytes=1, expected_bytes=100, persist=boom)
    db_failure_causes_tight_retry_loop = len(writes) > 1
    assert db_failure_causes_tight_retry_loop is False
    clock.t = 2.0
    with pytest.raises(RuntimeError, match="transient"):
        await gate.ingest(observed_bytes=2, expected_bytes=100, persist=boom)
    assert writes == [0, 2]


@pytest.mark.asyncio
async def test_false_fenced_write_stops_further_percent_writes() -> None:
    clock = _Clock()
    gate = ProgressPersistGate(clock=clock)
    writes: list[int] = []

    async def deny(percent: int) -> bool:
        writes.append(percent)
        return False

    await gate.ingest(observed_bytes=0, expected_bytes=100, persist=deny)
    false_fenced_write_marked_success = gate.last_persisted_percent is not None
    assert false_fenced_write_marked_success is False
    assert gate.ownership_lost is True
    clock.t = 1.0
    await gate.ingest(observed_bytes=40, expected_bytes=100, persist=deny)
    stale_progress_writer_keeps_writing = len(writes) > 1
    stale_worker_updates_progress = gate.last_persisted_percent is not None
    assert stale_progress_writer_keeps_writing is False
    assert stale_worker_updates_progress is False
    assert writes == [0]
