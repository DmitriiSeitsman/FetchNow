"""Central effective download policy projection."""

from __future__ import annotations

from dataclasses import dataclass

from fetchnow.core.config import Settings


@dataclass(frozen=True, slots=True)
class EffectiveDownloadPolicy:
    tier: str
    download_limit: int
    window_seconds: int
    delivery_rate_bytes_per_second: int | None
    allow_combined: bool = True
    allow_audio_only: bool = False
    allow_video_only: bool = False

    @property
    def audio_only_allowed(self) -> bool:
        """Compatibility projection for the existing product vocabulary."""
        return self.allow_audio_only

    @property
    def video_only_allowed(self) -> bool:
        """Compatibility projection for the existing product vocabulary."""
        return self.allow_video_only


def effective_download_policy(settings: Settings) -> EffectiveDownloadPolicy:
    """B2 resolves every anonymous client to the real Free policy only."""
    return EffectiveDownloadPolicy(
        tier="free",
        download_limit=int(settings.free_download_limit),
        window_seconds=int(settings.free_download_window_seconds),
        delivery_rate_bytes_per_second=(
            int(settings.free_delivery_rate_bytes_per_second)
            if settings.free_delivery_rate_limit_enabled
            else None
        ),
    )
