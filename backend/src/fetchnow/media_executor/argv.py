"""Server-built argv. Nothing in the UDS request selects a binary or a flag."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from fetchnow.media_executor.constants import PROVIDER_EXTRACTORS


def mux_argv(
    *,
    ffmpeg: str,
    video: Path,
    audio: Path,
    output: Path,
    container: str,
) -> list[str]:
    if container not in {"mp4", "webm"}:
        raise ValueError("container")
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-loglevel",
        "error",
        "-n",
        "-protocol_whitelist",
        "file",
        "-i",
        str(video),
        "-i",
        str(audio),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c",
        "copy",
        "-sn",
        "-dn",
        "-map_metadata",
        "-1",
        "-map_metadata:s",
        "-1",
        "-map_chapters",
        "-1",
        "-f",
        container,
        str(output),
    ]


def ffprobe_argv(*, ffprobe: str, source: Path) -> list[str]:
    return [
        ffprobe,
        "-hide_banner",
        "-loglevel",
        "error",
        "-protocol_whitelist",
        "file",
        "-print_format",
        "json",
        "-show_entries",
        "stream=codec_type,codec_name:format=format_name,duration,size",
        "-i",
        str(source),
    ]


def validate_proxy_url(proxy_url: str) -> str:
    if not proxy_url or not isinstance(proxy_url, str):
        raise ValueError("proxy_url")
    parsed = urlparse(proxy_url)
    if parsed.scheme != "http" or not parsed.hostname or parsed.path not in {"", "/"}:
        raise ValueError("proxy_url")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("proxy_url")
    if parsed.query or parsed.fragment:
        raise ValueError("proxy_url")
    return proxy_url.rstrip("/")


def network_inspect_argv(
    *,
    executable: str,
    url: str,
    provider_id: str,
    proxy_url: str,
    socket_timeout: int,
    cache_dir: Path,
) -> list[str]:
    extractors = PROVIDER_EXTRACTORS.get(provider_id)
    if not extractors:
        raise ValueError("provider_id")
    proxy = validate_proxy_url(proxy_url)
    ies = ",".join(sorted(extractors))
    return [
        executable,
        "--ignore-config",
        "--no-config-locations",
        "--no-plugin-dirs",
        "--no-js-runtimes",
        "--no-update",
        "--no-playlist",
        "--skip-download",
        "--dump-single-json",
        "--no-write-info-json",
        "--no-write-description",
        "--no-write-thumbnail",
        "--no-write-comments",
        "--no-write-playlist-metafiles",
        "--no-write-subs",
        "--no-write-auto-subs",
        "--quiet",
        "--no-warnings",
        "--no-mtime",
        "--retries",
        "0",
        "--fragment-retries",
        "0",
        "--default-search",
        "error",
        "--no-cookies",
        "--proxy",
        proxy,
        "--socket-timeout",
        str(max(1, socket_timeout)),
        "--cache-dir",
        str(cache_dir),
        "--use-extractors",
        ies,
        "--",
        url,
    ]


def network_download_argv(
    *,
    executable: str,
    url: str,
    provider_id: str,
    format_token: str,
    proxy_url: str,
    socket_timeout: int,
    cache_dir: Path,
    output_template: str,
    max_filesize_bytes: int,
) -> list[str]:
    extractors = PROVIDER_EXTRACTORS.get(provider_id)
    if not extractors:
        raise ValueError("provider_id")
    if max_filesize_bytes < 1:
        raise ValueError("max_filesize")
    proxy = validate_proxy_url(proxy_url)
    ies = ",".join(sorted(extractors))
    return [
        executable,
        "--ignore-config",
        "--no-config-locations",
        "--no-plugin-dirs",
        "--no-js-runtimes",
        "--no-update",
        "--no-playlist",
        "--no-write-info-json",
        "--no-write-description",
        "--no-write-thumbnail",
        "--no-write-comments",
        "--no-write-playlist-metafiles",
        "--no-write-subs",
        "--no-write-auto-subs",
        "--no-part",
        "--no-progress",
        "--quiet",
        "--no-warnings",
        "--no-mtime",
        "--retries",
        "0",
        "--fragment-retries",
        "0",
        "--default-search",
        "error",
        "--no-cookies",
        "--proxy",
        proxy,
        "--socket-timeout",
        str(max(1, socket_timeout)),
        "--cache-dir",
        str(cache_dir),
        "--max-filesize",
        str(max_filesize_bytes),
        "--use-extractors",
        ies,
        "-f",
        format_token,
        "-o",
        output_template,
        "--",
        url,
    ]


def tool_env(*, network: bool = False) -> dict[str, str]:
    # Network tools intentionally omit HTTP_PROXY env — argv --proxy is set by
    # the server. PATH stays minimal; network image should not ship ffmpeg.
    path = "/usr/bin:/bin"
    if network:
        # Prefer isolated bin dir that contains only yt-dlp wrapper if present.
        path = "/opt/fetchnow/bin:/usr/bin:/bin"
    return {
        "PATH": path,
        "LANG": "C",
        "LC_ALL": "C",
    }
