"""SEC-09 corrective regressions (F1–F8). Offline / unit evidence only."""

from __future__ import annotations

import contextlib
import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

from fetchnow.media_egress_proxy.policy import DenyConfig
from fetchnow.media_egress_proxy.server import EgressProxy, ProxyLimits, _tunnel
from fetchnow.media_executor.constants import PROFILE_NETWORK, WORKER_UID
from fetchnow.media_executor.layout import _remove_tree_fd, remove_job_directory
from fetchnow.media_executor.runner import LocalRunner, ToolOutcome, ToolRunner
from fetchnow.media_executor.server import ExecutorApp

_JOB = "22222222-2222-4222-8222-222222222222"
_URL = "https://vk.com/video-1_2"
ROOT = Path(__file__).resolve().parents[2]


def test_f1_dockerfile_import_closure_markers() -> None:
    offline = (ROOT / "backend/Dockerfile.media-executor").read_text()
    net = (ROOT / "backend/Dockerfile.media-net-executor").read_text()
    proxy = (ROOT / "backend/Dockerfile.media-egress-proxy").read_text()
    assert "downloads/__init__" not in offline
    assert "format_token" not in offline or "media_executor" in offline
    assert (
        "uv sync" not in net
        or "--no-install-project" in net
        or "yt-dlp==2026.7.4" in net
    )
    assert "url/models" not in proxy
    assert "printf" in offline and "package marker" in offline
    assert "yt-dlp==2026.7.4" in net
    assert "url/__init__" in proxy or "URL helpers" in proxy


def test_f1_slim_path_import_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate image PYTHONPATH: media_executor modules only."""
    import subprocess
    import sys

    slim = tmp_path / "src"
    (slim / "fetchnow").mkdir(parents=True)
    (slim / "fetchnow" / "__init__.py").write_text('"""marker"""\n')
    src = ROOT / "backend/src/fetchnow/media_executor"
    dest = slim / "fetchnow" / "media_executor"
    dest.mkdir()
    for path in src.glob("*.py"):
        (dest / path.name).write_text(path.read_text())
    (dest / "__init__.py").write_text('"""marker"""\n')
    script = tmp_path / "probe.py"
    script.write_text(
        "import importlib.util as u\n"
        "import fetchnow.media_executor.protocol\n"
        "import fetchnow.media_executor.__main__\n"
        "\n"
        "def missing(name: str) -> bool:\n"
        "    try:\n"
        "        return u.find_spec(name) is None\n"
        "    except ModuleNotFoundError:\n"
        "        return True\n"
        "\n"
        "assert missing('fetchnow.downloads.artifacts')\n"
        "assert missing('fetchnow.url.models')\n"
        "print('ok')\n"
    )
    proc = subprocess.run(
        [sys.executable, str(script)],
        env={**os.environ, "PYTHONPATH": str(slim)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


def test_f1_proxy_slim_path_import_closure(tmp_path: Path) -> None:
    import subprocess
    import sys

    slim = tmp_path / "src"
    (slim / "fetchnow").mkdir(parents=True)
    (slim / "fetchnow" / "__init__.py").write_text('"""marker"""\n')
    url = slim / "fetchnow" / "url"
    url.mkdir()
    (url / "__init__.py").write_text('"""URL helpers"""\n')
    (url / "destination.py").write_text(
        (ROOT / "backend/src/fetchnow/url/destination.py").read_text()
    )
    proxy_src = ROOT / "backend/src/fetchnow/media_egress_proxy"
    dest = slim / "fetchnow" / "media_egress_proxy"
    dest.mkdir()
    for path in proxy_src.glob("*.py"):
        (dest / path.name).write_text(path.read_text())
    (dest / "__init__.py").write_text('"""marker"""\n')
    script = tmp_path / "probe_proxy.py"
    script.write_text(
        "import importlib.util as u\n"
        "import fetchnow.media_egress_proxy.server\n"
        "\n"
        "def missing(name: str) -> bool:\n"
        "    try:\n"
        "        return u.find_spec(name) is None\n"
        "    except ModuleNotFoundError:\n"
        "        return True\n"
        "\n"
        "assert missing('fetchnow.url.models')\n"
        "assert missing('fetchnow.url.validate')\n"
        "print('ok')\n"
    )
    proc = subprocess.run(
        [sys.executable, str(script)],
        env={**os.environ, "PYTHONPATH": str(slim)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_f6_remove_nested_cache_and_symlink(tmp_path: Path) -> None:
    job = tmp_path / "job"
    job.mkdir()
    cache = job / "cache"
    cache.mkdir()
    (cache / "nested").mkdir()
    (cache / "nested" / "x").write_bytes(b"1")
    outside = tmp_path / "outside"
    outside.write_bytes(b"secret")
    link = job / "escape"
    link.symlink_to(outside)
    fd = os.open(job, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _remove_tree_fd(fd)
    finally:
        os.close(fd)
    # Children gone; job dir remains for caller rmdir.
    assert list(job.iterdir()) == []
    assert outside.read_bytes() == b"secret"


def test_f6_remove_job_directory_helper_invokes_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(argv))

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr("fetchnow.media_executor.layout.subprocess.run", fake_run)
    remove_job_directory(tmp_path / "job")
    assert calls and calls[0][-2] == "remove"


def test_f4_worker_cannot_raise_server_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, int | None] = {}

    class _Ok(ToolRunner):
        def run(
            self,
            *,
            attempt: Path,
            argv: list[str],
            timeout_seconds: float,
            cancel: object,
            protected: list[str],
            max_output_bytes: int | None = None,
            min_free_bytes: int | None = None,
        ) -> ToolOutcome:
            del timeout_seconds, cancel, protected
            seen["max"] = max_output_bytes
            seen["free"] = min_free_bytes
            (attempt / "output-artifact.bin").write_bytes(b"abc")
            assert "--max-filesize" in argv
            assert argv[argv.index("--max-filesize") + 1] == "50"
            return ToolOutcome(0, b"", b"", False, False)

    app = ExecutorApp(
        work_root=tmp_path,
        runner=_Ok(),
        profile=PROFILE_NETWORK,
        ytdlp="/opt/venv/bin/yt-dlp",
        proxy_url="http://egress-proxy:8888",
        max_filesize_bytes=50,
        socket_dir=tmp_path / "sock",
        peer_lookup=lambda _c: WORKER_UID,
    )
    app.handle(
        json.dumps(
            {"v": 1, "op": "reserve", "job_id": _JOB, "attempt": 1, "fence": 4}
        ).encode()
        + b"\n",
        peer_uid=WORKER_UID,
    )
    monkeypatch.setattr(
        "fetchnow.media_executor.server.artifact_stat",
        lambda job_dir: ("output-artifact.bin", 3),
    )
    body = app.handle(
        (
            json.dumps(
                {
                    "v": 1,
                    "op": "download_progressive",
                    "job_id": _JOB,
                    "attempt": 1,
                    "fence": 4,
                    "url": _URL,
                    "provider_id": "vk",
                    "format_token": "fmt_1",
                    "max_bytes": 5000,
                    "min_free_bytes": 12,
                }
            )
            + "\n"
        ).encode(),
        peer_uid=WORKER_UID,
    )
    assert b'"ok":true' in body
    assert seen["max"] == 50
    assert seen["free"] == 12


def test_f4_size_monitor_kills_local_runner(tmp_path: Path) -> None:
    script = tmp_path / "grow.py"
    script.write_text(
        "import time, pathlib\n"
        "p=pathlib.Path('out.bin')\n"
        "while True:\n"
        " data=p.read_bytes() if p.exists() else b''\n"
        " p.write_bytes(data + b'x'*1000)\n"
        " time.sleep(0.05)\n"
    )
    runner = LocalRunner()
    cancel = threading.Event()
    outcome = runner.run(
        attempt=tmp_path,
        argv=[__import__("sys").executable, str(script)],
        timeout_seconds=5,
        cancel=cancel,
        protected=[],
        max_output_bytes=5_000,
        min_free_bytes=None,
    )
    assert outcome.exit_code == 1
    assert outcome.cancelled is False


def test_f7_tunnel_releases_on_stalled_send() -> None:
    left, right = socket.socketpair()
    peer_a, peer_b = socket.socketpair()
    try:
        left.setblocking(False)
        right.setblocking(False)
        peer_a.setblocking(False)
        peer_b.close()
        started = time.monotonic()
        _tunnel(left, peer_a, idle_timeout=0.2)
        assert time.monotonic() - started < 2.0
    finally:
        for s in (left, right, peer_a):
            with contextlib.suppress(OSError):
                s.close()


def test_f7_header_deadline_slowloris() -> None:
    proxy = EgressProxy(
        deny=DenyConfig(networks=(), hosts=()),
        limits=ProxyLimits(handshake_timeout=0.2, max_connections=2),
    )
    server, client = socket.socketpair()
    try:

        def slow() -> None:
            # Drip bytes without completing headers.
            for ch in b"CO":
                client.sendall(bytes([ch]))
                time.sleep(0.15)

        threading.Thread(target=slow, daemon=True).start()
        started = time.monotonic()
        proxy.handle_client(server)
        assert time.monotonic() - started < 2.0
    finally:
        server.close()
        client.close()


def test_f2_cache_created_via_owned_helper_not_root_mkdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path] = []

    def fake_make(path: Path, *, uid: int, gid: int, mode: int) -> None:
        calls.append(path)
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, mode)

    monkeypatch.setattr(
        "fetchnow.media_executor.server.make_owned_directory", fake_make
    )

    class _Ok(ToolRunner):
        def run(self, **kwargs):  # type: ignore[no-untyped-def]
            return ToolOutcome(0, b'{"id":"x"}', b"", False, False)

    app = ExecutorApp(
        work_root=tmp_path,
        runner=_Ok(),
        profile=PROFILE_NETWORK,
        ytdlp="/opt/venv/bin/yt-dlp",
        proxy_url="http://egress-proxy:8888",
        socket_dir=tmp_path / "sock",
        make_job=lambda p: p.mkdir(mode=0o2700),
        peer_lookup=lambda _c: WORKER_UID,
    )
    app.handle(
        json.dumps(
            {"v": 1, "op": "reserve", "job_id": _JOB, "attempt": 1, "fence": 4}
        ).encode()
        + b"\n",
        peer_uid=WORKER_UID,
    )
    app.handle(
        (
            json.dumps(
                {
                    "v": 1,
                    "op": "inspect_metadata",
                    "job_id": _JOB,
                    "attempt": 1,
                    "fence": 4,
                    "url": _URL,
                    "provider_id": "vk",
                }
            )
            + "\n"
        ).encode(),
        peer_uid=WORKER_UID,
    )
    assert any(path.name == "cache" for path in calls)


def test_f5_cancel_release_waits_for_completion(tmp_path: Path) -> None:
    import asyncio

    from fetchnow.media_executor.client import ExecutorCallError
    from fetchnow.media_executor.net_client import _cancel_wait_release

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.running = True

        async def cancel(self, **kwargs):  # type: ignore[no-untyped-def]
            self.calls.append("cancel")
            self.running = False

        async def release(self, **kwargs):  # type: ignore[no-untyped-def]
            self.calls.append("release")
            if self.running:
                raise ExecutorCallError("already_running")

    async def _go() -> None:
        client = FakeClient()
        task = asyncio.create_task(asyncio.sleep(0.01))
        await _cancel_wait_release(
            client,  # type: ignore[arg-type]
            job_id=_JOB,
            attempt=1,
            fence=4,
            op_task=task,
            wait_seconds=1.0,
        )
        assert client.calls[0] == "cancel"
        assert "release" in client.calls

    asyncio.run(_go())
