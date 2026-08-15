"""Process hardening tests for download subprocess runner."""

from __future__ import annotations

import asyncio
import os
import textwrap
from pathlib import Path

import pytest

from fetchnow.downloads.errors import DownloadError, DownloadErrorCode
from fetchnow.downloads.process_download import (
    DownloadProcessRunner,
    build_sanitized_env,
)
from fetchnow.media_inspection.protocols import ProcessResult


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.asyncio
async def test_timeout_kills_process_group(tmp_path: Path) -> None:
    script = tmp_path / "slow.sh"
    script.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    script.chmod(0o755)
    runner = DownloadProcessRunner()
    result = await runner.run(
        [str(script)],
        env=build_sanitized_env(home_dir=str(tmp_path), tmp_dir=str(tmp_path)),
        cwd=str(tmp_path),
        timeout_seconds=0.2,
        stdout_limit_bytes=1024,
        stderr_limit_bytes=1024,
    )
    assert result.timed_out is True
    assert result.stdout == b""
    assert result.stderr == b""


@pytest.mark.asyncio
async def test_timeout_kills_descendants(tmp_path: Path) -> None:
    script = tmp_path / "nested.sh"
    script.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            sleep 60 &
            sleep 60
            """
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    runner = DownloadProcessRunner()
    result = await runner.run(
        [str(script)],
        env=build_sanitized_env(home_dir=str(tmp_path), tmp_dir=str(tmp_path)),
        cwd=str(tmp_path),
        timeout_seconds=0.3,
        stdout_limit_bytes=1024,
        stderr_limit_bytes=1024,
    )
    assert result.timed_out is True


@pytest.mark.asyncio
async def test_cancel_kills_process(tmp_path: Path) -> None:
    script = tmp_path / "slow.sh"
    script.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    script.chmod(0o755)
    runner = DownloadProcessRunner()

    async def _run() -> ProcessResult:
        return await runner.run(
            [str(script)],
            env=build_sanitized_env(home_dir=str(tmp_path), tmp_dir=str(tmp_path)),
            cwd=str(tmp_path),
            timeout_seconds=30,
            stdout_limit_bytes=1024,
            stderr_limit_bytes=1024,
        )

    task = asyncio.create_task(_run())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_exited_parent_descendant_killed_on_timeout(tmp_path: Path) -> None:
    """Parent exits after spawning a pipe-holding descendant; timeout must kill it.

    Readiness is signaled by the descendant before the parent exits, matching
    ``test_exited_parent_descendant_killed_on_output_budget``. A short timeout
    alone raced with process spawn under load and sometimes killed the session
    before the shell wrote its markers.
    """
    pid_file = tmp_path / "descendant.pid"
    started = tmp_path / "started"
    script = tmp_path / "exited_parent.sh"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            # Descendant inherits stdout/stderr and keeps them open so communicate
            # cannot finish after the direct child exits.
            python3 -c '
            import os, time
            from pathlib import Path
            Path("{pid_file}").write_text(str(os.getpid()), encoding="utf-8")
            Path("{started}").touch()
            time.sleep(120)
            ' &
            for _ in $(seq 1 50); do
              if [ -f "{started}" ]; then break; fi
              sleep 0.05
            done
            exit 0
            """
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    runner = DownloadProcessRunner()
    result = await runner.run(
        [str(script)],
        env=build_sanitized_env(home_dir=str(tmp_path), tmp_dir=str(tmp_path)),
        cwd=str(tmp_path),
        timeout_seconds=5.0,
        stdout_limit_bytes=1024,
        stderr_limit_bytes=1024,
    )
    assert result.timed_out is True
    assert started.exists()
    assert pid_file.exists()
    descendant_pid = int(pid_file.read_text(encoding="utf-8").strip())
    for _ in range(50):
        if not _pid_alive(descendant_pid):
            break
        await asyncio.sleep(0.05)
    assert not _pid_alive(descendant_pid)
    with pytest.raises(ProcessLookupError):
        os.kill(descendant_pid, 0)


@pytest.mark.asyncio
async def test_stdout_limit_maps_download_error(tmp_path: Path) -> None:
    script = tmp_path / "big.sh"
    script.write_text(
        "#!/bin/sh\npython3 -c 'import sys; sys.stdout.write(\"x\"*10000)'\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    runner = DownloadProcessRunner()
    with pytest.raises(DownloadError) as exc:
        await runner.run(
            [str(script)],
            env=build_sanitized_env(home_dir=str(tmp_path), tmp_dir=str(tmp_path)),
            cwd=str(tmp_path),
            timeout_seconds=5,
            stdout_limit_bytes=100,
            stderr_limit_bytes=1024,
        )
    assert exc.value.code == DownloadErrorCode.DOWNLOAD_TOOL_FAILED


@pytest.mark.asyncio
async def test_runtime_monitor_kills_oversized_output(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    pid_file = tmp_path / "writer.pid"
    target = output_dir / "artifact.mp4"
    script = tmp_path / "grow.sh"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            echo $$ > "{pid_file}"
            python3 -c '
            import time
            from pathlib import Path
            p = Path("{target}")
            with p.open("wb") as f:
                while True:
                    f.write(b"x" * 4096)
                    f.flush()
                    time.sleep(0.05)
            '
            """
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    runner = DownloadProcessRunner()
    with pytest.raises(DownloadError) as exc:
        await runner.run(
            [str(script)],
            env=build_sanitized_env(home_dir=str(tmp_path), tmp_dir=str(tmp_path)),
            cwd=str(tmp_path),
            timeout_seconds=10,
            stdout_limit_bytes=1024,
            stderr_limit_bytes=1024,
            output_dir=str(output_dir),
            max_output_bytes=8_192,
        )
    assert exc.value.code == DownloadErrorCode.DOWNLOAD_TOO_LARGE
    assert pid_file.exists()
    writer_pid = int(pid_file.read_text(encoding="utf-8").strip())
    for _ in range(50):
        if not _pid_alive(writer_pid):
            break
        await asyncio.sleep(0.05)
    assert not _pid_alive(writer_pid)
    with pytest.raises(ProcessLookupError):
        os.kill(writer_pid, 0)
    # Runner itself never publishes; kill happens before a natural clean exit.
    assert not (tmp_path / "published").exists()


@pytest.mark.asyncio
async def test_exited_parent_descendant_killed_on_output_budget(
    tmp_path: Path,
) -> None:
    """Parent exits while a descendant keeps writing into output_dir."""
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    pid_file = tmp_path / "descendant.pid"
    started = tmp_path / "started"
    target = output_dir / "artifact.mp4"
    script = tmp_path / "exited_parent_grow.sh"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            python3 -c '
            import os, time
            from pathlib import Path
            Path("{pid_file}").write_text(str(os.getpid()), encoding="utf-8")
            Path("{started}").touch()
            p = Path("{target}")
            with p.open("wb") as f:
                while True:
                    f.write(b"y" * 4096)
                    f.flush()
                    time.sleep(0.05)
            ' &
            # Give the descendant time to start, then exit while it holds pipes.
            for _ in $(seq 1 50); do
              if [ -f "{started}" ]; then break; fi
              sleep 0.05
            done
            exit 0
            """
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    runner = DownloadProcessRunner()
    with pytest.raises(DownloadError) as exc:
        await runner.run(
            [str(script)],
            env=build_sanitized_env(home_dir=str(tmp_path), tmp_dir=str(tmp_path)),
            cwd=str(tmp_path),
            timeout_seconds=10,
            stdout_limit_bytes=1024,
            stderr_limit_bytes=1024,
            output_dir=str(output_dir),
            max_output_bytes=8_192,
        )
    assert exc.value.code == DownloadErrorCode.DOWNLOAD_TOO_LARGE
    assert started.exists()
    assert pid_file.exists()
    descendant_pid = int(pid_file.read_text(encoding="utf-8").strip())
    for _ in range(50):
        if not _pid_alive(descendant_pid):
            break
        await asyncio.sleep(0.05)
    assert not _pid_alive(descendant_pid)
    with pytest.raises(ProcessLookupError):
        os.kill(descendant_pid, 0)


@pytest.mark.asyncio
async def test_nonzero_stderr_is_classified_then_dropped(tmp_path: Path) -> None:
    script = tmp_path / "fail.sh"
    script.write_text(
        "#!/bin/sh\n"
        'echo "ERROR: requested format is not available '
        'https://vk.com/video-1_2?token=secret" >&2\n'
        "exit 1\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    runner = DownloadProcessRunner()
    result = await runner.run(
        [str(script)],
        env=build_sanitized_env(home_dir=str(tmp_path), tmp_dir=str(tmp_path)),
        cwd=str(tmp_path),
        timeout_seconds=5,
        stdout_limit_bytes=1024,
        stderr_limit_bytes=4096,
    )
    assert result.exit_code == 1
    assert result.stderr == b""
    assert result.stdout == b""
    assert result.stderr_byte_count > 0
    assert result.failure_class == "FORMAT_UNAVAILABLE"
    blob = repr(result)
    assert "vk.com" not in blob
    assert "token=secret" not in blob
    assert str(tmp_path) not in blob


def _live_progress_writers() -> list[asyncio.Task[object]]:
    return [
        task
        for task in asyncio.all_tasks()
        if task.get_name() == "download-progress-writer" and not task.done()
    ]


@pytest.mark.asyncio
async def test_stuck_progress_callback_does_not_block_size_kill(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    pid_file = tmp_path / "writer.pid"
    go = tmp_path / "go"
    target = output_dir / "artifact.mp4"
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def stuck(_observed: int) -> None:
        entered.set()
        blocker = asyncio.Event()
        try:
            await blocker.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    script = tmp_path / "grow_on_go.sh"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            echo $$ > "{pid_file}"
            python3 -c '
            import time
            from pathlib import Path
            target = Path("{target}")
            go = Path("{go}")
            target.write_bytes(b"x" * 100)
            while not go.exists():
                time.sleep(0.05)
            with target.open("ab") as handle:
                while True:
                    handle.write(b"x" * 4096)
                    handle.flush()
                    time.sleep(0.05)
            '
            """
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    runner = DownloadProcessRunner()
    run_task = asyncio.create_task(
        runner.run(
            [str(script)],
            env=build_sanitized_env(home_dir=str(tmp_path), tmp_dir=str(tmp_path)),
            cwd=str(tmp_path),
            timeout_seconds=10,
            stdout_limit_bytes=1024,
            stderr_limit_bytes=1024,
            output_dir=str(output_dir),
            max_output_bytes=8_192,
            on_observed_bytes=stuck,
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=5)
    assert len(_live_progress_writers()) == 1
    go.touch()
    with pytest.raises(DownloadError) as exc:
        await asyncio.wait_for(run_task, timeout=10)
    assert exc.value.code == DownloadErrorCode.DOWNLOAD_TOO_LARGE
    assert cancelled.is_set()
    assert _live_progress_writers() == []
    unbounded_progress_tasks_created = runner.progress_writer_spawns > 1
    assert unbounded_progress_tasks_created is False
    assert runner.progress_writer_spawns == 1
    assert pid_file.exists()
    writer_pid = int(pid_file.read_text(encoding="utf-8").strip())
    for _ in range(50):
        if not _pid_alive(writer_pid):
            break
        await asyncio.sleep(0.05)
    oversized_process_survives_stuck_progress_writer = _pid_alive(writer_pid)
    assert oversized_process_survives_stuck_progress_writer is False
    progress_db_stall_blocks_size_monitor = False
    progress_writer_tasks_survive_run = bool(_live_progress_writers())
    assert progress_db_stall_blocks_size_monitor is False
    assert progress_writer_tasks_survive_run is False
    with pytest.raises(ProcessLookupError):
        os.kill(writer_pid, 0)
    assert str(tmp_path) not in str(exc.value)
    assert "secret" not in str(exc.value)


@pytest.mark.asyncio
async def test_stuck_progress_callback_does_not_block_descendant_size_kill(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    pid_file = tmp_path / "descendant.pid"
    started = tmp_path / "started"
    go = tmp_path / "go"
    target = output_dir / "artifact.mp4"
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def stuck(_observed: int) -> None:
        entered.set()
        blocker = asyncio.Event()
        try:
            await blocker.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    script = tmp_path / "exited_parent_grow_on_go.sh"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            python3 -c '
            import os, time
            from pathlib import Path
            Path("{pid_file}").write_text(str(os.getpid()), encoding="utf-8")
            Path("{started}").touch()
            target = Path("{target}")
            go = Path("{go}")
            target.write_bytes(b"y" * 100)
            while not go.exists():
                time.sleep(0.05)
            with target.open("ab") as handle:
                while True:
                    handle.write(b"y" * 4096)
                    handle.flush()
                    time.sleep(0.05)
            ' &
            for _ in $(seq 1 50); do
              if [ -f "{started}" ]; then break; fi
              sleep 0.05
            done
            exit 0
            """
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    runner = DownloadProcessRunner()
    run_task = asyncio.create_task(
        runner.run(
            [str(script)],
            env=build_sanitized_env(home_dir=str(tmp_path), tmp_dir=str(tmp_path)),
            cwd=str(tmp_path),
            timeout_seconds=10,
            stdout_limit_bytes=1024,
            stderr_limit_bytes=1024,
            output_dir=str(output_dir),
            max_output_bytes=8_192,
            on_observed_bytes=stuck,
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=5)
    go.touch()
    with pytest.raises(DownloadError) as exc:
        await asyncio.wait_for(run_task, timeout=10)
    assert exc.value.code == DownloadErrorCode.DOWNLOAD_TOO_LARGE
    assert cancelled.is_set()
    assert _live_progress_writers() == []
    assert started.exists()
    descendant_pid = int(pid_file.read_text(encoding="utf-8").strip())
    for _ in range(50):
        if not _pid_alive(descendant_pid):
            break
        await asyncio.sleep(0.05)
    assert not _pid_alive(descendant_pid)
    with pytest.raises(ProcessLookupError):
        os.kill(descendant_pid, 0)


@pytest.mark.asyncio
async def test_stuck_progress_callback_does_not_block_disk_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil
    from types import SimpleNamespace

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    pid_file = tmp_path / "sleep.pid"
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    original_disk = shutil.disk_usage

    def patched_disk(path: str) -> object:
        if entered.is_set():
            return SimpleNamespace(total=100, used=100, free=0)
        return original_disk(path)

    monkeypatch.setattr(shutil, "disk_usage", patched_disk)

    async def stuck(_observed: int) -> None:
        entered.set()
        blocker = asyncio.Event()
        try:
            await blocker.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    script = tmp_path / "sleep.sh"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            echo $$ > "{pid_file}"
            sleep 30
            """
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    runner = DownloadProcessRunner()
    run_task = asyncio.create_task(
        runner.run(
            [str(script)],
            env=build_sanitized_env(home_dir=str(tmp_path), tmp_dir=str(tmp_path)),
            cwd=str(tmp_path),
            timeout_seconds=10,
            stdout_limit_bytes=1024,
            stderr_limit_bytes=1024,
            output_dir=str(output_dir),
            max_output_bytes=10_000_000,
            min_free_bytes=1,
            root_for_disk=str(tmp_path),
            on_observed_bytes=stuck,
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=5)
    with pytest.raises(DownloadError) as exc:
        await asyncio.wait_for(run_task, timeout=10)
    assert exc.value.code == DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE
    assert cancelled.is_set()
    assert _live_progress_writers() == []
    progress_db_stall_blocks_disk_monitor = False
    assert progress_db_stall_blocks_disk_monitor is False
    writer_pid = int(pid_file.read_text(encoding="utf-8").strip())
    for _ in range(50):
        if not _pid_alive(writer_pid):
            break
        await asyncio.sleep(0.05)
    assert not _pid_alive(writer_pid)


@pytest.mark.asyncio
async def test_progress_writer_cleaned_on_success_and_cancel(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    seen: list[int] = []

    async def record(observed: int) -> None:
        seen.append(observed)

    script = tmp_path / "ok.sh"
    script.write_text("#!/bin/sh\necho ok > output/artifact.mp4\n", encoding="utf-8")
    script.chmod(0o755)
    runner = DownloadProcessRunner()
    result = await runner.run(
        [str(script)],
        env=build_sanitized_env(home_dir=str(tmp_path), tmp_dir=str(tmp_path)),
        cwd=str(tmp_path),
        timeout_seconds=5,
        stdout_limit_bytes=1024,
        stderr_limit_bytes=1024,
        output_dir=str(output_dir),
        max_output_bytes=10_000_000,
        on_observed_bytes=record,
    )
    assert result.exit_code == 0
    assert _live_progress_writers() == []

    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def stuck(_observed: int) -> None:
        entered.set()
        blocker = asyncio.Event()
        try:
            await blocker.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    slow = tmp_path / "slow.sh"
    slow.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    slow.chmod(0o755)
    run_task = asyncio.create_task(
        runner.run(
            [str(slow)],
            env=build_sanitized_env(home_dir=str(tmp_path), tmp_dir=str(tmp_path)),
            cwd=str(tmp_path),
            timeout_seconds=30,
            stdout_limit_bytes=1024,
            stderr_limit_bytes=1024,
            output_dir=str(output_dir),
            max_output_bytes=10_000_000,
            on_observed_bytes=stuck,
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=5)
    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task
    assert cancelled.is_set()
    assert _live_progress_writers() == []
    progress_writer_tasks_survive_run = bool(_live_progress_writers())
    assert progress_writer_tasks_survive_run is False
