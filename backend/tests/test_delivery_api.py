"""Authenticated delivery API tests (offline)."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from fetchnow.core.config import Settings
from fetchnow.delivery.main import create_delivery_app
from fetchnow.delivery.reader import ArtifactReader
from fetchnow.delivery.service import DeliveryAuthorization, DeliveryService
from fetchnow.downloads.artifacts import MIN_ORPHAN_GRACE_SECONDS, ArtifactStore
from fetchnow.downloads.errors import DownloadError, DownloadErrorCode
from fetchnow.downloads.models import MediaDownloadJob
from fetchnow.jobs.credentials import generate_access_token


def _settings(root: Path, **overrides: Any) -> Settings:
    kwargs = {
        "APP_ENV": "test",
        "DATABASE_URL": "postgresql+asyncpg://fetchnow:fetchnow@127.0.0.1:5432/fetchnow",
        "MEDIA_DELIVERY_ENABLED": True,
        "MEDIA_DELIVERY_ROOT": str(root),
        "MEDIA_DELIVERY_CHUNK_BYTES": 4096,
        "MEDIA_DELIVERY_CONCURRENCY": 2,
        "MEDIA_DELIVERY_RANGE_ENABLED": True,
    }
    kwargs.update(overrides)
    return Settings(**kwargs)


def _prepare_root(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _publish(
    root: Path, payload: bytes = b"abcdefghijklmnopqrstuvwxyz"
) -> dict[str, Any]:
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


@pytest.fixture
async def delivery_client(tmp_path: Path):
    root = _prepare_root(tmp_path / "root")
    meta = _publish(root)
    settings = _settings(root)
    app = create_delivery_app(settings)
    reader = ArtifactReader(str(root))

    class _StubDelivery(DeliveryService):
        async def authorize(self, **_kwargs: Any) -> DeliveryAuthorization:
            return DeliveryAuthorization(
                job=_job_row(meta), now=datetime.now(tz=UTC)
            )

    service: DeliveryService = _StubDelivery(settings, reader=reader)

    class _FakeSession:
        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    def _fake_factory() -> _FakeSession:
        return _FakeSession()

    # Bypass real DB: delivery routes only need a session context around authorize.
    app.state.engine = MagicMock()
    app.state.session_factory = _fake_factory
    app.state.settings = settings
    app.state.delivery_service = service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, meta, service


@pytest.mark.asyncio
async def test_correct_bearer_streams(delivery_client: Any) -> None:
    client, meta, _service = delivery_client
    token = generate_access_token()
    response = await client.get(
        f"/api/v1/media/download-jobs/{meta['job_id']}/content",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.content == meta["payload"]
    assert response.headers["content-length"] == str(meta["bytes"])
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "attachment;" in response.headers["content-disposition"]
    assert str(meta["artifact_id"]) not in response.text
    assert "published" not in response.text
    assert token not in response.text
    assert token not in response.headers.get("content-disposition", "")


@pytest.mark.asyncio
async def test_missing_bearer_indistinguishable(delivery_client: Any) -> None:
    client, meta, _service = delivery_client
    response = await client.get(
        f"/api/v1/media/download-jobs/{meta['job_id']}/content"
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DOWNLOAD_JOB_NOT_FOUND"
    assert response.headers.get("cache-control") == "no-store"


@pytest.mark.asyncio
async def test_wrong_bearer_indistinguishable(
    delivery_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _meta, service = delivery_client

    async def deny(self: Any, **_kwargs: Any) -> DeliveryAuthorization:
        raise DownloadError(DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND)

    monkeypatch.setattr(type(service), "authorize", deny)
    response = await client.get(
        f"/api/v1/media/download-jobs/{uuid.uuid4()}/content",
        headers={"Authorization": f"Bearer {generate_access_token()}"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DOWNLOAD_JOB_NOT_FOUND"


@pytest.mark.asyncio
async def test_head_mirrors_headers_no_body(delivery_client: Any) -> None:
    client, meta, _service = delivery_client
    response = await client.head(
        f"/api/v1/media/download-jobs/{meta['job_id']}/content",
        headers={"Authorization": f"Bearer {generate_access_token()}"},
    )
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-length"] == str(meta["bytes"])
    assert response.headers["accept-ranges"] == "bytes"


@pytest.mark.asyncio
async def test_single_range_partial(delivery_client: Any) -> None:
    client, meta, _service = delivery_client
    response = await client.get(
        f"/api/v1/media/download-jobs/{meta['job_id']}/content",
        headers={
            "Authorization": f"Bearer {generate_access_token()}",
            "Range": "bytes=0-4",
        },
    )
    assert response.status_code == 206
    assert response.content == meta["payload"][:5]
    assert response.headers["content-range"] == f"bytes 0-4/{meta['bytes']}"
    assert response.headers["content-length"] == "5"


@pytest.mark.asyncio
async def test_multiple_ranges_rejected(delivery_client: Any) -> None:
    client, meta, _service = delivery_client
    response = await client.get(
        f"/api/v1/media/download-jobs/{meta['job_id']}/content",
        headers={
            "Authorization": f"Bearer {generate_access_token()}",
            "Range": "bytes=0-1,2-3",
        },
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_unsatisfiable_range(delivery_client: Any) -> None:
    client, meta, _service = delivery_client
    response = await client.get(
        f"/api/v1/media/download-jobs/{meta['job_id']}/content",
        headers={
            "Authorization": f"Bearer {generate_access_token()}",
            "Range": f"bytes={meta['bytes']}-",
        },
    )
    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{meta['bytes']}"


@pytest.mark.asyncio
async def test_disabled_delivery_returns_catalog(tmp_path: Path) -> None:
    settings = Settings(
        APP_ENV="test",
        DATABASE_URL=(
            "postgresql+asyncpg://fetchnow:fetchnow@127.0.0.1:5432/fetchnow"
        ),
        MEDIA_DELIVERY_ENABLED=False,
    )
    app = create_delivery_app(settings)

    class _FakeSession:
        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    app.state.engine = MagicMock()
    app.state.session_factory = lambda: _FakeSession()
    app.state.settings = settings
    app.state.delivery_service = DeliveryService(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/api/v1/media/download-jobs/{uuid.uuid4()}/content",
            headers={"Authorization": f"Bearer {generate_access_token()}"},
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DELIVERY_DISABLED"


@pytest.mark.asyncio
async def test_chunks_are_bounded(delivery_client: Any) -> None:
    client, meta, service = delivery_client
    assert service.chunk_bytes == 4096
    response = await client.get(
        f"/api/v1/media/download-jobs/{meta['job_id']}/content",
        headers={"Authorization": f"Bearer {generate_access_token()}"},
    )
    assert response.status_code == 200
    assert len(response.content) == meta["bytes"]


@pytest.mark.asyncio
async def test_no_artifact_id_or_path_in_error(
    delivery_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, meta, service = delivery_client

    async def boom(self: Any, **_kwargs: Any) -> DeliveryAuthorization:
        raise DownloadError(
            DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
            internal_reason="READY_POINTER_INCOMPLETE",
        )

    monkeypatch.setattr(type(service), "authorize", boom)
    response = await client.get(
        f"/api/v1/media/download-jobs/{meta['job_id']}/content",
        headers={"Authorization": f"Bearer {generate_access_token()}"},
    )
    blob = response.text
    assert str(meta["artifact_id"]) not in blob
    assert "READY_POINTER" not in blob
    assert "/published/" not in blob
