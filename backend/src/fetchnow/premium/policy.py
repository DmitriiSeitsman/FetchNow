"""Server-side Premium capability projection for future policy consumers.

A2 semantics: overlapping entitlements are allowed. Each paid order grants its
own 24h window from that order's paid_at. Premium is active while any
entitlement window is active. Purchases do not extend an existing window.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from fetchnow.premium.models import PremiumEntitlement


@dataclass(frozen=True, slots=True)
class PremiumCapability:
    is_premium: bool
    premium_expires_at: datetime | None
    product_code: str | None


def capability_from_entitlement(
    entitlement: PremiumEntitlement | None,
) -> PremiumCapability:
    if entitlement is None:
        return PremiumCapability(
            is_premium=False, premium_expires_at=None, product_code=None
        )
    return PremiumCapability(
        is_premium=True,
        premium_expires_at=entitlement.expires_at,
        product_code=entitlement.product_code,
    )
