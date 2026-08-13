"""Active lease-loss watchdog: kill descendants of yt-dlp/ffmpeg/ffprobe."""

from __future__ import annotations

import asyncio
import os
import textwrap
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from fetchnow.core.config import Settings
from fetchnow.downloads.artifacts import MIN_ORPHAN_GRACE_SECONDS, ArtifactStore
from fetchnow.downloads.executor import (
    DownloadClaimSnapshot,
    DownloadExecutor,
    lease_watch_interval_seconds,
)
from fetchnow.downloads.process_download import DownloadProcessRunner
from fetchnow.downloads.repository import MediaDownloadJobRepository
from fetchnow.downloads.selection import (
    MuxedDownloadSelection,
    ResolvedDownloadSelection,
)
from fetchnow.media_inspection.models import FormatCategory
from fetchnow.media_inspection.protocols import ProcessResult

_DB = "postgresql+asyncpg://fetchnow:fetchnow@localhost:5432/fetchnow"
_VIDEO_TOKEN = "url720-secret"
_AUDIO_TOKEN = "audio-aac-secret"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _settings(tmp_path: Path, *, mux: bool) -> Settings:
    artifacts = tmp_path / "artifacts"
    return Settings(
        APP_ENV="test",
        DATABASE_URL=_DB,
        MEDIA_DOWNLOADS_ENABLED=True,
        MEDIA_MUXING_ENABLED=mux,
        MEDIA_DOWNLOAD_MIN_FREE_BYTES=0,
        MEDIA_DOWNLOAD_TEMP_ROOT=str(artifacts),
        MEDIA_INSPECTION_YTDLP_PATH=str(tmp_path / "ytdlp.sh"),
        MEDIA_MUXING_FFMPEG_PATH=str(tmp_path / "ffmpeg.sh"),
        MEDIA_MUXING_FFPROBE_PATH=str(tmp_path / "ffprobe.sh"),
    )


def _store(root: Path) -> ArtifactStore:
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return ArtifactStore(
        root=str(root),
        max_bytes=1024 * 1024,
        orphan_grace_seconds=MIN_ORPHAN_GRACE_SECONDS,
    )


class _FakeSession:
    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        return None


def _mux_selection() -> MuxedDownloadSelection:
    return MuxedDownloadSelection(
        format_option_id="fmt_muxaaaaaaaaaaaaaaaaaaaaaa",
        container="mp4",
        width=1280,
        height=720,
        fps=30.0,
        has_video=True,
        has_audio=True,
        category=FormatCategory.PROGRESSIVE,
        quality_label="p720",
        free_tier_eligible=True,
        approx_bytes=8_400_000,
        video_approx_bytes=8_000_000,
        audio_approx_bytes=400_000,
        expected_duration_seconds=12.0,
        video_format_token=_VIDEO_TOKEN,
        audio_format_token=_AUDIO_TOKEN,
    )


def _direct_selection() -> ResolvedDownloadSelection:
    return ResolvedDownloadSelection(
        format_option_id="fmt_abc123aaaaaaaaaaaaaaaaaaaa",
        container="mp4",
        width=1280,
        height=720,
        fps=30.0,
        has_video=True,
        has_audio=True,
        category=FormatCategory.PROGRESSIVE,
        quality_label="p720",
        free_tier_eligible=True,
        approx_bytes=100,
        provider_format_token="url720",
    )


def _snap() -> DownloadClaimSnapshot:
    now = datetime.now(tz=UTC)
    return DownloadClaimSnapshot(
        job_id=uuid.uuid4(),
        media_job_id=uuid.uuid4(),
        fence=1,
        attempt_count=1,
        format_option_id="fmt_muxaaaaaaaaaaaaaaaaaaaaaa",
        provider_id="vk",
        canonical_provider_url="https://vk.com/video-1_2",
        media_id="-1_2",
        hostname="vk.com",
        path="/video-1_2",
        scheme="https",
        port=443,
        selected_format_snapshot={"formatOptionId": "fmt_muxaaaaaaaaaaaaaaaaaaaaaa"},
        expires_at=now + timedelta(hours=1),
    )


def _executor(
    settings: Settings, store: ArtifactStore
) -> DownloadExecutor:
    return DownloadExecutor(
        settings,
        session_factory=MagicMock(return_value=_FakeSession()),
        inspection_service=MagicMock(),
        inspection_registry=MagicMock(),
        artifact_store=store,
        process_runner=DownloadProcessRunner(),
        worker_id="test-worker",
    )


def _patch(executor: DownloadExecutor, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "fetchnow.downloads.executor.validate_ytdlp_executable",
        lambda path: path,
    )
    monkeypatch.setattr(
        "fetchnow.downloads.executor.validate_trusted_executable",
        lambda path, kind: path,
    )
    binding = MagicMock()
    binding.allowed_extractor_keys = frozenset({"vk"})
    executor._inspection_registry.resolve = MagicMock(return_value=binding)


def _write_exec(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _install_tools(tmp_path: Path, *, hang: str, pid_file: Path, fifo: Path) -> None:
    hang_fn = textwrap.dedent(
        f"""\
        hang() {{
          sleep 300 &
          echo $! > "{pid_file}"
          printf x > "{fifo}"
          wait
        }}
        """
    )
    ytdlp = hang_fn + textwrap.dedent(
        f"""\
        o=""
        prev=""
        for arg in "$@"; do
          if [ "$prev" = "-o" ]; then
            o="$arg"
            break
          fi
          prev="$arg"
        done
        case "$o" in
          *video/*)
            if [ "{hang}" = "video" ]; then hang; exit 0; fi
            mkdir -p video
            printf 'videobytes!!!!!!' > video/stream.mp4
            ;;
          *audio/*)
            if [ "{hang}" = "audio" ]; then hang; exit 0; fi
            mkdir -p audio
            printf 'audiobytes!!!!!!' > audio/stream.m4a
            ;;
          *output/*)
            if [ "{hang}" = "direct" ]; then hang; exit 0; fi
            mkdir -p output
            printf 'directbytes!!!!!' > output/artifact.mp4
            ;;
        esac
        """
    )
    ffmpeg = hang_fn + textwrap.dedent(
        f"""\
        if [ "{hang}" = "ffmpeg" ]; then hang; exit 0; fi
        out=""
        for arg in "$@"; do
          out="$arg"
        done
        printf 'muxed-artifact!!' > "$out"
        """
    )
    ffprobe = hang_fn + textwrap.dedent(
        """\
        if [ "HANG_STAGE" = "ffprobe" ]; then hang; exit 0; fi
        inp=""
        prev=""
        for arg in "$@"; do
          if [ "$prev" = "-i" ]; then
            inp="$arg"
            break
          fi
          prev="$arg"
        done
        size=$(wc -c < "$inp" | tr -d ' ')
        printf '%s' '{"streams":[{"codec_type":"video","codec_name":"h264"},'
        printf '%s' '{"codec_type":"audio","codec_name":"aac"}],"format":{'
        printf '%s' '"format_name":"mov,mp4,m4a,3gp,3g2,mj2",'
        printf '"duration":"12.0","size":"%s"}}' "$size"
        """
    ).replace("HANG_STAGE", hang)
    _write_exec(tmp_path / "ytdlp.sh", ytdlp)
    _write_exec(tmp_path / "ffmpeg.sh", ffmpeg)
    _write_exec(tmp_path / "ffprobe.sh", ffprobe)


async def _wait_fifo(fifo: Path) -> None:
    def _read() -> None:
        with fifo.open("rb") as handle:
            handle.read(1)

    await asyncio.to_thread(_read)


def test_lease_watch_interval_is_strictly_below_expiry() -> None:
    for lease in (5.0, 30.0, 120.0, 600.0):
        interval = lease_watch_interval_seconds(lease)
        assert 0 < interval < lease


@pytest.mark.asyncio
async def test_lease_lost_during_direct_download_kills_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _run_hang_case(tmp_path, monkeypatch, hang="direct", mux=False)


@pytest.mark.asyncio
async def test_lease_lost_during_video_download_kills_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _run_hang_case(tmp_path, monkeypatch, hang="video", mux=True)


@pytest.mark.asyncio
async def test_lease_lost_during_audio_download_kills_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _run_hang_case(tmp_path, monkeypatch, hang="audio", mux=True)


@pytest.mark.asyncio
async def test_lease_lost_during_ffmpeg_kills_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _run_hang_case(tmp_path, monkeypatch, hang="ffmpeg", mux=True)


@pytest.mark.asyncio
async def test_lease_lost_during_ffprobe_kills_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _run_hang_case(tmp_path, monkeypatch, hang="ffprobe", mux=True)


async def _run_hang_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    hang: str,
    mux: bool,
    cancel: bool = False,
) -> DownloadExecutor:
    pid_file = tmp_path / "child.pid"
    fifo = tmp_path / "ready.fifo"
    os.mkfifo(fifo)
    _install_tools(tmp_path, hang=hang, pid_file=pid_file, fifo=fifo)
    store = _store(tmp_path / "artifacts")
    settings = _settings(tmp_path, mux=mux)
    executor = _executor(settings, store)
    _patch(executor, monkeypatch)
    snap = _snap()
    selection: MuxedDownloadSelection | ResolvedDownloadSelection
    selection = _mux_selection() if mux else _direct_selection()
    lose = False
    tick = asyncio.Event()

    async def owned(*, job_id: uuid.UUID, fence: int) -> bool:
        del job_id, fence
        return not lose

    async def watch_wait() -> None:
        await tick.wait()

    monkeypatch.setattr(executor, "lease_still_owned", owned)
    executor._lease_watch_wait = watch_wait  # type: ignore[method-assign]
    monkeypatch.setattr(
        executor, "_resolve_selection", AsyncMock(return_value=selection)
    )
    fail = AsyncMock()
    monkeypatch.setattr(executor, "_fail", fail)
    complete = AsyncMock(return_value=True)
    monkeypatch.setattr(MediaDownloadJobRepository, "complete_ready", complete)
    monkeypatch.setattr(
        MediaDownloadJobRepository,
        "database_now",
        AsyncMock(return_value=datetime.now(tz=UTC)),
    )
    monkeypatch.setattr(
        MediaDownloadJobRepository, "release_to_queued", AsyncMock()
    )

    task = asyncio.create_task(executor.execute(snap))
    await _wait_fifo(fifo)
    child = int(pid_file.read_text(encoding="utf-8").strip())
    assert _pid_alive(child)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        lose = True
        tick.set()
        await task
    assert not _pid_alive(child)
    assert executor._watchdog_started == executor._watchdog_finished
    assert not executor._watchdog_tasks
    fail.assert_not_awaited()
    complete.assert_not_awaited()
    published_root = store.root / "published"
    assert not published_root.exists() or not any(published_root.iterdir())
    return executor


@pytest.mark.asyncio
async def test_watchdog_tasks_never_survive_after_lease_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = await _run_hang_case(
        tmp_path, monkeypatch, hang="video", mux=True
    )
    assert executor._watchdog_finished >= 1
    assert all(task.done() for task in tuple(executor._watchdog_tasks))


@pytest.mark.asyncio
async def test_normal_completion_cancels_and_awaits_watchdog_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path / "artifacts")
    settings = _settings(tmp_path, mux=False)
    (tmp_path / "ytdlp.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (tmp_path / "ytdlp.sh").chmod(0o755)

    class _OnceRunner:
        async def run(self, argv: list[str], **kwargs: object) -> ProcessResult:
            del argv
            started = kwargs.get("started")
            setter = getattr(started, "set", None)
            if callable(setter):
                setter()
            output_dir = kwargs.get("output_dir")
            assert isinstance(output_dir, str)
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            (Path(output_dir) / "artifact.mp4").write_bytes(b"executor-payload")
            return ProcessResult(
                exit_code=0,
                stdout=b"",
                stderr=b"",
                timed_out=False,
                cancelled=False,
            )

    executor = DownloadExecutor(
        settings,
        session_factory=MagicMock(return_value=_FakeSession()),
        inspection_service=MagicMock(),
        inspection_registry=MagicMock(),
        artifact_store=store,
        process_runner=_OnceRunner(),  # type: ignore[arg-type]
        worker_id="test-worker",
    )
    _patch(executor, monkeypatch)
    monkeypatch.setattr(executor, "lease_still_owned", AsyncMock(return_value=True))
    monkeypatch.setattr(
        executor, "_resolve_selection", AsyncMock(return_value=_direct_selection())
    )
    monkeypatch.setattr(
        MediaDownloadJobRepository,
        "database_now",
        AsyncMock(return_value=datetime.now(tz=UTC)),
    )
    complete = AsyncMock(return_value=True)
    monkeypatch.setattr(MediaDownloadJobRepository, "complete_ready", complete)
    await executor.execute(_snap())
    assert executor._watchdog_started == 1
    assert executor._watchdog_finished == 1
    assert not executor._watchdog_tasks
    complete.assert_awaited()


@pytest.mark.asyncio
async def test_stale_worker_cannot_stage_publish_or_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _run_hang_case(tmp_path, monkeypatch, hang="ffmpeg", mux=True)
    published_root = tmp_path / "artifacts" / "published"
    assert not published_root.exists() or not any(published_root.iterdir())


@pytest.mark.asyncio
async def test_cancellation_remains_cancellation_not_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _run_hang_case(
        tmp_path, monkeypatch, hang="direct", mux=False, cancel=True
    )


@pytest.mark.asyncio
async def test_watchdog_db_failure_fails_closed_without_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "child.pid"
    fifo = tmp_path / "ready.fifo"
    os.mkfifo(fifo)
    _install_tools(tmp_path, hang="direct", pid_file=pid_file, fifo=fifo)
    store = _store(tmp_path / "artifacts")
    settings = _settings(tmp_path, mux=False)
    executor = _executor(settings, store)
    _patch(executor, monkeypatch)
    tick = asyncio.Event()
    explode = False

    async def owned(*, job_id: uuid.UUID, fence: int) -> bool:
        del job_id, fence
        if explode:
            raise RuntimeError("db down")
        return True

    async def watch_wait() -> None:
        await tick.wait()

    monkeypatch.setattr(executor, "lease_still_owned", owned)
    executor._lease_watch_wait = watch_wait  # type: ignore[method-assign]
    monkeypatch.setattr(
        executor,
        "_resolve_selection",
        AsyncMock(return_value=_direct_selection()),
    )
    fail = AsyncMock()
    monkeypatch.setattr(executor, "_fail", fail)
    task = asyncio.create_task(executor.execute(_snap()))
    await _wait_fifo(fifo)
    child = int(pid_file.read_text(encoding="utf-8").strip())
    explode = True
    tick.set()
    await task
    assert not _pid_alive(child)
    fail.assert_not_awaited()
    assert executor._watchdog_started == executor._watchdog_finished
