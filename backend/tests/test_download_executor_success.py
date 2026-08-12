"""DownloadExecutor success-path integration with a fake process runner."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from fetchnow.core.config import Settings
from fetchnow.downloads.artifacts import MIN_ORPHAN_GRACE_SECONDS, ArtifactStore
from fetchnow.downloads.errors import DownloadError, DownloadErrorCode
from fetchnow.downloads.executor import DownloadClaimSnapshot, DownloadExecutor
from fetchnow.downloads.repository import MediaDownloadJobRepository
from fetchnow.downloads.selection import ResolvedDownloadSelection
from fetchnow.media_inspection.models import FormatCategory
from fetchnow.media_inspection.protocols import ProcessResult

_DB = "postgresql+asyncpg://fetchnow:fetchnow@localhost:5432/fetchnow"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        APP_ENV="test",
        DATABASE_URL=_DB,
        MEDIA_DOWNLOADS_ENABLED=True,
        MEDIA_DOWNLOAD_MIN_FREE_BYTES=0,
        MEDIA_DOWNLOAD_TEMP_ROOT=str(tmp_path / "artifacts"),
        MEDIA_INSPECTION_YTDLP_PATH="/opt/venv/bin/yt-dlp",
    )


def _store(root: Path) -> ArtifactStore:
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return ArtifactStore(
        root=str(root),
        max_bytes=1024,
        orphan_grace_seconds=MIN_ORPHAN_GRACE_SECONDS,
    )


def _selection() -> ResolvedDownloadSelection:
    return ResolvedDownloadSelection(
        format_option_id="fmt_abc123",
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


def _snap(*, fence: int = 1) -> DownloadClaimSnapshot:
    now = datetime.now(tz=UTC)
    return DownloadClaimSnapshot(
        job_id=uuid.uuid4(),
        media_job_id=uuid.uuid4(),
        fence=fence,
        attempt_count=1,
        format_option_id="fmt_abc123",
        provider_id="vk",
        canonical_provider_url="https://vk.com/video-1_2",
        media_id="-1_2",
        hostname="vk.com",
        path="/video-1_2",
        scheme="https",
        port=443,
        selected_format_snapshot={"formatOptionId": "fmt_abc123"},
        expires_at=now + timedelta(hours=1),
    )


class _FakeSession:
    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        return None


class _WritingRunner:
    """Writes a valid artifact into workspace.output/ then exits 0."""

    def __init__(self, payload: bytes = b"executor-payload") -> None:
        self.payload = payload
        self.calls = 0

    async def run(self, argv: list[str], **kwargs: object) -> ProcessResult:
        del argv
        self.calls += 1
        output_dir = kwargs.get("output_dir")
        assert isinstance(output_dir, str)
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        (Path(output_dir) / "artifact.mp4").write_bytes(self.payload)
        return ProcessResult(
            exit_code=0,
            stdout=b"",
            stderr=b"",
            timed_out=False,
            cancelled=False,
        )


def _executor(
    settings: Settings,
    store: ArtifactStore,
    *,
    runner: object | None = None,
) -> DownloadExecutor:
    session_factory = MagicMock(return_value=_FakeSession())
    return DownloadExecutor(
        settings,
        session_factory=session_factory,
        inspection_service=MagicMock(),
        inspection_registry=MagicMock(),
        artifact_store=store,
        process_runner=runner or _WritingRunner(),  # type: ignore[arg-type]
        worker_id="test-worker",
    )


def _patch_download_tooling(
    executor: DownloadExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "fetchnow.downloads.executor.validate_ytdlp_executable",
        lambda path: path,
    )
    binding = MagicMock()
    binding.allowed_extractor_keys = frozenset({"vk"})
    executor._inspection_registry.resolve = MagicMock(return_value=binding)


@pytest.mark.asyncio
async def test_executor_success_publishes_ready_and_removes_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "artifacts"
    store = _store(root)
    settings = _settings(tmp_path)
    runner = _WritingRunner()
    executor = _executor(settings, store, runner=runner)
    snap = _snap()
    selection = _selection()

    _patch_download_tooling(executor, monkeypatch)
    monkeypatch.setattr(
        executor, "lease_still_owned", AsyncMock(return_value=True)
    )
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

    assert runner.calls == 1
    complete.assert_awaited_once()
    published_root = store.root / "published"
    assert published_root.is_dir()
    published_dirs = [p for p in published_root.iterdir() if p.is_dir()]
    assert len(published_dirs) == 1
    assert (published_dirs[0] / "artifact.mp4").is_file()
    # Attempt workspace must be gone after require_gone cleanup.
    job_dir = store.root / str(snap.job_id)
    assert not job_dir.exists()


@pytest.mark.asyncio
async def test_executor_cleanup_failure_does_not_commit_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "artifacts"
    store = _store(root)
    settings = _settings(tmp_path)
    executor = _executor(settings, store)
    snap = _snap()
    selection = _selection()

    _patch_download_tooling(executor, monkeypatch)
    monkeypatch.setattr(
        executor, "lease_still_owned", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        executor, "_resolve_selection", AsyncMock(return_value=selection)
    )

    real_cleanup = ArtifactStore.cleanup_attempt_workspace

    def _cleanup_fail(
        self: ArtifactStore,
        workspace: object,
        *,
        require_gone: bool = False,
    ) -> None:
        if require_gone:
            raise DownloadError(
                DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
                internal_reason="WORKSPACE_CLEANUP_RESIDUAL",
            )
        real_cleanup(self, workspace, require_gone=False)  # type: ignore[arg-type]

    monkeypatch.setattr(ArtifactStore, "cleanup_attempt_workspace", _cleanup_fail)
    complete = AsyncMock(return_value=True)
    monkeypatch.setattr(MediaDownloadJobRepository, "complete_ready", complete)
    fail = AsyncMock()
    monkeypatch.setattr(executor, "_fail", fail)
    monkeypatch.setattr(
        MediaDownloadJobRepository,
        "database_now",
        AsyncMock(return_value=datetime.now(tz=UTC)),
    )

    await executor.execute(snap)

    complete.assert_not_awaited()
    fail.assert_awaited()
    published_dirs = list((store.root / "published").iterdir())
    assert len(published_dirs) == 1


@pytest.mark.asyncio
async def test_executor_stale_complete_ready_leaves_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "artifacts"
    store = _store(root)
    settings = _settings(tmp_path)
    executor = _executor(settings, store)
    snap = _snap()
    selection = _selection()

    _patch_download_tooling(executor, monkeypatch)
    monkeypatch.setattr(
        executor, "lease_still_owned", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        executor, "_resolve_selection", AsyncMock(return_value=selection)
    )
    monkeypatch.setattr(
        MediaDownloadJobRepository,
        "database_now",
        AsyncMock(return_value=datetime.now(tz=UTC)),
    )
    monkeypatch.setattr(
        MediaDownloadJobRepository, "complete_ready", AsyncMock(return_value=False)
    )
    fail = AsyncMock()
    monkeypatch.setattr(executor, "_fail", fail)

    await executor.execute(snap)

    fail.assert_not_awaited()
    published_dirs = [p for p in (store.root / "published").iterdir() if p.is_dir()]
    assert len(published_dirs) == 1


@pytest.mark.asyncio
async def test_executor_complete_ready_raise_leaves_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "artifacts"
    store = _store(root)
    settings = _settings(tmp_path)
    executor = _executor(settings, store)
    snap = _snap()
    selection = _selection()

    _patch_download_tooling(executor, monkeypatch)
    monkeypatch.setattr(
        executor, "lease_still_owned", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        executor, "_resolve_selection", AsyncMock(return_value=selection)
    )
    monkeypatch.setattr(
        MediaDownloadJobRepository,
        "database_now",
        AsyncMock(return_value=datetime.now(tz=UTC)),
    )
    monkeypatch.setattr(
        MediaDownloadJobRepository,
        "complete_ready",
        AsyncMock(side_effect=RuntimeError("db-boom")),
    )
    fail = AsyncMock()
    monkeypatch.setattr(executor, "_fail", fail)

    await executor.execute(snap)

    fail.assert_awaited()
    published_dirs = [p for p in (store.root / "published").iterdir() if p.is_dir()]
    assert len(published_dirs) == 1
