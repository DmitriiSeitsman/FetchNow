"""Authenticated private artifact delivery (PR7).

Delivery is a dedicated process boundary: it authorizes via the parent MediaJob
Bearer token, opens published artifacts read-only through descriptor-relative
``O_NOFOLLOW`` paths, and streams bytes from an already-open FD. It never runs
yt-dlp, never mutates artifacts, and never exposes storage identifiers.
"""

from __future__ import annotations

__all__: list[str] = []
