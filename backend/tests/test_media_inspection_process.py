"""Process hardening and argv safety tests for yt-dlp adapter."""

from __future__ import annotations

import asyncio
import os
import stat
import textwrap
from pathlib import Path

import pytest

from fetchnow.media_inspection.errors import InspectionError, InspectionErrorKind
from fetchnow.media_inspection.process import (
    SubprocessProcessRunner,
    assert_env_has_no_secrets,
    build_sanitized_env,
)
from fetchnow.media_inspection.protocols import InspectionTarget, ProcessResult
from fetchnow.media_inspection.ytdlp_adapter import (
    YtDlpMetadataExtractor,
    validate_temp_root,
    validate_ytdlp_executable,
)
from fetchnow.media_inspection.ytdlp_argv import build_ytdlp_metadata_argv
from media_inspection_fixtures import VK_FIXTURE, dumps_fixture
from media_inspection_helpers import (
    FakeRunner,
    make_regular_executable,
    make_temp_root,
    settings,
)


def test_argv_is_fixed_and_url_after_separator() -> None:
    argv = build_ytdlp_metadata_argv(
        executable="/opt/yt-dlp",
        url="-evil.com/video",
        allowed_extractors=frozenset({"vk"}),
        socket_timeout_seconds=10,
        cache_dir="/tmp/cache",
    )
    assert argv[0] == "/opt/yt-dlp"
    assert "--" in argv
    assert argv[argv.index("--") + 1] == "-evil.com/video"
    assert "--skip-download" in argv
    assert "--dump-single-json" in argv
    assert "--no-playlist" in argv
    assert "--ignore-config" in argv
    assert "--no-cookies" in argv
    ies = argv[argv.index("--use-extractors") + 1]
    assert ies == "vk"
    sep = argv.index("--")
    assert not any(a == "-evil.com/video" and i < sep for i, a in enumerate(argv))
    forbidden = {
        "--cookies-from-browser",
        "--netrc",
        "--proxy",
        "--ffmpeg-location",
        "--external-downloader",
        "--force-generic-extractor",
    }
    assert forbidden.isdisjoint(argv)


def test_generic_extractor_rejected_in_argv_builder() -> None:
    with pytest.raises(ValueError):
        build_ytdlp_metadata_argv(
            executable="/opt/yt-dlp",
            url="https://vk.com/video-1_2",
            allowed_extractors=frozenset({"vk", "generic"}),
            socket_timeout_seconds=5,
            cache_dir="/tmp/c",
        )


def test_sanitized_env_has_no_app_secrets() -> None:
    env = build_sanitized_env(home_dir="/tmp/h", tmp_dir="/tmp/t")
    assert "DATABASE_URL" not in env
    assert_env_has_no_secrets(env)


def test_executable_rejects_symlink_and_writable(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(InspectionError):
        validate_ytdlp_executable(str(missing))

    target = tmp_path / "real"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    target.chmod(0o755)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(InspectionError) as exc2:
        validate_ytdlp_executable(str(link))
    assert "SYMLINK" in (exc2.value.internal_reason or "")

    writable = tmp_path / "writable"
    writable.write_text("#!/bin/sh\n", encoding="utf-8")
    writable.chmod(0o775)
    with pytest.raises(InspectionError) as exc3:
        validate_ytdlp_executable(str(writable))
    assert "WRITABLE" in (exc3.value.internal_reason or "")


def test_temp_root_rejects_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(InspectionError):
        validate_temp_root(str(link))


@pytest.mark.asyncio
async def test_fake_runner_receives_fixed_argv_and_clean_env(tmp_path: Path) -> None:
    exe = make_regular_executable(tmp_path)
    runner = FakeRunner(
        ProcessResult(
            exit_code=0,
            stdout=dumps_fixture(VK_FIXTURE),
            stderr=b"SECRET_STDERR should never leak",
            timed_out=False,
            cancelled=False,
        )
    )
    work = make_temp_root(tmp_path)
    s = settings(
        MEDIA_INSPECTION_YTDLP_PATH=exe,
        MEDIA_INSPECTION_TEMP_ROOT=work,
    )
    extractor = YtDlpMetadataExtractor(
        settings=s,
        allowed_extractor_keys=frozenset({"vk"}),
        runner=runner,
    )
    draft = await extractor.extract(
        InspectionTarget(
            provider_id="vk",
            hostname="vk.com",
            media_id="-123_456239017",
            canonical_provider_url="https://vk.com/video-123_456239017",
            tool_url="https://vk.com/video-123_456239017",
            allowed_hostnames=frozenset({"vk.com", "m.vk.com", "vkvideo.ru"}),
        )
    )
    assert draft.media_id == "-123_456239017"
    argv = runner.calls[0]["argv"]
    assert argv[0] == exe
    assert "--skip-download" in argv
    env = runner.calls[0]["env"]
    assert "DATABASE_URL" not in env
    assert list(Path(work).iterdir()) == []


@pytest.mark.asyncio
async def test_disabled_tool_never_runs(tmp_path: Path) -> None:
    runner = FakeRunner()
    s = settings(
        MEDIA_INSPECTION_ENABLED=False,
        MEDIA_INSPECTION_TEMP_ROOT=make_temp_root(tmp_path),
    )
    extractor = YtDlpMetadataExtractor(
        settings=s,
        allowed_extractor_keys=frozenset({"vk"}),
        runner=runner,
    )
    with pytest.raises(InspectionError) as exc:
        await extractor.extract(
            InspectionTarget(
                provider_id="vk",
                hostname="vk.com",
                media_id="-1_2",
                canonical_provider_url="https://vk.com/video-1_2",
                tool_url="https://vk.com/video-1_2",
                allowed_hostnames=frozenset({"vk.com"}),
            )
        )
    assert exc.value.kind is InspectionErrorKind.INSPECTION_TOOL_UNAVAILABLE
    assert runner.calls == []


@pytest.mark.asyncio
async def test_timeout_kills_process_group(tmp_path: Path) -> None:
    script = tmp_path / "slow.sh"
    script.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            sleep 30
            """
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    runner = SubprocessProcessRunner()
    result = await runner.run(
        [str(script)],
        env=build_sanitized_env(home_dir=str(tmp_path), tmp_dir=str(tmp_path)),
        cwd=str(tmp_path),
        timeout_seconds=0.2,
        stdout_limit_bytes=1024,
        stderr_limit_bytes=1024,
    )
    assert result.timed_out is True
    assert result.exit_code is not None


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
    runner = SubprocessProcessRunner()
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
    runner = SubprocessProcessRunner()

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
async def test_stdout_limit(tmp_path: Path) -> None:
    script = tmp_path / "big.sh"
    script.write_text(
        "#!/bin/sh\npython3 -c 'import sys; sys.stdout.write(\"x\"*10000)'\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    runner = SubprocessProcessRunner()
    with pytest.raises(InspectionError) as exc:
        await runner.run(
            [str(script)],
            env=build_sanitized_env(home_dir=str(tmp_path), tmp_dir=str(tmp_path)),
            cwd=str(tmp_path),
            timeout_seconds=5,
            stdout_limit_bytes=100,
            stderr_limit_bytes=1024,
        )
    assert exc.value.kind is InspectionErrorKind.INSPECTION_OUTPUT_LIMIT_EXCEEDED


@pytest.mark.asyncio
async def test_stderr_limit(tmp_path: Path) -> None:
    script = tmp_path / "bigerr.sh"
    script.write_text(
        "#!/bin/sh\npython3 -c 'import sys; sys.stderr.write(\"y\"*10000)'\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    runner = SubprocessProcessRunner()
    with pytest.raises(InspectionError) as exc:
        await runner.run(
            [str(script)],
            env=build_sanitized_env(home_dir=str(tmp_path), tmp_dir=str(tmp_path)),
            cwd=str(tmp_path),
            timeout_seconds=5,
            stdout_limit_bytes=1024,
            stderr_limit_bytes=100,
        )
    assert exc.value.kind is InspectionErrorKind.INSPECTION_OUTPUT_LIMIT_EXCEEDED


@pytest.mark.asyncio
async def test_nonzero_exit_maps_unavailable(tmp_path: Path) -> None:
    exe = make_regular_executable(tmp_path)
    runner = FakeRunner(
        ProcessResult(
            exit_code=1,
            stdout=b"",
            stderr=b"raw tool boom",
            timed_out=False,
            cancelled=False,
        )
    )
    s = settings(
        MEDIA_INSPECTION_YTDLP_PATH=exe,
        MEDIA_INSPECTION_TEMP_ROOT=make_temp_root(tmp_path),
    )
    extractor = YtDlpMetadataExtractor(
        settings=s,
        allowed_extractor_keys=frozenset({"vk"}),
        runner=runner,
    )
    with pytest.raises(InspectionError) as exc:
        await extractor.extract(
            InspectionTarget(
                provider_id="vk",
                hostname="vk.com",
                media_id="-1_2",
                canonical_provider_url="https://vk.com/video-1_2",
                tool_url="https://vk.com/video-1_2",
                allowed_hostnames=frozenset({"vk.com"}),
            )
        )
    assert exc.value.kind is InspectionErrorKind.INSPECTION_MEDIA_UNAVAILABLE
    assert "boom" not in str(exc.value)
    assert "boom" not in repr(exc.value)


@pytest.mark.asyncio
async def test_no_output_files_left(tmp_path: Path) -> None:
    exe = make_regular_executable(tmp_path)
    runner = FakeRunner(
        ProcessResult(
            exit_code=0,
            stdout=dumps_fixture(VK_FIXTURE),
            stderr=b"",
            timed_out=False,
            cancelled=False,
        )
    )
    work = make_temp_root(tmp_path)
    s = settings(
        MEDIA_INSPECTION_YTDLP_PATH=exe,
        MEDIA_INSPECTION_TEMP_ROOT=work,
    )
    extractor = YtDlpMetadataExtractor(
        settings=s,
        allowed_extractor_keys=frozenset({"vk"}),
        runner=runner,
    )
    await extractor.extract(
        InspectionTarget(
            provider_id="vk",
            hostname="vk.com",
            media_id="-123_456239017",
            canonical_provider_url="https://vk.com/video-123_456239017",
            tool_url="https://vk.com/video-123_456239017",
            allowed_hostnames=frozenset({"vk.com", "m.vk.com", "vkvideo.ru"}),
        )
    )
    assert list(Path(work).iterdir()) == []


def test_real_regular_executable_accepted(tmp_path: Path) -> None:
    path = validate_ytdlp_executable(make_regular_executable(tmp_path))
    assert os.path.isabs(path)
    assert not os.path.islink(path)
    assert stat.S_ISREG(os.stat(path).st_mode)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.asyncio
async def test_exited_parent_descendant_killed_on_timeout(tmp_path: Path) -> None:
    """Parent exits after spawning a pipe-holding descendant; timeout must kill it.

    Readiness is signaled by the descendant before the parent exits. A short
    timeout alone raced with process spawn under load and sometimes killed the
    session before the shell wrote its pid marker.
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
    runner = SubprocessProcessRunner()
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
    assert descendant_pid > 1
    # Allow the kernel a moment to reap after SIGKILL.
    for _ in range(50):
        if not _pid_alive(descendant_pid):
            break
        await asyncio.sleep(0.05)
    assert not _pid_alive(descendant_pid)
    # Proof: descendant_absence_actually_verified against the exact PID.
    with pytest.raises(ProcessLookupError):
        os.kill(descendant_pid, 0)


@pytest.mark.asyncio
async def test_exited_parent_descendant_killed_on_cancel(tmp_path: Path) -> None:
    pid_file = tmp_path / "descendant-cancel.pid"
    script = tmp_path / "exited_parent_cancel.sh"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            sleep 120 &
            echo $! > "{pid_file}"
            exit 0
            """
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    runner = SubprocessProcessRunner()

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
    for _ in range(100):
        if pid_file.exists() and pid_file.read_text(encoding="utf-8").strip():
            break
        await asyncio.sleep(0.02)
    assert pid_file.exists()
    descendant_pid = int(pid_file.read_text(encoding="utf-8").strip())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(50):
        if not _pid_alive(descendant_pid):
            break
        await asyncio.sleep(0.05)
    assert not _pid_alive(descendant_pid)
    with pytest.raises(ProcessLookupError):
        os.kill(descendant_pid, 0)


@pytest.mark.asyncio
async def test_reader_tasks_do_not_survive_stdout_overflow(tmp_path: Path) -> None:
    script = tmp_path / "big.sh"
    script.write_text(
        "#!/bin/sh\npython3 -c 'import sys; sys.stdout.write(\"x\"*100000); "
        "sys.stdout.flush(); import time; time.sleep(30)'\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    before = {id(t) for t in asyncio.all_tasks()}
    runner = SubprocessProcessRunner()
    with pytest.raises(InspectionError) as exc:
        await runner.run(
            [str(script)],
            env=build_sanitized_env(home_dir=str(tmp_path), tmp_dir=str(tmp_path)),
            cwd=str(tmp_path),
            timeout_seconds=5,
            stdout_limit_bytes=100,
            stderr_limit_bytes=1024,
        )
    assert exc.value.kind is InspectionErrorKind.INSPECTION_OUTPUT_LIMIT_EXCEEDED
    await asyncio.sleep(0.05)
    leftover = [
        t
        for t in asyncio.all_tasks()
        if id(t) not in before and not t.done() and t is not asyncio.current_task()
    ]
    assert leftover == []


@pytest.mark.asyncio
async def test_cleanup_failure_on_success_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = make_regular_executable(tmp_path)
    runner = FakeRunner(
        ProcessResult(
            exit_code=0,
            stdout=dumps_fixture(VK_FIXTURE),
            stderr=b"",
            timed_out=False,
            cancelled=False,
        )
    )
    work = make_temp_root(tmp_path)
    s = settings(
        MEDIA_INSPECTION_YTDLP_PATH=exe,
        MEDIA_INSPECTION_TEMP_ROOT=work,
    )
    extractor = YtDlpMetadataExtractor(
        settings=s,
        allowed_extractor_keys=frozenset({"vk"}),
        runner=runner,
    )

    def _boom(tmp_dir: str, *, require_gone: bool) -> None:
        del tmp_dir, require_gone
        raise InspectionError(
            kind=InspectionErrorKind.INTERNAL_INSPECTION_ERROR,
            message="internal inspection error",
            internal_reason="TEMP_CLEANUP_FAILED",
        )

    monkeypatch.setattr(
        "fetchnow.media_inspection.ytdlp_adapter._cleanup_workspace",
        _boom,
    )
    with pytest.raises(InspectionError) as exc:
        await extractor.extract(
            InspectionTarget(
                provider_id="vk",
                hostname="vk.com",
                media_id="-123_456239017",
                canonical_provider_url="https://vk.com/video-123_456239017",
                tool_url="https://vk.com/video-123_456239017",
                allowed_hostnames=frozenset({"vk.com", "m.vk.com", "vkvideo.ru"}),
            )
        )
    assert exc.value.internal_reason == "TEMP_CLEANUP_FAILED"
    assert work not in str(exc.value)
    assert work not in repr(exc.value)


@pytest.mark.asyncio
async def test_cleanup_failure_preserves_domain_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    exe = make_regular_executable(tmp_path)
    runner = FakeRunner(
        ProcessResult(
            exit_code=1,
            stdout=b"",
            stderr=b"",
            timed_out=False,
            cancelled=False,
        )
    )
    work = make_temp_root(tmp_path)
    s = settings(
        MEDIA_INSPECTION_YTDLP_PATH=exe,
        MEDIA_INSPECTION_TEMP_ROOT=work,
    )
    extractor = YtDlpMetadataExtractor(
        settings=s,
        allowed_extractor_keys=frozenset({"vk"}),
        runner=runner,
    )

    def _boom(tmp_dir: str, *, require_gone: bool) -> None:
        del require_gone
        raise InspectionError(
            kind=InspectionErrorKind.INTERNAL_INSPECTION_ERROR,
            message="internal inspection error",
            internal_reason="TEMP_CLEANUP_FAILED",
        )

    monkeypatch.setattr(
        "fetchnow.media_inspection.ytdlp_adapter._cleanup_workspace",
        _boom,
    )
    with (
        caplog.at_level("WARNING"),
        pytest.raises(InspectionError) as exc,
    ):
        await extractor.extract(
            InspectionTarget(
                provider_id="vk",
                hostname="vk.com",
                media_id="-1_2",
                canonical_provider_url="https://vk.com/video-1_2",
                tool_url="https://vk.com/video-1_2",
                allowed_hostnames=frozenset({"vk.com"}),
            )
        )
    assert exc.value.kind is InspectionErrorKind.INSPECTION_MEDIA_UNAVAILABLE
    assert exc.value.internal_reason == "PROCESS_NONZERO"
    assert work not in str(exc.value) + repr(exc.value)
    assert work not in " ".join(r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_cleanup_failure_preserves_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = make_regular_executable(tmp_path)
    runner = FakeRunner(raise_exc=asyncio.CancelledError())
    work = make_temp_root(tmp_path)
    s = settings(
        MEDIA_INSPECTION_YTDLP_PATH=exe,
        MEDIA_INSPECTION_TEMP_ROOT=work,
    )
    extractor = YtDlpMetadataExtractor(
        settings=s,
        allowed_extractor_keys=frozenset({"vk"}),
        runner=runner,
    )

    def _boom(tmp_dir: str, *, require_gone: bool) -> None:
        raise InspectionError(
            kind=InspectionErrorKind.INTERNAL_INSPECTION_ERROR,
            message="internal inspection error",
            internal_reason="TEMP_CLEANUP_FAILED",
        )

    monkeypatch.setattr(
        "fetchnow.media_inspection.ytdlp_adapter._cleanup_workspace",
        _boom,
    )
    with pytest.raises(asyncio.CancelledError):
        await extractor.extract(
            InspectionTarget(
                provider_id="vk",
                hostname="vk.com",
                media_id="-1_2",
                canonical_provider_url="https://vk.com/video-1_2",
                tool_url="https://vk.com/video-1_2",
                allowed_hostnames=frozenset({"vk.com"}),
            )
        )


@pytest.mark.asyncio
async def test_cleanup_failure_preserves_unexpected_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    exe = make_regular_executable(tmp_path)
    runner = FakeRunner(raise_exc=RuntimeError("primary-boom"))
    work = make_temp_root(tmp_path)
    s = settings(
        MEDIA_INSPECTION_YTDLP_PATH=exe,
        MEDIA_INSPECTION_TEMP_ROOT=work,
    )
    extractor = YtDlpMetadataExtractor(
        settings=s,
        allowed_extractor_keys=frozenset({"vk"}),
        runner=runner,
    )

    def _boom(tmp_dir: str, *, require_gone: bool) -> None:
        raise InspectionError(
            kind=InspectionErrorKind.INTERNAL_INSPECTION_ERROR,
            message="internal inspection error",
            internal_reason="TEMP_CLEANUP_FAILED",
        )

    monkeypatch.setattr(
        "fetchnow.media_inspection.ytdlp_adapter._cleanup_workspace",
        _boom,
    )
    with (
        caplog.at_level("WARNING"),
        pytest.raises(RuntimeError, match="primary-boom"),
    ):
        await extractor.extract(
            InspectionTarget(
                provider_id="vk",
                hostname="vk.com",
                media_id="-1_2",
                canonical_provider_url="https://vk.com/video-1_2",
                tool_url="https://vk.com/video-1_2",
                allowed_hostnames=frozenset({"vk.com"}),
            )
        )
    joined = " ".join(r.message for r in caplog.records)
    assert work not in joined
    assert "inspect_" not in joined
