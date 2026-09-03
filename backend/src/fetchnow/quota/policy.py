"""Central effective download policy projection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from fetchnow.core.config import Settings
from fetchnow.premium.policy import PremiumCapability


@dataclass(frozen=True, slots=True)
class EffectiveDownloadPolicy:
    tier: str
    download_limit: int | None
    quota_window_seconds: int | None
    delivery_rate_bytes_per_second: int | None
    premium_expires_at: datetime | None
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

    @property
    def window_seconds(self) -> int | None:
        """Compatibility alias for existing quota repository callers."""
        return self.quota_window_seconds


def effective_download_policy(
    settings: Settings,
    capability: PremiumCapability | None = None,
) -> EffectiveDownloadPolicy:
    """Project the single effective server-side download policy."""
    if capability is not None and capability.is_premium:
        if capability.premium_expires_at is None:
            raise ValueError("active Premium capability requires an expiry")
        return EffectiveDownloadPolicy(
            tier="premium",
            download_limit=None,
            quota_window_seconds=None,
            delivery_rate_bytes_per_second=None,
            premium_expires_at=capability.premium_expires_at,
        )
    return EffectiveDownloadPolicy(
        tier="free",
        download_limit=int(settings.free_download_limit),
        quota_window_seconds=int(settings.free_download_window_seconds),
        delivery_rate_bytes_per_second=(
            int(settings.free_delivery_rate_bytes_per_second)
            if settings.free_delivery_rate_limit_enabled
            else None
        ),
        premium_expires_at=None,
    )
