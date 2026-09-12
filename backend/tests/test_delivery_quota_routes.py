"""Transport-level delivery-accounting lifecycle tests without PostgreSQL."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from fetchnow.core.config import Settings
from fetchnow.delivery import routes as delivery_routes
from fetchnow.delivery.main import create_delivery_app
from fetchnow.delivery.reader import ArtifactReader
from fetchnow.delivery.service import DeliveryAuthorization, DeliveryService
from fetchnow.downloads.artifacts import MIN_ORPHAN_GRACE_SECONDS, ArtifactStore
from fetchnow.downloads.browser_grant_tokens import (
    COOKIE_NAME,
    generate_grant_token,
)
from fetchnow.downloads.models import MediaDownloadJob
from fetchnow.jobs.credentials import generate_access_token
from fetchnow.quota.delivery import (
    DeliveryAccountingAttempt,
    DeliveryAccountingResult,
    DeliveryQuotaAccounting,
)


class _FakeSession:
    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _RecordingAccounting(DeliveryQuotaAccounting):
    def __init__(self, *, fail_begin: bool = False, fail_checkpoint: bool = False):
        super().__init__(compatibility_mode=False)
        self.fail_begin = fail_begin
        self.fail_checkpoint = fail_checkpoint
        self.begins: list[tuple[int, int]] = []
        self.checkpoints: list[int] = []
        self.finalizations: list[int] = []

    async def begin(self, **kwargs: Any) -> DeliveryAccountingAttempt | None:
        if self.fail_begin:
            raise RuntimeError("accounting unavailable")
        start, end = int(kwargs["start"]), int(kwargs["end"])
        self.begins.append((start, end))
        return DeliveryAccountingAttempt(uuid.uuid4(), start, end)

    async def checkpoint(self, **kwargs: Any) -> DeliveryAccountingResult:
        observed = int(kwargs["observed_end"])
        self.checkpoints.append(observed)
        if self.fail_checkpoint:
            raise RuntimeError("checkpoint unavailable")
        return DeliveryAccountingResult()

    async def finalize(self, **kwargs: Any) -> DeliveryAccountingResult:
        self.finalizations.append(int(kwargs["observed_end"]))
        return DeliveryAccountingResult()


def _prepare_root(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _wire(
    tmp_path: Path,
    accounting: _RecordingAccounting,
    *,
    payload: bytes = b"abcdefghijklmnopqrstuvwxyz0123456789",
) -> tuple[Any, dict[str, Any]]:
    root = _prepare_root(tmp_path / "root")
    store = ArtifactStore(
        root=str(root),
        max_bytes=1024 * 1024,
        orphan_grace_seconds=MIN_ORPHAN_GRACE_SECONDS,
    )
    job_id = uuid.uuid4()
    workspace = store.create_attempt_workspace(job_id=job_id, attempt=1, fence=2)
    (workspace.output / "artifact.mp4").write_bytes(payload)
    expires = datetime.now(tz=UTC) + timedelta(hours=1)
    published = store.publish(
        workspace=workspace,
        expected_container="mp4",
        job_id=job_id,
        format_option_id="fmt_x",
        expires_at=expires,
    )
    metadata = {
        "job_id": job_id,
        "artifact_id": published.artifact_id,
        "expires": expires,
        "bytes": len(payload),
        "payload": payload,
    }
    settings = Settings(
        APP_ENV="test",
        DATABASE_URL="postgresql+asyncpg://unused@127.0.0.1:5432/unused",
        MEDIA_DELIVERY_ENABLED=True,
        MEDIA_BROWSER_DELIVERY_ENABLED=True,
        MEDIA_DELIVERY_ROOT=str(root),
        MEDIA_DELIVERY_CHUNK_BYTES=4096,
        MEDIA_DELIVERY_RANGE_ENABLED=True,
    )
    app = create_delivery_app(settings)
    reader = ArtifactReader(str(root))

    def job() -> MediaDownloadJob:
        now = datetime.now(tz=UTC)
        return MediaDownloadJob(
            id=job_id,
            media_job_id=uuid.uuid4(),
            schema_version=1,
            public_state="ready",
            format_option_id="fmt_x",
            provider_id="vk",
            canonical_provider_url="https://vk.com/video-1_2",
            media_id="-1_2",
            hostname="vk.com",
            path="/video-1_2",
            scheme="https",
            port=None,
            selected_format_snapshot={"formatOptionId": "fmt_x"},
            attempt_count=1,
            max_attempts=3,
            available_at=now,
            fence_token=2,
            artifact_id=published.artifact_id,
            artifact_bytes=len(payload),
            artifact_content_type=published.content_type,
            artifact_container="mp4",
            public_error_code=None,
            created_at=now,
            updated_at=now,
            completed_at=now,
            expires_at=expires,
        )

    class _StubDelivery(DeliveryService):
        async def authorize(self, **_kwargs: Any) -> DeliveryAuthorization:
            return DeliveryAuthorization(job=job(), now=datetime.now(tz=UTC))

        async def authorize_browser_grant(
            self, **_kwargs: Any
        ) -> DeliveryAuthorization:
            return DeliveryAuthorization(job=job(), now=datetime.now(tz=UTC))

    app.state.engine = MagicMock()
    app.state.session_factory = lambda: _FakeSession()
    app.state.settings = settings
    app.state.delivery_service = _StubDelivery(settings, reader=reader)
    app.state.delivery_quota_accounting = accounting
    return app, metadata


def _bearer_headers(**extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {generate_access_token()}", **extra}


@pytest.mark.asyncio
async def test_get_and_range_finalize_only_observed_bytes(tmp_path: Path) -> None:
    accounting = _RecordingAccounting()
    app, meta = _wire(tmp_path, accounting)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/api/v1/media/download-jobs/{meta['job_id']}/content",
            headers=_bearer_headers(Range="bytes=3-7"),
        )
    assert response.status_code == 206
    assert response.content == meta["payload"][3:8]
    assert accounting.begins == [(3, 8)]
    assert accounting.finalizations == [8]


@pytest.mark.asyncio
async def test_head_and_unsatisfiable_range_create_no_evidence(tmp_path: Path) -> None:
    accounting = _RecordingAccounting()
    app, meta = _wire(tmp_path, accounting)
    path = f"/api/v1/media/download-jobs/{meta['job_id']}/content"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        head = await client.head(path, headers=_bearer_headers())
        rejected = await client.get(
            path,
            headers=_bearer_headers(Range="bytes=999-1000"),
        )
        multipart = await client.get(
            path,
            headers=_bearer_headers(Range="bytes=0-1,3-4"),
        )
        malformed = await client.get(
            path,
            headers=_bearer_headers(Range="not-a-range"),
        )
    assert head.status_code == 200
    assert rejected.status_code == 416
    assert multipart.status_code == 400
    assert malformed.status_code == 400
    assert accounting.begins == []
    assert accounting.finalizations == []


@pytest.mark.asyncio
async def test_begin_failure_returns_no_artifact_body(tmp_path: Path) -> None:
    accounting = _RecordingAccounting(fail_begin=True)
    app, meta = _wire(tmp_path, accounting)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/api/v1/media/download-jobs/{meta['job_id']}/content",
            headers=_bearer_headers(),
        )
    assert response.status_code == 500
    assert response.content != meta["payload"]
    assert accounting.finalizations == []


@pytest.mark.asyncio
async def test_required_checkpoint_failure_stops_stream_at_durable_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    accounting = _RecordingAccounting(fail_checkpoint=True)
    app, meta = _wire(tmp_path, accounting, payload=b"x" * 8192)
    monkeypatch.setattr(delivery_routes, "DELIVERY_CHECKPOINT_BYTES", 4096)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(RuntimeError, match="checkpoint unavailable"):
            await client.get(
                f"/api/v1/media/download-jobs/{meta['job_id']}/content",
                headers=_bearer_headers(),
            )
    assert accounting.checkpoints == [4096]
    assert accounting.finalizations == [4096]


@pytest.mark.asyncio
async def test_early_eof_does_not_finalize_requested_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    accounting = _RecordingAccounting()
    app, meta = _wire(tmp_path, accounting)

    async def short_stream(*_args: Any, **_kwargs: Any) -> AsyncIterator[bytes]:
        yield b"abc"

    monkeypatch.setattr(delivery_routes, "iter_fd_range", short_stream)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/api/v1/media/download-jobs/{meta['job_id']}/content",
            headers=_bearer_headers(),
        )
    assert response.content == b"abc"
    assert accounting.begins == [(0, meta["bytes"])]
    assert accounting.finalizations == [3]


@pytest.mark.asyncio
async def test_browser_grant_route_uses_same_accounting_component(
    tmp_path: Path,
) -> None:
    accounting = _RecordingAccounting()
    app, meta = _wire(tmp_path, accounting)
    grant_id = uuid.uuid4()
    path = f"/api/v1/media/browser-grants/{grant_id}/content"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            path,
            headers={"Cookie": f"{COOKIE_NAME}={generate_grant_token()}"},
        )
    assert response.status_code == 200
    assert response.content == meta["payload"]
    assert accounting.begins == [(0, meta["bytes"])]
    assert accounting.finalizations == [meta["bytes"]]


@pytest.mark.asyncio
async def test_finalization_is_cancellation_shielded() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    class _ShieldProbe(_RecordingAccounting):
        async def finalize(self, **kwargs: Any) -> DeliveryAccountingResult:
            entered.set()
            await release.wait()
            finished.set()
            return await super().finalize(**kwargs)

    accounting = _ShieldProbe()
    attempt = DeliveryAccountingAttempt(uuid.uuid4(), 0, 10)
    task = asyncio.create_task(
        delivery_routes._finalize_accounting(  # noqa: SLF001 - lifecycle boundary
            accounting=accounting,
            attempt=attempt,
            observed_end=4,
            session_factory=lambda: _FakeSession(),  # type: ignore[arg-type]
        )
    )
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
    assert accounting.finalizations == [4]


@pytest.mark.asyncio
async def test_finalization_timeout_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _NeverFinalizes(_RecordingAccounting):
        async def finalize(self, **_kwargs: Any) -> DeliveryAccountingResult:
            await asyncio.Event().wait()
            return DeliveryAccountingResult()  # pragma: no cover

    monkeypatch.setattr(delivery_routes, "DELIVERY_FINALIZE_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(TimeoutError):
        await delivery_routes._finalize_accounting(  # noqa: SLF001
            accounting=_NeverFinalizes(),
            attempt=DeliveryAccountingAttempt(uuid.uuid4(), 0, 10),
            observed_end=4,
            session_factory=lambda: _FakeSession(),  # type: ignore[arg-type]
        )
