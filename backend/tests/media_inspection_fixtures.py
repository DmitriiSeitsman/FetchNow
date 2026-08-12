"""Minimized yt-dlp-shaped fixtures (no signed/CDN URLs, no copyrighted blobs)."""

from __future__ import annotations

import json

VK_FIXTURE: dict[str, object] = {
    "id": "-123_456239017",
    "title": "Sample VK clip",
    "duration": 42,
    "extractor": "vk",
    "extractor_key": "vk",
    "webpage_url": "https://vk.com/video-123_456239017",
    "original_url": "https://vk.com/video-123_456239017",
    "formats": [
        {
            "format_id": "url720",
            "ext": "mp4",
            "width": 1280,
            "height": 720,
            "fps": 30,
            "vcodec": "avc1.64001F",
            "acodec": "mp4a.40.2",
            "filesize": 5_000_000,
            "url": "https://example.invalid/signed/PLACEHOLDER_NOT_REAL",
        },
        {
            "format_id": "url1080",
            "ext": "mp4",
            "width": 1920,
            "height": 1080,
            "fps": 30,
            "vcodec": "avc1.640028",
            "acodec": "mp4a.40.2",
            "filesize": 12_000_000,
            "url": "https://cdn.example.invalid/direct/PLACEHOLDER",
        },
        {
            "format_id": "url360",
            "ext": "mp4",
            "width": 640,
            "height": 360,
            "fps": 30,
            "vcodec": "avc1.4D401E",
            "acodec": "mp4a.40.2",
            "filesize": 2_000_000,
            "url": "https://example.invalid/360/PLACEHOLDER",
        },
        {
            "format_id": "dash-video",
            "ext": "mp4",
            "width": 1280,
            "height": 720,
            "fps": 30,
            "vcodec": "avc1.64001F",
            "acodec": "none",
            "filesize_approx": 4_000_000,
            "url": "https://example.invalid/vonly/PLACEHOLDER",
        },
        {
            "format_id": "dash-audio",
            "ext": "m4a",
            "vcodec": "none",
            "acodec": "mp4a.40.2",
            "filesize_approx": 500_000,
            "url": "https://example.invalid/aonly/PLACEHOLDER",
        },
    ],
}

RUTUBE_FIXTURE: dict[str, object] = {
    "id": "abc123def456",
    "title": "Sample Rutube video",
    "duration": 120,
    "extractor": "rutube",
    "extractor_key": "rutube",
    "webpage_url": "https://rutube.ru/video/abc123def456/",
    "original_url": "https://rutube.ru/video/abc123def456/",
    "formats": [
        {
            "format_id": "m3u8-720",
            "ext": "mp4",
            "width": 1280,
            "height": 720,
            "fps": 25,
            "vcodec": "avc1.64001F",
            "acodec": "mp4a.40.2",
            "filesize_approx": 8_000_000,
            "url": "https://rutube.example.invalid/hls/PLACEHOLDER.m3u8",
        },
        {
            "format_id": "m3u8-480",
            "ext": "mp4",
            "width": 854,
            "height": 480,
            "fps": 25,
            "vcodec": "avc1.4D401F",
            "acodec": "mp4a.40.2",
            "filesize_approx": 4_000_000,
            "url": "https://rutube.example.invalid/hls/PLACEHOLDER480.m3u8",
        },
    ],
}


def dumps_fixture(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )
