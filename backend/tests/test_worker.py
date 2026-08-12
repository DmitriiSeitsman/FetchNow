"""Worker lifecycle tests for MediaJobWorkerRunner."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fetchnow.core.config import Settings
from fetchnow.jobs.worker_loop import MediaJobWorkerRunner, _ClaimedSnapshot
from fetchnow.worker import run_worker

_DB = "postgresql+asyncpg://fetchnow:fetchnow@localhost:5432/fetchnow"


@pytest.mark.asyncio
async def test_worker_graceful_shutdown() -> None:
    settings = Settings(
        APP_ENV="test",
        LOG_LEVEL="WARNING",
        DATABASE_URL=_DB,
        WORKER_POLL_INTERVAL_SECONDS=0.05,
        MEDIA_JOBS_ENABLED=False,
    )
    stop_event = asyncio.Event()

    with patch("fetchnow.worker.get_settings", return_value=settings):
        task = asyncio.create_task(run_worker(stop_event=stop_event))
        await asyncio.sleep(0.08)
        stop_event.set()
        await asyncio.wait_for(task, timeout=1.0)
        assert task.done()
        assert task.exception() is None


@pytest.mark.asyncio
async def test_media_job_worker_stop_event() -> None:
    settings = Settings(
        APP_ENV="test",
        LOG_LEVEL="WARNING",
        DATABASE_URL=_DB,
        WORKER_POLL_INTERVAL_SECONDS=0.05,
        MEDIA_JOBS_ENABLED=False,
    )
    stop_event = asyncio.Event()
    engine = MagicMock()
    engine.dispose = AsyncMock()
    session_factory = MagicMock()

    runner = MediaJobWorkerRunner(
        settings,
        stop_event=stop_event,
        engine=engine,
        session_factory=session_factory,
        http_client=MagicMock(aclose=AsyncMock()),
    )
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(0.08)
    stop_event.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert task.done()
    assert task.exception() is None
    engine.dispose.assert_not_awaited()  # owns_engine=False


@pytest.mark.asyncio
async def test_disabled_media_jobs_does_not_claim() -> None:
    settings = Settings(
        APP_ENV="test",
        LOG_LEVEL="WARNING",
        DATABASE_URL=_DB,
        WORKER_POLL_INTERVAL_SECONDS=0.05,
        MEDIA_JOBS_ENABLED=False,
        MEDIA_INSPECTION_ENABLED=False,
    )
    stop_event = asyncio.Event()
    engine = MagicMock()
    engine.dispose = AsyncMock()
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.commit = AsyncMock()
    session_factory = MagicMock(return_value=session)

    claim_mock = AsyncMock(return_value=[])
    with patch(
        "fetchnow.jobs.worker_loop.MediaJobRepository.claim_next",
        new=claim_mock,
    ):
        runner = MediaJobWorkerRunner(
            settings,
            stop_event=stop_event,
            engine=engine,
            session_factory=session_factory,
            http_client=MagicMock(aclose=AsyncMock()),
        )
        task = asyncio.create_task(runner.run())
        await asyncio.sleep(0.12)
        stop_event.set()
        await asyncio.wait_for(task, timeout=1.0)

    claim_mock.assert_not_called()


@pytest.mark.asyncio
async def test_lease_loss_cancels_inflight_inspection() -> None:
    settings = Settings(
        APP_ENV="test",
        LOG_LEVEL="WARNING",
        DATABASE_URL=_DB,
        MEDIA_JOBS_ENABLED=True,
        MEDIA_INSPECTION_ENABLED=False,
    )
    runner = MediaJobWorkerRunner(
        settings,
        engine=MagicMock(dispose=AsyncMock()),
        session_factory=MagicMock(),
        http_client=MagicMock(aclose=AsyncMock()),
    )
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_inspection(_snapshot: _ClaimedSnapshot) -> dict[str, object]:
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()
        return {}

    runner._execute_inspection = blocked_inspection  # type: ignore[method-assign]
    runner._lease_heartbeat = AsyncMock(return_value=False)  # type: ignore[method-assign]
    snap = _ClaimedSnapshot(
        job_id=uuid.uuid4(),
        fence=1,
        provider_id="vk",
        canonical_provider_url="https://vk.com/video-1_2",
        media_id="-1_2",
        hostname="vk.com",
        path="/video-1_2",
        scheme="https",
        port=None,
        attempt_count=1,
    )

    await runner._run_claimed(snap)

    assert started.is_set()
    assert cancelled.is_set()
