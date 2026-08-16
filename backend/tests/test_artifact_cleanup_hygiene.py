"""Worker hygiene: expire clears DB pointers; reconcile retries physical delete."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from fetchnow.core.config import Settings
from fetchnow.jobs.worker_loop import MediaJobWorkerRunner

_DB = "postgresql+asyncpg://fetchnow:fetchnow@localhost:5432/fetchnow"


def _settings() -> Settings:
    return Settings(
        APP_ENV="test",
        LOG_LEVEL="WARNING",
        DATABASE_URL=_DB,
        MEDIA_JOBS_ENABLED=False,
        MEDIA_DOWNLOADS_ENABLED=True,
        MEDIA_INSPECTION_ENABLED=False,
        MEDIA_BROWSER_DELIVERY_ENABLED=False,
        WORKER_POLL_INTERVAL_SECONDS=0.05,
        MEDIA_DOWNLOAD_TEMP_ROOT="/tmp/fetchnow-test-unused",
        MEDIA_DOWNLOAD_ORPHAN_GRACE_SECONDS=60,
    )


@pytest.mark.asyncio
async def test_hygiene_failed_delete_still_runs_reconcile_retry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    artifact_id = uuid.uuid4()
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.commit = AsyncMock()
    session_factory = MagicMock(return_value=session)

    repo = MagicMock()
    repo.database_now = AsyncMock(return_value=datetime.now(tz=UTC))
    repo.reclaim_expired_leases = AsyncMock(return_value=0)
    repo.expire_due_jobs = AsyncMock(return_value=[artifact_id])
    repo.list_active_fences = AsyncMock(return_value=frozenset())
    repo.list_referenced_artifact_ids = AsyncMock(return_value=frozenset())

    store = MagicMock()
    store.delete_artifact = MagicMock(return_value=False)
    store.reconcile = MagicMock(return_value=1)
    executor = MagicMock()
    executor.artifact_store = store

    runner = MediaJobWorkerRunner(
        _settings(),
        engine=MagicMock(dispose=AsyncMock()),
        session_factory=session_factory,
        http_client=MagicMock(aclose=AsyncMock()),
    )
    runner._ensure_download_executor = MagicMock(return_value=executor)  # type: ignore[method-assign]
    runner._active_download = {}

    with (
        pytest.MonkeyPatch.context() as mp,
        caplog.at_level(logging.WARNING, logger="fetchnow.jobs.worker_loop"),
    ):
        mp.setattr(
            "fetchnow.jobs.worker_loop.MediaDownloadJobRepository",
            lambda _session: repo,
        )
        await runner._hygiene_download_jobs()

    store.delete_artifact.assert_called_once_with(artifact_id)
    store.reconcile.assert_called_once()
    kwargs = store.reconcile.call_args.kwargs
    assert kwargs["referenced_artifact_ids"] == frozenset()
    assert any(
        "artifact_delete_failed" in r.getMessage()
        and "will_retry_via_reconcile=true" in r.getMessage()
        and str(artifact_id) in r.getMessage()
        for r in caplog.records
    )
    # No credential material in hygiene logs.
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "Bearer" not in joined
    assert "__Secure-fetchnow_delivery" not in joined


@pytest.mark.asyncio
async def test_hygiene_successful_expire_delete_idempotent() -> None:
    artifact_id = uuid.uuid4()
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.commit = AsyncMock()
    session_factory = MagicMock(return_value=session)

    repo = MagicMock()
    repo.database_now = AsyncMock(return_value=datetime.now(tz=UTC))
    repo.reclaim_expired_leases = AsyncMock(return_value=0)
    repo.expire_due_jobs = AsyncMock(return_value=[artifact_id])
    repo.list_active_fences = AsyncMock(return_value=frozenset())
    repo.list_referenced_artifact_ids = AsyncMock(return_value=frozenset())

    store = MagicMock()
    store.delete_artifact = MagicMock(return_value=True)
    store.reconcile = MagicMock(return_value=0)
    executor = MagicMock()
    executor.artifact_store = store

    runner = MediaJobWorkerRunner(
        _settings(),
        engine=MagicMock(dispose=AsyncMock()),
        session_factory=session_factory,
        http_client=MagicMock(aclose=AsyncMock()),
    )
    runner._ensure_download_executor = MagicMock(return_value=executor)  # type: ignore[method-assign]
    runner._active_download = {}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "fetchnow.jobs.worker_loop.MediaDownloadJobRepository",
            lambda _session: repo,
        )
        await runner._hygiene_download_jobs()
        await runner._hygiene_download_jobs()

    assert store.delete_artifact.call_count == 2
    assert store.reconcile.call_count == 2
