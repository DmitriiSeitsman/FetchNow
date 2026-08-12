"""Cancellation and short-read ownership tests for delivery streaming."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from fetchnow.core.config import Settings
from fetchnow.delivery import routes as delivery_routes
from fetchnow.delivery.main import create_delivery_app
from fetchnow.delivery.reader import ArtifactReader, OpenArtifactHandle
from fetchnow.delivery.service import DeliveryAuthorization, DeliveryService
from fetchnow.delivery.stream import iter_fd_range
from fetchnow.downloads.artifacts import MIN_ORPHAN_GRACE_SECONDS, ArtifactStore
from fetchnow.downloads.errors import DownloadError, DownloadErrorCode
from fetchnow.downloads.models import MediaDownloadJob
from fetchnow.jobs.credentials import generate_access_token

_BARRIER_BOUND_S = 2.0


def _prepare_root(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _publish(root: Path, payload: bytes = b"abcdefghijklmnopqrstuvwxyz012345") -> dict:
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
    return {
        "job_id": job_id,
        "artifact_id": published.artifact_id,
        "expires": expires,
        "fence": 2,
        "bytes": len(payload),
        "content_type": published.content_type,
        "payload": payload,
    }


def _job_row(meta: dict[str, Any]) -> MediaDownloadJob:
    now = datetime.now(tz=UTC)
    return MediaDownloadJob(
        id=meta["job_id"],
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
        fence_token=meta["fence"],
        artifact_id=meta["artifact_id"],
        artifact_bytes=meta["bytes"],
        artifact_content_type=meta["content_type"],
        artifact_container="mp4",
        public_error_code=None,
        created_at=now,
        updated_at=now,
        completed_at=now,
        expires_at=meta["expires"],
    )


def _settings(root: Path, *, concurrency: int = 1) -> Settings:
    return Settings(
        APP_ENV="test",
        DATABASE_URL=(
            "postgresql+asyncpg://fetchnow:fetchnow@127.0.0.1:5432/fetchnow"
        ),
        MEDIA_DELIVERY_ENABLED=True,
        MEDIA_DELIVERY_ROOT=str(root),
        MEDIA_DELIVERY_CHUNK_BYTES=4096,
        MEDIA_DELIVERY_CONCURRENCY=concurrency,
        MEDIA_DELIVERY_RANGE_ENABLED=True,
    )


async def _reacquire_all(
    service: DeliveryService, *, n: int, bound_s: float = 0.5
) -> None:
    for _ in range(n):
        async with asyncio.timeout(bound_s):
            await service.semaphore.acquire()
    for _ in range(n):
        service.semaphore.release()


def _wire_app(
    app: Any,
    *,
    settings: Settings,
    service: DeliveryService,
    session_factory: Any,
) -> None:
    app.state.engine = MagicMock()
    app.state.session_factory = session_factory
    app.state.settings = settings
    app.state.delivery_service = service


@pytest.mark.asyncio
async def test_cancel_during_authorize_releases_permit(tmp_path: Path) -> None:
    root = _prepare_root(tmp_path / "root")
    meta = _publish(root)
    settings = _settings(root, concurrency=1)
    app = create_delivery_app(settings)
    reader = ArtifactReader(str(root))
    authorize_entered = asyncio.Event()
    gate = asyncio.Event()

    class _Stub(DeliveryService):
        async def authorize(self, **_kwargs: Any) -> DeliveryAuthorization:
            authorize_entered.set()
            await gate.wait()
            return DeliveryAuthorization(
                job=_job_row(meta), now=datetime.now(tz=UTC)
            )

    service = _Stub(settings, reader=reader)

    class _FakeSession:
        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    _wire_app(
        app,
        settings=settings,
        service=service,
        session_factory=lambda: _FakeSession(),
    )

    transport = ASGITransport(app=app)
    token = generate_access_token()

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        req_task = asyncio.create_task(
            client.get(
                f"/api/v1/media/download-jobs/{meta['job_id']}/content",
                headers={"Authorization": f"Bearer {token}"},
            )
        )
        async with asyncio.timeout(_BARRIER_BOUND_S):
            await authorize_entered.wait()
        req_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await req_task

    assert service.semaphore._value == 1
    await _reacquire_all(service, n=1)


@pytest.mark.asyncio
async def test_cancel_during_session_aexit_releases_permit(tmp_path: Path) -> None:
    root = _prepare_root(tmp_path / "root")
    meta = _publish(root)
    settings = _settings(root, concurrency=1)
    app = create_delivery_app(settings)
    reader = ArtifactReader(str(root))
    exit_entered = asyncio.Event()
    exit_gate = asyncio.Event()

    class _Stub(DeliveryService):
        async def authorize(self, **_kwargs: Any) -> DeliveryAuthorization:
            return DeliveryAuthorization(
                job=_job_row(meta), now=datetime.now(tz=UTC)
            )

    service = _Stub(settings, reader=reader)

    class _SlowExitSession:
        async def __aenter__(self) -> _SlowExitSession:
            return self

        async def __aexit__(self, *args: object) -> None:
            exit_entered.set()
            await exit_gate.wait()

    _wire_app(
        app,
        settings=settings,
        service=service,
        session_factory=lambda: _SlowExitSession(),
    )

    transport = ASGITransport(app=app)
    token = generate_access_token()

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        req_task = asyncio.create_task(
            client.get(
                f"/api/v1/media/download-jobs/{meta['job_id']}/content",
                headers={"Authorization": f"Bearer {token}"},
            )
        )
        async with asyncio.timeout(_BARRIER_BOUND_S):
            await exit_entered.wait()
        req_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await req_task

    assert service.semaphore._value == 1
    await _reacquire_all(service, n=1)


@pytest.mark.asyncio
async def test_cancel_after_auth_before_open_releases_permit(
    tmp_path: Path,
) -> None:
    root = _prepare_root(tmp_path / "root")
    meta = _publish(root)
    settings = _settings(root, concurrency=1)
    app = create_delivery_app(settings)
    reader = ArtifactReader(str(root))
    after_auth_entered = asyncio.Event()
    after_auth = asyncio.Event()
    open_called = {"value": False}

    class _Stub(DeliveryService):
        async def authorize(self, **_kwargs: Any) -> DeliveryAuthorization:
            authz = DeliveryAuthorization(
                job=_job_row(meta), now=datetime.now(tz=UTC)
            )
            after_auth_entered.set()
            await after_auth.wait()
            return authz

        def open_artifact(self, authz: DeliveryAuthorization) -> OpenArtifactHandle:
            open_called["value"] = True
            return super().open_artifact(authz)

    service = _Stub(settings, reader=reader)

    class _FakeSession:
        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    _wire_app(
        app,
        settings=settings,
        service=service,
        session_factory=lambda: _FakeSession(),
    )

    transport = ASGITransport(app=app)
    token = generate_access_token()
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        req_task = asyncio.create_task(
            client.get(
                f"/api/v1/media/download-jobs/{meta['job_id']}/content",
                headers={"Authorization": f"Bearer {token}"},
            )
        )
        async with asyncio.timeout(_BARRIER_BOUND_S):
            await after_auth_entered.wait()
        req_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await req_task

    assert open_called["value"] is False
    assert service.semaphore._value == 1
    await _reacquire_all(service, n=1)


@pytest.mark.asyncio
async def test_streaming_response_constructor_failure_cleans_prestream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _prepare_root(tmp_path / "root")
    meta = _publish(root)
    settings = _settings(root, concurrency=1)
    app = create_delivery_app(settings)
    reader = ArtifactReader(str(root))
    opened_fd = {"fd": -1}
    release_count = {"n": 0}

    class _Stub(DeliveryService):
        async def authorize(self, **_kwargs: Any) -> DeliveryAuthorization:
            return DeliveryAuthorization(
                job=_job_row(meta), now=datetime.now(tz=UTC)
            )

        def open_artifact(self, authz: DeliveryAuthorization) -> OpenArtifactHandle:
            handle = super().open_artifact(authz)
            opened_fd["fd"] = handle.fd
            return handle

    service = _Stub(settings, reader=reader)
    real_release = service.semaphore.release

    def counting_release() -> None:
        release_count["n"] += 1
        real_release()

    monkeypatch.setattr(service.semaphore, "release", counting_release)

    class _FakeSession:
        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    _wire_app(
        app,
        settings=settings,
        service=service,
        session_factory=lambda: _FakeSession(),
    )

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("streaming-response-ctor-failed")

    monkeypatch.setattr(delivery_routes, "StreamingResponse", boom)

    transport = ASGITransport(app=app)
    token = generate_access_token()
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/api/v1/media/download-jobs/{meta['job_id']}/content",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert opened_fd["fd"] >= 0
    with pytest.raises(OSError):
        os.fstat(opened_fd["fd"])
    assert release_count["n"] == 1
    assert service.semaphore._value == 1
    await _reacquire_all(service, n=1)
    # No orphaned delivery tasks beyond the completed request.
    pending = [
        t
        for t in asyncio.all_tasks()
        if t is not asyncio.current_task() and not t.done()
    ]
    assert pending == []


@pytest.mark.asyncio
async def test_short_read_aborts_not_clean_completion(tmp_path: Path) -> None:
    root = _prepare_root(tmp_path / "root")
    meta = _publish(root, payload=b"0123456789abcdef")
    reader = ArtifactReader(str(root))
    handle = reader.open_published(
        artifact_id=meta["artifact_id"],
        download_job_id=meta["job_id"],
        format_option_id="fmt_x",
        expected_bytes=meta["bytes"],
        expected_container="mp4",
        expected_content_type=meta["content_type"],
        expected_expires_at=meta["expires"],
        expected_fence=meta["fence"],
    )
    with pytest.raises(DownloadError) as exc:
        async for _ in iter_fd_range(
            handle, start=0, length=meta["bytes"] + 8, chunk_bytes=4
        ):
            pass
    assert exc.value.code == DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE
    with pytest.raises(OSError):
        os.fstat(handle.fd)


@pytest.mark.asyncio
async def test_range_short_read_aborts(tmp_path: Path) -> None:
    root = _prepare_root(tmp_path / "root")
    meta = _publish(root, payload=b"0123456789abcdef")
    reader = ArtifactReader(str(root))
    handle = reader.open_published(
        artifact_id=meta["artifact_id"],
        download_job_id=meta["job_id"],
        format_option_id="fmt_x",
        expected_bytes=meta["bytes"],
        expected_container="mp4",
        expected_content_type=meta["content_type"],
        expected_expires_at=meta["expires"],
        expected_fence=meta["fence"],
    )
    with pytest.raises(DownloadError) as exc:
        async for _ in iter_fd_range(
            handle, start=10, length=100, chunk_bytes=8
        ):
            pass
    assert exc.value.code == DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE
