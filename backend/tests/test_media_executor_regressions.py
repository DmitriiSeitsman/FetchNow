"""Regressions for real lifecycle contracts, not a native isolation claim."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from unittest.mock import Mock

import pytest

from fetchnow.media_executor.client import ExecutorCallError, UnixExecutorClient
from fetchnow.media_executor.handoff import HandoffError, copy_regular
from fetchnow.media_executor.layout import make_owned_directory
from fetchnow.media_executor.protocol import ProtocolError, parse_request
from fetchnow.media_executor.runner import ToolOutcome, ToolRunner
from fetchnow.media_executor.server import ExecutorApp

JOB = "11111111-1111-4111-8111-111111111111"


def frame(op: str) -> bytes:
    return json.dumps(dict(v=1, op=op, job_id=JOB, attempt=1, fence=1)).encode() + b"\n"


def test_directory_creation_never_changes_server_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("os.geteuid", lambda: 0)
    mutate = Mock(side_effect=AssertionError("server credentials changed"))
    monkeypatch.setattr("fetchnow.media_executor.layout._set_ids", mutate)
    run = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr("fetchnow.media_executor.layout.subprocess.run", run)
    make_owned_directory(tmp_path / "job", uid=10003, gid=10001, mode=0o2770)
    mutate.assert_not_called()
    assert "-m" in run.call_args.args[0]


def test_stale_directory_cannot_be_reserved_after_restart(tmp_path: Path) -> None:
    (tmp_path / f"{JOB}_1_1").mkdir()
    app = ExecutorApp(
        work_root=tmp_path,
        runner=Mock(),
        ffmpeg="/ffmpeg",
        ffprobe="/ffprobe",
        socket_dir=tmp_path,
    )
    assert json.loads(app.handle(frame("reserve"), peer_uid=10001))["ok"] is False
    assert (
        json.loads(app.handle(frame("ffprobe_validate"), peer_uid=10001))["code"]
        == "not_reserved"
    )


def test_none_exit_code_is_never_success(tmp_path: Path) -> None:
    runner = Mock()
    runner.run.return_value = ToolOutcome(None, b"", b"", False, False)
    app = ExecutorApp(
        work_root=tmp_path,
        runner=runner,
        ffmpeg="/ffmpeg",
        ffprobe="/ffprobe",
        socket_dir=tmp_path,
    )
    app.handle(frame("reserve"), peer_uid=10001)
    assert (
        json.loads(app.handle(frame("ffprobe_validate"), peer_uid=10001))["ok"] is False
    )


def test_cancel_same_client_does_not_wait_for_running_rpc(tmp_path: Path) -> None:
    started = threading.Event()

    class Hold(ToolRunner):
        def run(self, **kwargs: object) -> ToolOutcome:
            started.set()
            cancel = kwargs["cancel"]
            assert isinstance(cancel, threading.Event)
            assert cancel.wait(3), "cancel blocked behind active RPC"
            return ToolOutcome(-9, b"", b"", False, True)

    path = Path(f"/tmp/fn-reg-{uuid.uuid4().hex[:8]}.sock")
    app = ExecutorApp(
        work_root=tmp_path,
        runner=Hold(),
        ffmpeg="/ffmpeg",
        ffprobe="/ffprobe",
        socket_dir=tmp_path,
        peer_lookup=lambda _: 10001,
    )
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(path))
    threading.Thread(target=app.serve, args=(sock,), daemon=True).start()

    async def exercise() -> None:
        client = UnixExecutorClient(path)
        await client.reserve(job_id=JOB, attempt=1, fence=1)
        task = asyncio.create_task(
            client.ffprobe_validate(job_id=JOB, attempt=1, fence=1, timeout_seconds=4)
        )
        assert await asyncio.to_thread(started.wait, 2)
        await asyncio.wait_for(client.cancel(job_id=JOB, attempt=1, fence=1), 1)
        assert (await task).cancelled

    try:
        asyncio.run(exercise())
    finally:
        sock.close()
        path.unlink(missing_ok=True)


def test_reserve_error_is_not_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failed(*args: object, **kwargs: object) -> dict[str, object]:
        return {"ok": False, "code": "failed"}

    monkeypatch.setattr(UnixExecutorClient, "_call", failed)
    with pytest.raises(ExecutorCallError, match="reserve_failed"):
        asyncio.run(
            UnixExecutorClient(Path("/missing")).reserve(job_id=JOB, attempt=1, fence=1)
        )


def test_fifo_output_does_not_block_worker(tmp_path: Path) -> None:
    import os

    fifo = tmp_path / "output-mux"
    os.mkfifo(fifo)
    with pytest.raises(HandoffError, match="not_regular"):
        copy_regular(fifo, tmp_path / "published", max_bytes=10)


@pytest.mark.parametrize("container", [{}, [], 1])
def test_unhashable_container_is_protocol_error(container: object) -> None:
    data = json.loads(frame("mux_copy"))
    data["container"] = container
    with pytest.raises(ProtocolError):
        parse_request(json.dumps(data).encode() + b"\n")


@pytest.mark.parametrize("size,expected_code", [(16, 0), (300000, 1)])
def test_job_output_is_drained_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    size: int,
    expected_code: int,
) -> None:
    from fetchnow.media_executor import job_process

    # Local pipe/reaping regression, NOT cgroup/Landlock evidence.
    monkeypatch.setattr(
        job_process.ctypes, "CDLL", lambda *a, **kw: Mock(prctl=lambda *args: 0)
    )
    original = subprocess.Popen
    launched = []

    def launch(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        proc = original(*args, **kwargs)
        launched.append(proc)
        return proc

    monkeypatch.setattr(job_process.subprocess, "Popen", launch)

    def terminate(_path: Path) -> None:
        for proc in launched:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(proc.pid, 9)

    monkeypatch.setattr(job_process, "write_cgroup_kill", terminate)
    read_fd, write_fd = os.pipe()
    try:
        result = job_process.execute(
            tmp_path,
            read_fd,
            3,
            0,
            0,
            tmp_path,
            [sys.executable, "-c", f"import sys; sys.stdout.write('x'*{size})"],
        )
    finally:
        os.close(read_fd)
        os.close(write_fd)
    assert result["exit_code"] == expected_code
    assert result["timed_out"] is False
    assert len(result["stdout"]) <= 262144
    assert launched[0].returncode is not None
