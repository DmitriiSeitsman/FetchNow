"""Muxed download execution: exact tokens, ffmpeg copy, lease fencing."""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from fetchnow.core.config import Settings
from fetchnow.downloads.artifacts import MIN_ORPHAN_GRACE_SECONDS, ArtifactStore
from fetchnow.downloads.errors import DownloadErrorCode, raise_download_error
from fetchnow.downloads.executor import (
    DownloadClaimSnapshot,
    DownloadExecutor,
    _peak_reservation_bytes,
)
from fetchnow.downloads.repository import MediaDownloadJobRepository
from fetchnow.downloads.selection import MuxedDownloadSelection
from fetchnow.media_inspection.models import FormatCategory
from fetchnow.media_inspection.protocols import ProcessResult

_DB = "postgresql+asyncpg://fetchnow:fetchnow@localhost:5432/fetchnow"
_VIDEO_TOKEN = "url720-secret"
_AUDIO_TOKEN = "audio-aac-secret"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        APP_ENV="test",
        DATABASE_URL=_DB,
        MEDIA_DOWNLOADS_ENABLED=True,
        MEDIA_MUXING_ENABLED=True,
        MEDIA_DOWNLOAD_MIN_FREE_BYTES=0,
        MEDIA_DOWNLOAD_TEMP_ROOT=str(tmp_path / "artifacts"),
        MEDIA_INSPECTION_YTDLP_PATH="/opt/venv/bin/yt-dlp",
        MEDIA_MUXING_FFMPEG_PATH="/usr/bin/ffmpeg",
        MEDIA_MUXING_FFPROBE_PATH="/usr/bin/ffprobe",
    )


def _store(root: Path) -> ArtifactStore:
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return ArtifactStore(
        root=str(root),
        max_bytes=1024 * 1024,
        orphan_grace_seconds=MIN_ORPHAN_GRACE_SECONDS,
    )


def _selection() -> MuxedDownloadSelection:
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


class _MuxRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def run(self, argv: list[str], **kwargs: object) -> ProcessResult:
        self.calls.append(list(argv))
        started = kwargs.get("started")
        setter = getattr(started, "set", None)
        if callable(setter):
            setter()
        capture = bool(kwargs.get("capture_stdout"))
        if capture:
            payload = {
                "streams": [
                    {"codec_type": "video", "codec_name": "h264"},
                    {"codec_type": "audio", "codec_name": "aac"},
                ],
                "format": {
                    "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                    "duration": "12.0",
                    "size": "16",
                },
            }
            return ProcessResult(
                exit_code=0,
                stdout=json.dumps(payload).encode("utf-8"),
                stderr=b"",
                timed_out=False,
                cancelled=False,
            )
        output_dir = kwargs.get("output_dir")
        assert isinstance(output_dir, str)
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        if argv[-1].endswith(".mp4") and "-c" in argv:
            Path(argv[-1]).write_bytes(b"muxed-artifact!!")
        elif output_dir.endswith("/video"):
            (Path(output_dir) / "stream.mp4").write_bytes(b"video-bytes-here")
        elif output_dir.endswith("/audio"):
            (Path(output_dir) / "stream.m4a").write_bytes(b"audio-bytes-here")
        else:
            (Path(output_dir) / "artifact.mp4").write_bytes(b"muxed-artifact!!")
        return ProcessResult(
            exit_code=0,
            stdout=b"",
            stderr=b"",
            timed_out=False,
            cancelled=False,
        )


class _FakeSession:
    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        return None


def _executor(
    settings: Settings, store: ArtifactStore, runner: object
) -> DownloadExecutor:
    return DownloadExecutor(
        settings,
        session_factory=MagicMock(return_value=_FakeSession()),
        inspection_service=MagicMock(),
        inspection_registry=MagicMock(),
        artifact_store=store,
        process_runner=runner,  # type: ignore[arg-type]
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


@pytest.mark.asyncio
async def test_muxed_success_reaches_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path / "artifacts")
    settings = _settings(tmp_path)
    runner = _MuxRunner()
    executor = _executor(settings, store, runner)
    snap = _snap()
    selection = _selection()
    _patch(executor, monkeypatch)
    monkeypatch.setattr(executor, "lease_still_owned", AsyncMock(return_value=True))
    monkeypatch.setattr(
        executor, "_resolve_selection", AsyncMock(return_value=selection)
    )
    monkeypatch.setattr(
        MediaDownloadJobRepository,
        "database_now",
        AsyncMock(return_value=datetime.now(tz=UTC)),
    )
    complete = AsyncMock(return_value=True)
    monkeypatch.setattr(MediaDownloadJobRepository, "complete_ready", complete)

    await executor.execute(snap)

    assert complete.await_count == 1
    assert len(runner.calls) == 4
    video_argv, audio_argv, ffmpeg_argv, probe_argv = runner.calls
    assert "-f" in video_argv
    assert video_argv[video_argv.index("-f") + 1] == _VIDEO_TOKEN
    assert audio_argv[audio_argv.index("-f") + 1] == _AUDIO_TOKEN
    assert "+" not in "".join(video_argv)
    assert "+" not in "".join(audio_argv)
    assert ffmpeg_argv[ffmpeg_argv.index("-c") + 1] == "copy"
    assert "-map_metadata:s" in ffmpeg_argv
    assert "--no-part" in video_argv
    assert "--no-part" in audio_argv
    assert "filename" not in probe_argv[probe_argv.index("-show_entries") + 1]
    assert "http" not in " ".join(ffmpeg_argv)
    published = list((store.root / "published").iterdir())
    assert len(published) == 1
    assert (published[0] / "artifact.mp4").is_file()
    job_dir = store.root / str(snap.job_id)
    assert not job_dir.exists()
    blob = repr(selection) + str(complete.await_args)
    assert _VIDEO_TOKEN not in blob
    assert _AUDIO_TOKEN not in blob


@pytest.mark.asyncio
async def test_lease_loss_does_not_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path / "artifacts")
    settings = _settings(tmp_path)
    runner = _MuxRunner()
    executor = _executor(settings, store, runner)
    snap = _snap()
    _patch(executor, monkeypatch)
    owned = AsyncMock(side_effect=[True, True, False])
    monkeypatch.setattr(executor, "lease_still_owned", owned)
    monkeypatch.setattr(
        executor, "_resolve_selection", AsyncMock(return_value=_selection())
    )
    fail = AsyncMock()
    monkeypatch.setattr(executor, "_fail", fail)

    await executor.execute(snap)

    fail.assert_not_awaited()
    published = store.root / "published"
    assert not published.exists() or not any(published.iterdir())


@pytest.mark.asyncio
async def test_identity_drift_never_starts_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path / "artifacts")
    settings = _settings(tmp_path)
    runner = _MuxRunner()
    executor = _executor(settings, store, runner)
    snap = _snap()
    _patch(executor, monkeypatch)
    monkeypatch.setattr(executor, "lease_still_owned", AsyncMock(return_value=True))

    async def _bad_identity(_snap: object) -> object:
        raise_download_error(
            DownloadErrorCode.FORMAT_UNAVAILABLE,
            internal_reason="IDENTITY_DRIFT",
        )

    monkeypatch.setattr(executor, "_resolve_selection", _bad_identity)
    fail = AsyncMock()
    monkeypatch.setattr(executor, "_fail", fail)
    await executor.execute(snap)
    assert runner.calls == []
    fail.assert_awaited()


def test_peak_disk_includes_inputs_and_output() -> None:
    selection = _selection()
    peak = _peak_reservation_bytes(selection, 10_000_000)
    assert peak == 8_000_000 + 400_000 + 8_400_000
    unknown = MuxedDownloadSelection(
        format_option_id="fmt_muxbbbbbbbbbbbbbbbbbbbbbb",
        container="mp4",
        width=1280,
        height=720,
        fps=30.0,
        has_video=True,
        has_audio=True,
        category=FormatCategory.PROGRESSIVE,
        quality_label="p720",
        free_tier_eligible=True,
        approx_bytes=None,
        video_approx_bytes=None,
        audio_approx_bytes=None,
        expected_duration_seconds=None,
        video_format_token=_VIDEO_TOKEN,
        audio_format_token=_AUDIO_TOKEN,
    )
    max_bytes = 1_000
    peak_unknown = _peak_reservation_bytes(unknown, max_bytes)
    assert peak_unknown == max_bytes + max_bytes + max_bytes
