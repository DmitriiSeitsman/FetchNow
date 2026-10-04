"""Worker flag, fail-closed client, and publication fence."""

from __future__ import annotations

import asyncio
import socket
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from fetchnow.core.config import Settings
from fetchnow.downloads.errors import DownloadError, DownloadErrorCode
from fetchnow.downloads.executor import (
    DownloadClaimSnapshot,
    DownloadExecutor,
    _LeaseLostError,
    offline_mux_backend,
)
from fetchnow.media_executor.client import ExecutorCallError, UnixExecutorClient
from fetchnow.media_executor.constants import OUTPUT_MUX, WORKER_UID
from fetchnow.media_executor.layout import job_directory
from fetchnow.media_executor.protocol import Request
from fetchnow.media_executor.server import ExecutorApp
from fetchnow.media_inspection.protocols import ProcessResult

_DB = "postgresql+asyncpg://fetchnow:fetchnow@localhost:5432/fetchnow"


def test_flag_off_keeps_inprocess_backend() -> None:
    settings = Settings(APP_ENV="test", DATABASE_URL=_DB)
    assert settings.media_executor_enabled is False
    assert offline_mux_backend(settings) == "inprocess"


def test_flag_on_requires_absolute_paths() -> None:
    with pytest.raises(ValueError, match="MEDIA_EXECUTOR_SOCKET"):
        Settings(
            APP_ENV="test",
            DATABASE_URL=_DB,
            MEDIA_EXECUTOR_ENABLED=True,
        )


def test_missing_socket_is_unavailable(tmp_path: Path) -> None:
    client = UnixExecutorClient(tmp_path / "missing.sock")
    with pytest.raises(ExecutorCallError) as exc:
        asyncio.run(
            client.reserve(
                job_id="11111111-1111-4111-8111-111111111111",
                attempt=1,
                fence=1,
            )
        )
    assert exc.value.code == "unavailable"


def test_client_roundtrip_and_unauthorized_uid(tmp_path: Path) -> None:
    # macOS rejects long AF_UNIX paths. Keep this name short.
    path = Path(f"/tmp/fn-{uuid.uuid4().hex[:8]}.sock")
    seen: list[int] = []

    def _peer(_conn: socket.socket) -> int:
        seen.append(1)
        return WORKER_UID if len(seen) == 1 else 0

    app = ExecutorApp(
        work_root=tmp_path / "work",
        runner=MagicMock(),
        ffmpeg="/usr/bin/ffmpeg",
        ffprobe="/usr/bin/ffprobe",
        socket_dir=tmp_path,
        peer_lookup=_peer,
    )
    (tmp_path / "work").mkdir()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    thread = threading.Thread(target=app.serve, args=(sock,), daemon=True)
    thread.start()
    client = UnixExecutorClient(path)
    asyncio.run(
        client.reserve(
            job_id="11111111-1111-4111-8111-111111111111",
            attempt=1,
            fence=2,
        )
    )
    with pytest.raises(ExecutorCallError) as denied:
        asyncio.run(
            client.reserve(
                job_id="11111111-1111-4111-8111-111111111111",
                attempt=1,
                fence=3,
            )
        )
    assert denied.value.code == "protocol"
    sock.close()
    path.unlink(missing_ok=True)


def _snapshot() -> DownloadClaimSnapshot:
    now = datetime.now(UTC)
    return DownloadClaimSnapshot(
        job_id=uuid.UUID("11111111-1111-4111-8111-111111111111"),
        media_job_id=uuid.uuid4(),
        fence=7,
        attempt_count=1,
        format_option_id="fmt_muxaaaaaaaaaaaaaaaaaaaaaa",
        provider_id="vk",
        canonical_provider_url="https://vk.com/video-1_2",
        media_id="-1_2",
        hostname="vk.com",
        path="/video-1_2",
        scheme="https",
        port=443,
        selected_format_snapshot={},
        expires_at=now + timedelta(hours=1),
    )


def _executor(tmp_path: Path, client: object) -> DownloadExecutor:
    settings = Settings(
        APP_ENV="test",
        DATABASE_URL=_DB,
        MEDIA_DOWNLOADS_ENABLED=True,
        MEDIA_MUXING_ENABLED=True,
        MEDIA_EXECUTOR_ENABLED=True,
        MEDIA_EXECUTOR_SOCKET=str(tmp_path / "worker.sock"),
        MEDIA_EXECUTOR_WORK_ROOT=str(tmp_path / "work"),
        MEDIA_DOWNLOAD_TEMP_ROOT=str(tmp_path / "artifacts"),
    )
    executor = DownloadExecutor(
        settings,
        session_factory=MagicMock(),
        inspection_service=MagicMock(),
        inspection_registry=MagicMock(),
        artifact_store=MagicMock(),
        executor_client=client,  # type: ignore[arg-type]
        worker_id="test-worker",
    )
    executor._advance_progress = AsyncMock()  # type: ignore[method-assign]
    executor._finish_tool_stage = MagicMock()  # type: ignore[method-assign]
    return executor


def test_unavailable_executor_does_not_fall_back(tmp_path: Path) -> None:
    class _Down:
        async def reserve(self, **_kwargs: object) -> None:
            raise ExecutorCallError("unavailable")

        async def release(self, **_kwargs: object) -> None:
            return None

    executor = _executor(tmp_path, _Down())
    workspace = SimpleNamespace(mux=tmp_path / "mux")
    video = tmp_path / "video"
    audio = tmp_path / "audio"
    video.write_bytes(b"v")
    audio.write_bytes(b"a")
    with pytest.raises(DownloadError) as exc:
        asyncio.run(
            executor._mux_and_probe_via_executor(
                _snapshot(),
                workspace,  # type: ignore[arg-type]
                video_path=video,
                audio_path=audio,
                container="mp4",
                max_bytes=1000,
            )
        )
    assert exc.value.code is DownloadErrorCode.MUXING_FAILED
    assert exc.value.internal_reason == "EXECUTOR_UNAVAILABLE"


def test_stale_fence_is_not_copied_into_the_attempt(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    snap = _snapshot()
    request = Request(
        op="reserve",
        job_id=str(snap.job_id),
        attempt=snap.attempt_count,
        fence=snap.fence,
    )
    job_dir = job_directory(work, request)

    class _Ok:
        async def reserve(self, **_kwargs: object) -> None:
            job_dir.mkdir()

        async def mux_copy(self, **_kwargs: object) -> ProcessResult:
            return ProcessResult(0, b"", b"", False, False)

        async def ffprobe_validate(self, **_kwargs: object) -> ProcessResult:
            (job_dir / OUTPUT_MUX).write_bytes(b"muxed")
            return ProcessResult(0, b"{}", b"", False, False)

        async def release(self, **_kwargs: object) -> None:
            return None

        async def cancel(self, **_kwargs: object) -> None:
            return None

    executor = _executor(tmp_path, _Ok())
    executor.lease_still_owned = AsyncMock(return_value=False)  # type: ignore[method-assign]
    mux = tmp_path / "mux"
    mux.mkdir()
    video = tmp_path / "video"
    audio = tmp_path / "audio"
    video.write_bytes(b"v")
    audio.write_bytes(b"a")
    with pytest.raises(_LeaseLostError):
        asyncio.run(
            executor._mux_and_probe_via_executor(
                snap,
                SimpleNamespace(mux=mux),  # type: ignore[arg-type]
                video_path=video,
                audio_path=audio,
                container="mp4",
                max_bytes=1000,
            )
        )
    assert not (mux / "artifact.mp4").exists()


def test_rpc_failure_requests_remote_cancel(tmp_path: Path) -> None:
    client = SimpleNamespace(cancel=AsyncMock())
    executor = _executor(tmp_path, client)
    executor._watch_lease_during_run = AsyncMock()  # type: ignore[method-assign]

    async def failed() -> ProcessResult:
        raise ExecutorCallError("protocol")

    with pytest.raises(ExecutorCallError):
        asyncio.run(executor._call_executor(_snapshot(), failed))
    client.cancel.assert_awaited_once_with(
        job_id=str(_snapshot().job_id), attempt=1, fence=7
    )
