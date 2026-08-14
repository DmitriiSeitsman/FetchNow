"""Fixed ffmpeg argv: stream-copy only, no network, no user arguments."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from fetchnow.downloads.ffmpeg_argv import build_ffmpeg_mux_argv


def test_stream_copy_fixed_argv() -> None:
    argv = build_ffmpeg_mux_argv(
        executable="/usr/bin/ffmpeg",
        video_path="/var/lib/fetchnow/tmp/downloads/v/stream.mp4",
        audio_path="/var/lib/fetchnow/tmp/downloads/a/stream.m4a",
        output_path="/var/lib/fetchnow/tmp/downloads/mux/artifact.mp4",
        output_container="mp4",
    )
    assert argv[0] == "/usr/bin/ffmpeg"
    assert argv.count("-c") == 1
    assert argv[argv.index("-c") + 1] == "copy"
    assert "-n" in argv
    assert "-y" not in argv
    assert "-map" in argv
    assert "0:v:0" in argv
    assert "1:a:0" in argv
    assert argv[argv.index("-protocol_whitelist") + 1] == "file"
    assert argv[argv.index("-map_metadata") + 1] == "-1"
    assert "-map_metadata:s" in argv
    assert argv[argv.index("-map_metadata:s") + 1] == "-1"
    assert "crypto" not in argv
    assert "data" not in argv
    joined = " ".join(argv)
    assert "libx264" not in joined
    assert "aac" not in joined or "-c copy" in " ".join(argv)
    assert "http" not in joined
    assert "https" not in joined
    assert "tcp" not in joined
    assert "-filter" not in argv
    assert not any(part.startswith("-ihttp") for part in argv)


def test_rejects_relative_and_dash_paths() -> None:
    with pytest.raises(ValueError):
        build_ffmpeg_mux_argv(
            executable="ffmpeg",
            video_path="/v",
            audio_path="/a",
            output_path="/o",
            output_container="mp4",
        )
    with pytest.raises(ValueError):
        build_ffmpeg_mux_argv(
            executable="/usr/bin/ffmpeg",
            video_path="-i",
            audio_path="/a",
            output_path="/o",
            output_container="mp4",
        )
    with pytest.raises(ValueError):
        build_ffmpeg_mux_argv(
            executable="/usr/bin/ffmpeg",
            video_path="/v",
            audio_path="/a",
            output_path="/o",
            output_container="mkv",
        )


def test_ffprobe_argv_is_file_only() -> None:
    from fetchnow.downloads.ffprobe_argv import build_ffprobe_argv

    argv = build_ffprobe_argv(
        executable="/usr/bin/ffprobe",
        input_path="/var/lib/fetchnow/tmp/downloads/mux/artifact.mp4",
    )
    assert argv[argv.index("-protocol_whitelist") + 1] == "file"
    # stdin is disconnected by the subprocess runner. ffprobe 5.1 rejects
    # ffmpeg's -nostdin flag before it can inspect the artifact.
    assert "-nostdin" not in argv
    assert "crypto" not in argv
    assert "http" not in " ".join(argv)
    entries = argv[argv.index("-show_entries") + 1]
    assert "codec_type" in entries
    assert "codec_name" in entries
    assert "format_name" in entries
    assert "filename" not in entries
    assert "tags" not in entries


def test_system_ffprobe_accepts_every_probe_option_without_network() -> None:
    """Catch CLI drift against the ffprobe available to the test host."""
    from fetchnow.downloads.ffprobe_argv import build_ffprobe_argv

    executable = shutil.which("ffprobe")
    if executable is None:
        pytest.skip("ffprobe is not installed on this test host")
    argv = build_ffprobe_argv(
        executable=executable,
        input_path="/__fetchnow_nonexistent_probe_input__",
    )
    result = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
        timeout=10,
    )
    stderr = result.stderr.decode("utf-8", "replace")
    assert result.returncode != 0  # the deliberately nonexistent input
    assert "Option not found" not in stderr
    assert "Unrecognized option" not in stderr
    assert "Error splitting the argument list" not in stderr
