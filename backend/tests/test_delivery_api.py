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
from fetchnow.delivery.stream import MonotonicBytePacer
from fetchnow.downloads.artifacts import MIN_ORPHAN_GRACE_SECONDS, ArtifactStore
from fetchnow.downloads.errors import DownloadError, DownloadErrorCode
from fetchnow.downloads.models import MediaDownloadJob
from fetchnow.jobs.credentials import generate_access_token
from fetchnow.premium.policy import PremiumCapability
from fetchnow.quota.policy import effective_download_policy
from fetchnow.quota.repository import QuotaRepository


def _settings(root: Path, **overrides: Any) -> Settings:
    kwargs = {
        "APP_ENV": "test",
        "DATABASE_URL": "postgresql+asyncpg://unused@127.0.0.1:5432/unused",
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
async def test_suffix_and_open_ended_ranges_preserve_http_contract(
    delivery_client: Any,
) -> None:
    client, meta, _service = delivery_client
    token = generate_access_token()
    suffix = await client.get(
        f"/api/v1/media/download-jobs/{meta['job_id']}/content",
        headers={"Authorization": f"Bearer {token}", "Range": "bytes=-4"},
    )
    assert suffix.status_code == 206
    assert suffix.content == meta["payload"][-4:]
    assert suffix.headers["content-length"] == "4"
    assert suffix.headers["content-range"] == (
        f"bytes {meta['bytes'] - 4}-{meta['bytes'] - 1}/{meta['bytes']}"
    )

    opened = await client.get(
        f"/api/v1/media/download-jobs/{meta['job_id']}/content",
        headers={"Authorization": f"Bearer {token}", "Range": "bytes=5-"},
    )
    assert opened.status_code == 206
    assert opened.content == meta["payload"][5:]
    assert opened.headers["content-length"] == str(meta["bytes"] - 5)
    assert opened.headers["content-range"] == (
        f"bytes 5-{meta['bytes'] - 1}/{meta['bytes']}"
    )


@pytest.mark.asyncio
async def test_bearer_full_and_fresh_ranges_use_trusted_pacer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _prepare_root(tmp_path / "paced-root")
    meta = _publish(root, payload=b"0123456789abcdef")
    settings = _settings(
        root,
        FREE_DELIVERY_RATE_LIMIT_ENABLED=True,
        FREE_DELIVERY_RATE_BYTES_PER_SECOND=524_288,
    )
    app = create_delivery_app(settings)
    reader = ArtifactReader(str(root))

    class _StubDelivery(DeliveryService):
        async def authorize(self, **_kwargs: Any) -> DeliveryAuthorization:
            return DeliveryAuthorization(job=_job_row(meta), now=datetime.now(tz=UTC))

    class _FakeSession:
        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    service = _StubDelivery(settings, reader=reader)
    app.state.engine = MagicMock()
    app.state.session_factory = lambda: _FakeSession()
    app.state.settings = settings
    app.state.delivery_service = service
    paced: list[int] = []

    async def record_pace(self: MonotonicBytePacer, byte_count: int) -> None:
        paced.append(byte_count)

    monkeypatch.setattr(MonotonicBytePacer, "pace", record_pace)
    transport = ASGITransport(app=app)
    token = generate_access_token()
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        full = await client.get(
            f"/api/v1/media/download-jobs/{meta['job_id']}/content?rate=unlimited",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Tier": "premium",
                "Cookie": "delivery_rate=unlimited",
            },
        )
        assert full.status_code == 200
        for _ in range(2):
            partial = await client.get(
                f"/api/v1/media/download-jobs/{meta['job_id']}/content",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Range": "bytes=0-3",
                    "X-Tier": "premium",
                },
            )
            assert partial.status_code == 206
            assert partial.content == b"0123"

    assert paced == [16, 4, 4]


@pytest.mark.asyncio
async def test_premium_admission_snapshot_bypasses_product_pacer(
    delivery_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, meta, service = delivery_client
    expires_at = datetime.now(tz=UTC) + timedelta(hours=1)
    premium_policy = effective_download_policy(
        service._settings,  # noqa: SLF001 - focused policy boundary test
        PremiumCapability(True, expires_at, "premium_24h"),
    )

    async def authorize(self: DeliveryService, **_kwargs: Any) -> DeliveryAuthorization:
        return DeliveryAuthorization(
            job=_job_row(meta),
            now=datetime.now(tz=UTC),
            policy=premium_policy,
        )

    paced: list[int] = []

    async def record_pace(self: MonotonicBytePacer, byte_count: int) -> None:
        paced.append(byte_count)

    monkeypatch.setattr(type(service), "authorize", authorize)
    monkeypatch.setattr(MonotonicBytePacer, "pace", record_pace)
    response = await client.get(
        f"/api/v1/media/download-jobs/{meta['job_id']}/content",
        headers={"Authorization": f"Bearer {generate_access_token()}"},
    )
    assert response.status_code == 200
    assert response.content == meta["payload"]
    assert paced == []


@pytest.mark.asyncio
async def test_full_range_and_resume_delivery_do_not_touch_quota(
    delivery_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, meta, _service = delivery_client

    def forbidden_quota(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("artifact delivery must not mutate quota")

    monkeypatch.setattr(QuotaRepository, "__init__", forbidden_quota)
    token = generate_access_token()
    path = f"/api/v1/media/download-jobs/{meta['job_id']}/content"
    headers = {"Authorization": f"Bearer {token}"}
    full = await client.get(path, headers=headers)
    first = await client.get(path, headers={**headers, "Range": "bytes=0-3"})
    resumed = await client.get(path, headers={**headers, "Range": "bytes=4-"})
    assert full.status_code == 200
    assert first.status_code == 206
    assert resumed.status_code == 206
    assert first.content + resumed.content == meta["payload"]


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
            "postgresql+asyncpg://unused@127.0.0.1:5432/unused"
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
