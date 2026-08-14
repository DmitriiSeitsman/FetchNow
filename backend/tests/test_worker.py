"""Worker lifecycle tests for MediaJobWorkerRunner."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

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


def _poll_settings() -> Settings:
    return Settings(
        APP_ENV="test",
        LOG_LEVEL="WARNING",
        DATABASE_URL=_DB,
        MEDIA_JOBS_ENABLED=True,
        MEDIA_INSPECTION_ENABLED=True,
        MEDIA_INSPECTION_YTDLP_PATH="/usr/bin/yt-dlp",
        MEDIA_DOWNLOADS_ENABLED=False,
        WORKER_CONCURRENCY=1,
    )


def _claimed_job() -> MagicMock:
    job = MagicMock()
    job.id = uuid.uuid4()
    job.fence_token = 1
    job.provider_id = "vk"
    job.canonical_provider_url = "https://vk.com/video-1_2"
    job.media_id = "-1_2"
    job.hostname = "vk.com"
    job.path = "/video-1_2"
    job.scheme = "https"
    job.port = None
    job.attempt_count = 1
    return job


@pytest.mark.asyncio
async def test_poll_claims_queued_job_after_successful_expiry() -> None:
    """One poll iteration expires due jobs then claims; no IntegrityError."""
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.commit = AsyncMock()
    session_factory = MagicMock(return_value=session)
    repo = MagicMock()
    repo.database_now = AsyncMock(return_value=datetime.now(tz=UTC))
    repo.reclaim_expired_leases = AsyncMock(return_value=0)
    repo.expire_due_jobs = AsyncMock(return_value=1)
    queued = _claimed_job()
    repo.claim_next = AsyncMock(return_value=[queued])
    claimed = asyncio.Event()

    async def _set_claimed(_snap: _ClaimedSnapshot) -> None:
        claimed.set()

    runner = MediaJobWorkerRunner(
        _poll_settings(),
        engine=MagicMock(dispose=AsyncMock()),
        session_factory=session_factory,
        http_client=MagicMock(aclose=AsyncMock()),
    )
    runner._run_claimed = AsyncMock(side_effect=_set_claimed)  # type: ignore[method-assign]

    with patch(
        "fetchnow.jobs.worker_loop.MediaJobRepository",
        return_value=repo,
    ):
        await runner._poll_once()
        await asyncio.wait_for(claimed.wait(), timeout=1.0)

    repo.expire_due_jobs.assert_awaited()
    repo.claim_next.assert_awaited()
    session.commit.assert_awaited()
    assert not isinstance(repo.expire_due_jobs.side_effect, IntegrityError)


@pytest.mark.asyncio
async def test_expiry_integrity_error_would_skip_claim() -> None:
    """Poison expire IntegrityError must not be treated as success."""
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.commit = AsyncMock()
    session_factory = MagicMock(return_value=session)
    repo = MagicMock()
    repo.database_now = AsyncMock(return_value=datetime.now(tz=UTC))
    repo.reclaim_expired_leases = AsyncMock(return_value=0)
    repo.expire_due_jobs = AsyncMock(
        side_effect=IntegrityError("ck_media_jobs_result_state", {}, Exception())
    )
    repo.claim_next = AsyncMock(return_value=[])

    runner = MediaJobWorkerRunner(
        _poll_settings(),
        engine=MagicMock(dispose=AsyncMock()),
        session_factory=session_factory,
        http_client=MagicMock(aclose=AsyncMock()),
    )

    with (
        patch(
            "fetchnow.jobs.worker_loop.MediaJobRepository",
            return_value=repo,
        ),
        pytest.raises(IntegrityError),
    ):
        await runner._poll_once()

    session.commit.assert_not_awaited()
    repo.claim_next.assert_not_awaited()
