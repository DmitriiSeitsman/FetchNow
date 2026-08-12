"""Fixed argv builder for metadata-only yt-dlp invocations."""

from __future__ import annotations


def build_ytdlp_metadata_argv(
    *,
    executable: str,
    url: str,
    allowed_extractors: frozenset[str],
    socket_timeout_seconds: int,
    cache_dir: str,
) -> list[str]:
    """Build a fully server-controlled argv list.

    The URL is always placed after ``--`` so a leading-dash URL cannot become
    an option. Callers must never splice user text into flags. Generic
    extractor names are rejected. Cookies/netrc/config/plugins are disabled
    via fixed flags; ffmpeg/external downloaders are never enabled.
    """
    if not executable or executable.startswith("-"):
        raise ValueError("executable path invalid")
    if not url or not isinstance(url, str):
        raise ValueError("url invalid")
    if not allowed_extractors:
        raise ValueError("allowed extractors empty")
    if any(k.lower() == "generic" for k in allowed_extractors):
        raise ValueError("generic extractor forbidden")
    # Explicit allowlist only — never "default" (would include generic).
    ies = ",".join(sorted(k.lower() for k in allowed_extractors))
    timeout = max(1, int(socket_timeout_seconds))
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
        "--default-search",
        "error",
        "--use-extractors",
        ies,
        "--no-cookies",
        "--",
        url,
    ]
