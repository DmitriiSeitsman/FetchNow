"""Mux-coded overflow/timeout still kills descendants via the shared runner."""

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


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.asyncio
async def test_stdout_overflow_uses_muxing_failed_and_kills(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "child.pid"
    flood = tmp_path / "flood.py"
    flood.write_text(
        "\n".join(
            [
                "import sys, time",
                "sys.stdout.write('x' * 4096)",
                "sys.stdout.flush()",
                "time.sleep(30)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    script = tmp_path / "flood.sh"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            sleep 30 &
            echo $! > "{pid_file}"
            python3 "{flood}"
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
            stdout_limit_bytes=64,
            stderr_limit_bytes=1024,
            overflow_code=DownloadErrorCode.MUXING_FAILED,
        )
    assert exc.value.code == DownloadErrorCode.MUXING_FAILED
    child = int(pid_file.read_text(encoding="utf-8").strip())
    for _ in range(50):
        if not _pid_alive(child):
            break
        await asyncio.sleep(0.05)
    assert not _pid_alive(child)
