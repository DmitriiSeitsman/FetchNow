"""Protocol, handoff, and supervisor behavior that does not need Linux cgroups."""

from __future__ import annotations

import base64
import json
import shutil
import socket
import subprocess
import threading
import uuid
from pathlib import Path

import pytest

from fetchnow.downloads.ffmpeg_argv import build_ffmpeg_mux_argv
from fetchnow.downloads.ffprobe_argv import build_ffprobe_argv
from fetchnow.media_executor.argv import ffprobe_argv, mux_argv, tool_env
from fetchnow.media_executor.constants import WORKER_UID
from fetchnow.media_executor.handoff import HandoffError, copy_regular
from fetchnow.media_executor.layout import covers, job_directory, layout_rejection
from fetchnow.media_executor.protocol import ProtocolError, Request, parse_request
from fetchnow.media_executor.runner import LocalRunner, ToolOutcome, ToolRunner
from fetchnow.media_executor.server import ExecutorApp, peer_uid

_JOB = "11111111-1111-4111-8111-111111111111"


def _request(op: str, *, fence: int = 4, container: str | None = None) -> bytes:
    fields = {
        "v": 1,
        "op": op,
        "job_id": _JOB,
        "attempt": 1,
        "fence": fence,
    }
    if container is not None:
        fields["container"] = container
    return json.dumps(fields).encode("utf-8") + b"\n"


def _app(tmp_path: Path, runner: ToolRunner | None = None) -> ExecutorApp:
    return ExecutorApp(
        work_root=tmp_path,
        runner=runner or LocalRunner(),
        ffmpeg="/usr/bin/ffmpeg",
        ffprobe="/usr/bin/ffprobe",
        socket_dir=tmp_path / "sock",
        peer_lookup=lambda _conn: WORKER_UID,
    )


def test_malformed_oversized_unknown_and_network_ops() -> None:
    with pytest.raises(ProtocolError) as malformed:
        parse_request(b"{}")
    assert malformed.value.code == "malformed"
    with pytest.raises(ProtocolError) as extra:
        parse_request(_request("mux_copy", container="mp4")[:-1] + b',"argv":["sh"]}\n')
    assert extra.value.code == "malformed"
    with pytest.raises(ProtocolError) as unknown:
        parse_request(_request("shell"))
    assert unknown.value.code == "unknown_operation"
    with pytest.raises(ProtocolError) as network:
        parse_request(_request("inspect_metadata"))
    assert network.value.code == "network_not_in_sec08"
    with pytest.raises(ProtocolError) as download:
        parse_request(_request("download_progressive"))
    assert download.value.code == "network_not_in_sec08"
    huge = b"{" + b" " * 5000 + b"}\n"
    with pytest.raises(ProtocolError) as oversized:
        parse_request(huge)
    assert oversized.value.code == "oversized"


def test_unauthorized_peer_and_forged_shape(tmp_path: Path) -> None:
    app = _app(tmp_path)
    denied = app.handle(_request("reserve"), peer_uid=0)
    assert b"unauthorized" in denied
    forged = app.handle(_request("reserve", fence=9), peer_uid=WORKER_UID)
    assert b"reserved" in forged


def test_fixed_argv_matches_product_builder(tmp_path: Path) -> None:
    video = tmp_path / "v.mp4"
    audio = tmp_path / "a.m4a"
    output = tmp_path / "out.mp4"
    assert mux_argv(
        ffmpeg="/usr/bin/ffmpeg",
        video=video,
        audio=audio,
        output=output,
        container="mp4",
    ) == build_ffmpeg_mux_argv(
        executable="/usr/bin/ffmpeg",
        video_path=str(video),
        audio_path=str(audio),
        output_path=str(output),
        output_container="mp4",
    )
    probe = ffprobe_argv(ffprobe="/usr/bin/ffprobe", source=output)
    assert probe == build_ffprobe_argv(
        executable="/usr/bin/ffprobe", input_path=str(output)
    )
    env = tool_env()
    assert "DATABASE_URL" not in env
    assert env["PATH"] == "/usr/bin:/bin"
    assert "-nostdin" not in probe


def test_layout_rejects_read_only_root_over_work(tmp_path: Path) -> None:
    request = Request(op="reserve", job_id=_JOB, attempt=1, fence=4)
    attempt = job_directory(tmp_path, request)
    assert layout_rejection(["/usr"], [str(attempt)]) is None
    assert layout_rejection([str(tmp_path)], [str(attempt)]) == "ro_covers_protected"
    assert covers("/usr", "/usr/bin/ffmpeg")
    assert not covers("/usr", str(attempt))


def test_cancel_stops_one_job_and_overflow_rejects_a_third(tmp_path: Path) -> None:
    started: list[threading.Event] = []
    lock = threading.Lock()

    class _Hold(ToolRunner):
        def run(
            self,
            *,
            attempt: Path,
            argv: list[str],
            timeout_seconds: float,
            cancel: threading.Event,
            protected: list[str],
            max_output_bytes: int | None = None,
            min_free_bytes: int | None = None,
        ) -> ToolOutcome:
            del attempt, argv, timeout_seconds, protected
            flag = threading.Event()
            with lock:
                started.append(flag)
            flag.set()
            if cancel.wait(timeout=2):
                return ToolOutcome(None, b"", b"", False, True)
            return ToolOutcome(0, b"", b"", False, False)

    app = _app(tmp_path, _Hold())
    jobs = (
        (_JOB, 4, "mp4"),
        ("22222222-2222-4222-8222-222222222222", 4, "webm"),
        ("33333333-3333-4333-8333-333333333333", 4, "mp4"),
    )
    results: dict[str, bytes] = {}

    def _run(job_id: str, container: str) -> None:
        raw = _request("mux_copy", container=container).replace(
            _JOB.encode(), job_id.encode()
        )
        results[job_id] = app.handle(raw, peer_uid=WORKER_UID)

    threads: list[threading.Thread] = []
    for job_id, fence, container in jobs[:2]:
        assert b"reserved" in app.handle(
            _request("reserve", fence=fence).replace(_JOB.encode(), job_id.encode()),
            peer_uid=WORKER_UID,
        )
        thread = threading.Thread(target=_run, args=(job_id, container))
        thread.start()
        threads.append(thread)
    for _ in range(40):
        if len(started) >= 2:
            break
        threading.Event().wait(0.05)
    assert len(started) == 2
    third_id = jobs[2][0]
    assert b"reserved" in app.handle(
        _request("reserve").replace(_JOB.encode(), third_id.encode()),
        peer_uid=WORKER_UID,
    )
    overflow = app.handle(
        _request("mux_copy", container="mp4").replace(_JOB.encode(), third_id.encode()),
        peer_uid=WORKER_UID,
    )
    assert b"overflow" in overflow
    assert b"cancelling" in app.handle(
        _request("cancel").replace(_JOB.encode(), jobs[0][0].encode()),
        peer_uid=WORKER_UID,
    )
    threads[0].join(timeout=2)
    assert b"cancelled" in results[jobs[0][0]]
    assert jobs[1][0] not in results
    assert b"not_running" in app.handle(
        _request("cancel", fence=99), peer_uid=WORKER_UID
    )
    app.handle(
        _request("cancel").replace(_JOB.encode(), jobs[1][0].encode()),
        peer_uid=WORKER_UID,
    )
    threads[1].join(timeout=2)


def test_symlink_copy_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "real"
    source.write_bytes(b"video")
    link = tmp_path / "link"
    link.symlink_to(source)
    dest_dir = tmp_path / "job"
    dest_dir.mkdir()
    with pytest.raises(HandoffError):
        copy_regular(link, dest_dir / "input-video", max_bytes=100)
    nested = dest_dir / "sub"
    nested.mkdir()
    with pytest.raises(HandoffError):
        copy_regular(source, nested / ".." / ".." / "nope", max_bytes=100)


def test_peercred_fails_closed_without_the_socket_option() -> None:
    if hasattr(socket, "SO_PEERCRED"):
        return
    with pytest.raises(OSError, match="SO_PEERCRED"):
        peer_uid(socket.socket())


def test_launcher_source_keeps_abi_floor_and_drops_chown() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "fetchnow"
        / "media_executor"
        / "sec08-launch.c"
    ).read_text(encoding="utf-8")
    assert "#define MIN_ABI 6" in source
    assert "LANDLOCK_SCOPE_SIGNAL" in source
    assert "LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET" in source
    assert "chown" not in source
    assert "control_dir" not in source
    assert "no fallback" in source


def test_restart_drops_memory_cache(tmp_path: Path) -> None:
    class _Ok(ToolRunner):
        def run(
            self,
            *,
            attempt: Path,
            argv: list[str],
            timeout_seconds: float,
            cancel: threading.Event,
            protected: list[str],
            max_output_bytes: int | None = None,
            min_free_bytes: int | None = None,
        ) -> ToolOutcome:
            del attempt, argv, timeout_seconds, cancel, protected
            return ToolOutcome(0, b"{}", b"", False, False)

    first = _app(tmp_path, _Ok())
    assert b"reserved" in first.handle(_request("reserve"), peer_uid=WORKER_UID)
    assert b'"ok":true' in first.handle(
        _request("ffprobe_validate"), peer_uid=WORKER_UID
    )
    restarted = _app(tmp_path, _Ok())
    missing = restarted.handle(_request("ffprobe_validate"), peer_uid=WORKER_UID)
    assert b"not_reserved" in missing


def test_local_stream_copy_mp4_and_webm(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("ffmpeg/ffprobe not installed")
    app = ExecutorApp(
        work_root=tmp_path,
        runner=LocalRunner(),
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        socket_dir=tmp_path / "sock",
        peer_lookup=lambda _conn: WORKER_UID,
    )
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    pairs = (
        (
            _JOB,
            "mp4",
            [
                "-f",
                "lavfi",
                "-i",
                "testsrc=duration=1:size=160x120:rate=10",
                "-pix_fmt",
                "yuv420p",
                "-c:v",
                "libx264",
            ],
            ["-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "aac"],
        ),
        (
            "22222222-2222-4222-8222-222222222222",
            "webm",
            [
                "-f",
                "lavfi",
                "-i",
                "testsrc=duration=1:size=160x120:rate=10",
                "-c:v",
                "libvpx",
            ],
            ["-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "libopus"],
        ),
    )
    for job_id, container, video_args, audio_args in pairs:
        video = fixtures / f"v.{container}"
        audio = fixtures / f"a.{container}"
        subprocess.run(
            [ffmpeg, "-hide_banner", "-y", *video_args, str(video)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [ffmpeg, "-hide_banner", "-y", *audio_args, str(audio)],
            check=True,
            capture_output=True,
        )
        reserve = _request("reserve").replace(_JOB.encode(), job_id.encode())
        assert b"reserved" in app.handle(reserve, peer_uid=WORKER_UID)
        job = job_directory(
            tmp_path, Request(op="reserve", job_id=job_id, attempt=1, fence=4)
        )
        (job / "input-video").write_bytes(video.read_bytes())
        (job / "input-audio").write_bytes(audio.read_bytes())
        mux = _request("mux_copy", container=container).replace(
            _JOB.encode(), job_id.encode()
        )
        assert b'"ok":true' in app.handle(mux, peer_uid=WORKER_UID)
        output = job / "output-mux"
        assert output.is_file() and output.stat().st_size > 0
        probe = _request("ffprobe_validate").replace(_JOB.encode(), job_id.encode())
        body = app.handle(probe, peer_uid=WORKER_UID)
        assert b'"ok":true' in body
        assert b"format" in base64.b64decode(json.loads(body)["stdout_b64"])


def test_job_name_rejects_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        job_directory(
            tmp_path,
            Request(op="reserve", job_id="../escape", attempt=1, fence=1),
        )
    _ = uuid.UUID(_JOB)
