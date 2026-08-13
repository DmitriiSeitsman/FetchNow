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
    """Parent exits after spawning a pipe-holding descendant; timeout must kill it."""
    pid_file = tmp_path / "descendant.pid"
    started = tmp_path / "started"
    script = tmp_path / "exited_parent.sh"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            # Descendant inherits stdout/stderr and keeps them open so communicate
            # cannot finish after the direct child exits.
            sleep 120 &
            echo $! > "{pid_file}"
            touch "{started}"
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
        timeout_seconds=1.0,
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
