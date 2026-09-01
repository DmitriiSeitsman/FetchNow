"""Premium entitlement issuance and status resolution."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from fetchnow.payments.models import PaymentOrder
from fetchnow.premium.models import PremiumEntitlement
from fetchnow.premium.policy import PremiumCapability, capability_from_entitlement
from fetchnow.premium.repository import PremiumEntitlementRepository


@dataclass(frozen=True, slots=True)
class PremiumStatus:
    capability: PremiumCapability
    entitlement: PremiumEntitlement | None


class PremiumEntitlementService:
    __slots__ = ()

    async def ensure_for_paid_order(
        self, order: PaymentOrder, *, session: AsyncSession
    ) -> tuple[PremiumEntitlement, bool]:
        repo = PremiumEntitlementRepository(session)
        now = await repo.database_now()
        return await repo.ensure_for_paid_order(order, now=now)

    async def status_for_client(
        self, *, anonymous_client_id: uuid.UUID, session: AsyncSession
    ) -> PremiumStatus:
        repo = PremiumEntitlementRepository(session)
        now = await repo.database_now()
        entitlement = await repo.get_active_for_client(
            anonymous_client_id=anonymous_client_id, now=now
        )
        return PremiumStatus(
            capability=capability_from_entitlement(entitlement),
            entitlement=entitlement,
        )

    @staticmethod
    def public_dict(status: PremiumStatus, *, now: datetime) -> dict[str, Any]:
        capability = status.capability
        if not capability.is_premium or capability.premium_expires_at is None:
            return {"active": False}
        expires_at = capability.premium_expires_at
        remaining_seconds = max(
            0, int((expires_at - now).total_seconds())
        )
        return {
            "active": True,
            "expiresAt": expires_at.isoformat().replace("+00:00", "Z"),
            "productCode": capability.product_code,
            "remainingSeconds": remaining_seconds,
        }
