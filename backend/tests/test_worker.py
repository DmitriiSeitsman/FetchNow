"""Worker lifecycle tests."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from fetchnow.core.config import Settings
from fetchnow.worker import run_worker


@pytest.mark.asyncio
async def test_worker_graceful_shutdown() -> None:
    settings = Settings(
        APP_ENV="test",
        LOG_LEVEL="WARNING",
        WORKER_POLL_INTERVAL_SECONDS=0.05,
    )
    stop_event = asyncio.Event()

    with patch("fetchnow.worker.get_settings", return_value=settings):
        task = asyncio.create_task(run_worker(stop_event=stop_event))
        await asyncio.sleep(0.08)
        stop_event.set()
        await asyncio.wait_for(task, timeout=1.0)
        assert task.done()
        assert task.exception() is None
