"""Central effective download policy projection."""

from __future__ import annotations

from dataclasses import dataclass

from fetchnow.core.config import Settings


@dataclass(frozen=True, slots=True)
class EffectiveDownloadPolicy:
    tier: str
    download_limit: int
    window_seconds: int
    audio_only_allowed: bool = False
    video_only_allowed: bool = False


def effective_download_policy(settings: Settings) -> EffectiveDownloadPolicy:
    """B2 resolves every anonymous client to the real Free policy only."""
    return EffectiveDownloadPolicy(
        tier="free",
        download_limit=int(settings.free_download_limit),
        window_seconds=int(settings.free_download_window_seconds),
    )
