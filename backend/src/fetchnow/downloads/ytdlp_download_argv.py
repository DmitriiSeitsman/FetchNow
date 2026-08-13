"""Fixed argv builder for progressive yt-dlp download invocations."""

from __future__ import annotations

from fetchnow.downloads.format_token import validate_provider_format_token


def build_ytdlp_download_argv(
    *,
    executable: str,
    url: str,
    provider_format_token: str,
    allowed_extractors: frozenset[str],
    socket_timeout: int,
    cache_dir: str,
    output_template: str,
    max_filesize_bytes: int,
) -> list[str]:
    """Build a fully server-controlled download argv list.

    The URL is always placed after ``--`` so a leading-dash URL cannot become
    an option. ``provider_format_token`` is an argv value only — callers must
    never log it. Generic extractors are rejected. Cookies/netrc/config/plugins
    are disabled. No subtitle/thumbnail/comment writes. No merge/ffmpeg flags —
    progressive exact ``-f <token>`` only.
    """
    if not executable or executable.startswith("-"):
        raise ValueError("executable path invalid")
    if not url or not isinstance(url, str):
        raise ValueError("url invalid")
    try:
        token = validate_provider_format_token(provider_format_token)
    except ValueError as exc:
        raise ValueError("provider format token invalid") from exc
    if not allowed_extractors:
        raise ValueError("allowed extractors empty")
    if any(k.lower() == "generic" for k in allowed_extractors):
        raise ValueError("generic extractor forbidden")
    if not cache_dir or not isinstance(cache_dir, str):
        raise ValueError("cache_dir invalid")
    if (
        not output_template
        or not isinstance(output_template, str)
        or output_template.startswith("-")
        or "\x00" in output_template
    ):
        raise ValueError("output_template invalid")
    if (
        not isinstance(max_filesize_bytes, int)
        or isinstance(max_filesize_bytes, bool)
        or max_filesize_bytes < 1
    ):
        raise ValueError("max_filesize_bytes invalid")

    ies = ",".join(sorted(k.lower() for k in allowed_extractors))
    timeout = max(1, int(socket_timeout))
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
        "--cache-dir",
        cache_dir,
        "--no-mtime",
        "--retries",
        "0",
        "--fragment-retries",
        "0",
        "--socket-timeout",
        str(timeout),
        "--max-filesize",
        f"{max_filesize_bytes}B",
        "--default-search",
        "error",
        "--use-extractors",
        ies,
        "--no-cookies",
        "--no-netrc",
        "-f",
        token,
        "-o",
        output_template,
        "--",
        url,
    ]
