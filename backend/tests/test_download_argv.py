"""Argv safety tests for yt-dlp download invocations."""

from __future__ import annotations

import pytest

from fetchnow.downloads.ytdlp_download_argv import build_ytdlp_download_argv


def test_argv_shape_url_after_separator_shell_false() -> None:
    argv = build_ytdlp_download_argv(
        executable="/opt/yt-dlp",
        url="-evil.com/video",
        provider_format_token="url720",
        allowed_extractors=frozenset({"vk"}),
        socket_timeout=30,
        cache_dir="/tmp/cache",
        output_template="output/artifact.%(ext)s",
        max_filesize_bytes=1024,
    )
    assert argv[0] == "/opt/yt-dlp"
    assert "--" in argv
    assert argv[argv.index("--") + 1] == "-evil.com/video"
    assert argv[argv.index("-f") + 1] == "url720"
    assert argv[argv.index("--max-filesize") + 1] == "1024"
    from yt_dlp.utils import parse_bytes

    raw = argv[argv.index("--max-filesize") + 1]
    assert parse_bytes(raw) == 1024
    assert parse_bytes("1024B") is None
    assert "--no-cookies" in argv
    assert "--no-netrc" in argv
    assert "--ignore-config" in argv
    assert "--no-playlist" in argv
    assert "--no-part" in argv
    assert "--skip-download" not in argv
    # Callers must use create_subprocess_exec (shell=False); argv is a list.
    assert isinstance(argv, list)
    assert all(isinstance(item, str) for item in argv)


def test_generic_extractor_forbidden() -> None:
    with pytest.raises(ValueError, match="generic"):
        build_ytdlp_download_argv(
            executable="/opt/yt-dlp",
            url="https://vk.com/video-1_2",
            provider_format_token="url720",
            allowed_extractors=frozenset({"vk", "generic"}),
            socket_timeout=10,
            cache_dir="/tmp/c",
            output_template="artifact.%(ext)s",
            max_filesize_bytes=1024,
        )


def test_no_cookie_or_merge_flags() -> None:
    argv = build_ytdlp_download_argv(
        executable="/opt/yt-dlp",
        url="https://vk.com/video-1_2",
        provider_format_token="url720",
        allowed_extractors=frozenset({"vk"}),
        socket_timeout=10,
        cache_dir="/tmp/c",
        output_template="artifact.%(ext)s",
        max_filesize_bytes=2048,
    )
    forbidden = {
        "--cookies",
        "--cookies-from-browser",
        "--netrc",
        "--proxy",
        "--ffmpeg-location",
        "--merge-output-format",
        "--external-downloader",
        "--force-generic-extractor",
        "--write-subs",
        "--write-thumbnail",
    }
    assert forbidden.isdisjoint(argv)


def test_token_null_rejected() -> None:
    with pytest.raises(ValueError):
        build_ytdlp_download_argv(
            executable="/opt/yt-dlp",
            url="https://vk.com/video-1_2",
            provider_format_token="bad\x00token",
            allowed_extractors=frozenset({"vk"}),
            socket_timeout=10,
            cache_dir="/tmp/c",
            output_template="artifact.%(ext)s",
            max_filesize_bytes=1024,
        )


def test_reserved_token_rejected() -> None:
    with pytest.raises(ValueError):
        build_ytdlp_download_argv(
            executable="/opt/yt-dlp",
            url="https://vk.com/video-1_2",
            provider_format_token="best",
            allowed_extractors=frozenset({"vk"}),
            socket_timeout=10,
            cache_dir="/tmp/c",
            output_template="artifact.%(ext)s",
            max_filesize_bytes=1024,
        )
