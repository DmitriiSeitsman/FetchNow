"""One-time reconciliation for PAID orders that predate Premium issuance."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fetchnow.payments.audit import order_fingerprint
from fetchnow.payments.models import PaymentOrder
from fetchnow.payments.states import PaymentOrderState
from fetchnow.premium.audit import premium_event
from fetchnow.premium.models import PremiumEntitlement
from fetchnow.premium.repository import PremiumEntitlementRepository


@dataclass(frozen=True, slots=True)
class PremiumReconcileResult:
    scanned: int
    created: int
    reused: int


async def reconcile_paid_orders_without_entitlement(
    session: AsyncSession,
) -> PremiumReconcileResult:
    """Grant entitlements for historical PAID orders missing a row.

  Idempotent and transaction-safe: safe to rerun; creates at most one
  entitlement per payment order.
    """
    repo = PremiumEntitlementRepository(session)
    now = await repo.database_now()
    rows = (
        await session.scalars(
            select(PaymentOrder)
            .outerjoin(
                PremiumEntitlement,
                PremiumEntitlement.source_payment_order_id == PaymentOrder.id,
            )
            .where(
                PaymentOrder.status == PaymentOrderState.PAID.value,
                PremiumEntitlement.id.is_(None),
            )
            .order_by(PaymentOrder.paid_at.asc())
        )
    ).all()
    created = 0
    reused = 0
    for order in rows:
        if not isinstance(order, PaymentOrder):
            continue
        _, was_created = await repo.ensure_for_paid_order(order, now=now)
        if was_created:
            created += 1
            premium_event(
                "premium_entitlement_reconciled",
                payment_order_fingerprint=order_fingerprint(order.public_id),
                outcome="created",
            )
        else:
            reused += 1
    return PremiumReconcileResult(
        scanned=len(rows), created=created, reused=reused
    )
