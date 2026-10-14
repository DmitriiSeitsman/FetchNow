"""F2: server-side growth/tree/min-free limits for all tool operations."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from fetchnow.media_executor.constants import (
    INSPECT_MAX_GROWTH_BYTES,
    MAX_ARTIFACT_BYTES,
    OFFLINE_MUX_MAX_GROWTH_BYTES,
    OFFLINE_MUX_MAX_TREE_BYTES,
    PROFILE_NETWORK,
    PROFILE_OFFLINE,
    TOOL_MIN_FREE_BYTES,
    WORKER_UID,
)
from fetchnow.media_executor.protocol import Request
from fetchnow.media_executor.runner import LocalRunner, ToolRunner
from fetchnow.media_executor.server import ExecutorApp

_JOB = "11111111-1111-4111-8111-111111111111"


def _app(
    tmp_path: Path, runner: ToolRunner, *, profile: str = PROFILE_OFFLINE
) -> ExecutorApp:
    return ExecutorApp(
        work_root=tmp_path / "work",
        runner=runner,
        socket_dir=tmp_path / "sock",
        profile=profile,
        ffmpeg="/usr/bin/ffmpeg",
        ffprobe="/usr/bin/ffprobe",
        ytdlp="/opt/venv/bin/yt-dlp" if profile == PROFILE_NETWORK else None,
        proxy_url="http://egress-proxy:8888" if profile == PROFILE_NETWORK else None,
        peer_lookup=lambda _c: WORKER_UID,
    )


def test_limits_for_offline_and_inspect_are_positive_server_ceilings(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path, LocalRunner())
    mux = Request(op="mux_copy", job_id=_JOB, attempt=1, fence=1, container="mp4")
    growth, tree, free = app._limits_for(mux)
    assert growth == OFFLINE_MUX_MAX_GROWTH_BYTES
    assert tree == OFFLINE_MUX_MAX_TREE_BYTES
    assert free == TOOL_MIN_FREE_BYTES
    assert growth > 0 and tree > 0 and free > 0
    # Large legal inputs fit under tree while growth remains one artifact.
    assert tree >= 2 * MAX_ARTIFACT_BYTES + growth

    probe = Request(op="ffprobe_validate", job_id=_JOB, attempt=1, fence=1)
    g2, t2, f2 = app._limits_for(probe)
    assert 0 < g2 < growth
    assert t2 >= tree
    assert f2 == TOOL_MIN_FREE_BYTES

    net = _app(tmp_path / "net", LocalRunner(), profile=PROFILE_NETWORK)
    inspect = Request(
        op="inspect_metadata",
        job_id=_JOB,
        attempt=1,
        fence=1,
        url="https://vk.com/video-1_1",
        provider_id="vk",
    )
    g3, t3, f3 = net._limits_for(inspect)
    assert g3 == INSPECT_MAX_GROWTH_BYTES
    assert t3 == INSPECT_MAX_GROWTH_BYTES
    assert f3 == TOOL_MIN_FREE_BYTES


def test_download_client_cannot_raise_growth_ceiling(tmp_path: Path) -> None:
    app = _app(tmp_path / "net", LocalRunner(), profile=PROFILE_NETWORK)
    app.max_filesize_bytes = 100
    req = Request(
        op="download_progressive",
        job_id=_JOB,
        attempt=1,
        fence=1,
        url="https://vk.com/video-1_1",
        provider_id="vk",
        format_token="fmt_1",
        max_bytes=50_000,
        min_free_bytes=0,
    )
    growth, tree, free = app._limits_for(req)
    assert growth == 100
    assert tree == 100
    assert free == 0


def test_growth_ignores_preexisting_mux_inputs(tmp_path: Path) -> None:
    """Inputs already on disk must not consume the growth ceiling."""
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    # Simulate large staged inputs near the artifact ceiling.
    (attempt / "input-video").write_bytes(b"v" * 8_000)
    (attempt / "input-audio").write_bytes(b"a" * 8_000)
    script = attempt / "write_small.py"
    script.write_text(
        "from pathlib import Path\n"
        "Path('output-mux').write_bytes(b'o' * 100)\n"
    )
    outcome = LocalRunner().run(
        attempt=attempt,
        argv=[sys.executable, str(script)],
        timeout_seconds=5,
        cancel=threading.Event(),
        protected=[],
        max_growth_bytes=1_000,
        max_tree_bytes=20_000,
        min_free_bytes=None,
    )
    assert outcome.exit_code == 0
    assert (attempt / "output-mux").is_file()


def test_growth_limit_kills_during_execution(tmp_path: Path) -> None:
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    (attempt / "seed").write_bytes(b"s" * 100)
    script = attempt / "grow.py"
    script.write_text(
        "import time\n"
        "from pathlib import Path\n"
        "p = Path('out.bin')\n"
        "while True:\n"
        "    data = p.read_bytes() if p.exists() else b''\n"
        "    p.write_bytes(data + b'x' * 2000)\n"
        "    time.sleep(0.02)\n"
    )
    outcome = LocalRunner().run(
        attempt=attempt,
        argv=[sys.executable, str(script)],
        timeout_seconds=5,
        cancel=threading.Event(),
        protected=[],
        max_growth_bytes=4_000,
        max_tree_bytes=100_000,
        min_free_bytes=None,
    )
    assert outcome.exit_code == 1
    out = attempt / "out.bin"
    growth = out.stat().st_size if out.exists() else 0
    assert growth <= 20_000  # killed during growth; not unbounded


def test_measurement_error_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fetchnow.media_executor.runner as runner_mod

    monkeypatch.setattr(
        runner_mod,
        "_tree_bytes",
        lambda _p: (_ for _ in ()).throw(OSError("stat_failed")),
    )
    attempt = tmp_path / "a"
    attempt.mkdir()
    script = attempt / "sleep.py"
    script.write_text("import time\ntime.sleep(1)\n")
    outcome = LocalRunner().run(
        attempt=attempt,
        argv=[sys.executable, str(script)],
        timeout_seconds=2,
        cancel=threading.Event(),
        protected=[],
        max_growth_bytes=10,
        max_tree_bytes=10,
        min_free_bytes=None,
    )
    assert outcome.exit_code == 1


def test_neighbor_job_continues_while_limited_job_fails(tmp_path: Path) -> None:
    a1 = tmp_path / "job1"
    a2 = tmp_path / "job2"
    a1.mkdir()
    a2.mkdir()
    grow = a1 / "grow.py"
    grow.write_text(
        "import time\n"
        "from pathlib import Path\n"
        "p = Path('x')\n"
        "while True:\n"
        "    cur = p.read_bytes() if p.exists() else b''\n"
        "    p.write_bytes(cur + b'y' * 3000)\n"
        "    time.sleep(0.02)\n"
    )
    ok = a2 / "ok.py"
    ok.write_text("print('ok')\n")
    results: dict[str, int | None] = {}

    def run_limited() -> None:
        results["limited"] = LocalRunner().run(
            attempt=a1,
            argv=[sys.executable, str(grow)],
            timeout_seconds=5,
            cancel=threading.Event(),
            protected=[],
            max_growth_bytes=5_000,
            max_tree_bytes=50_000,
            min_free_bytes=None,
        ).exit_code

    def run_neighbor() -> None:
        time.sleep(0.1)
        results["neighbor"] = LocalRunner().run(
            attempt=a2,
            argv=[sys.executable, str(ok)],
            timeout_seconds=5,
            cancel=threading.Event(),
            protected=[],
            max_growth_bytes=1_000_000,
            max_tree_bytes=1_000_000,
            min_free_bytes=None,
        ).exit_code

    t1 = threading.Thread(target=run_limited)
    t2 = threading.Thread(target=run_neighbor)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert results["limited"] == 1
    assert results["neighbor"] == 0
