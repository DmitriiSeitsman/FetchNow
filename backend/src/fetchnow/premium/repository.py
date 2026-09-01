"""Transactional persistence for Premium entitlements."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fetchnow.payments.audit import order_fingerprint
from fetchnow.payments.models import PaymentOrder
from fetchnow.payments.states import PaymentOrderState
from fetchnow.premium.audit import premium_event
from fetchnow.premium.errors import PremiumEntitlementInvariantError
from fetchnow.premium.models import PremiumEntitlement


class PremiumEntitlementRepository:
    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def database_now(self) -> datetime:
        value = await self._session.scalar(select(func.clock_timestamp()))
        if not isinstance(value, datetime):
            raise PremiumEntitlementInvariantError()
        return value

    async def get_by_source_payment_order_id(
        self, payment_order_id: uuid.UUID
    ) -> PremiumEntitlement | None:
        value = await self._session.scalar(
            select(PremiumEntitlement).where(
                PremiumEntitlement.source_payment_order_id == payment_order_id
            )
        )
        return value if isinstance(value, PremiumEntitlement) else None

    async def get_active_for_client(
        self, *, anonymous_client_id: uuid.UUID, now: datetime
    ) -> PremiumEntitlement | None:
        value = await self._session.scalar(
            select(PremiumEntitlement)
            .where(
                PremiumEntitlement.anonymous_client_id == anonymous_client_id,
                PremiumEntitlement.starts_at <= now,
                PremiumEntitlement.expires_at > now,
            )
            .order_by(PremiumEntitlement.expires_at.desc())
            .limit(1)
        )
        return value if isinstance(value, PremiumEntitlement) else None

    async def ensure_for_paid_order(
        self, order: PaymentOrder, *, now: datetime
    ) -> tuple[PremiumEntitlement, bool]:
        if (
            order.status != PaymentOrderState.PAID.value
            or order.paid_at is None
            or order.entitlement_duration_seconds <= 0
        ):
            raise PremiumEntitlementInvariantError()
        existing = await self.get_by_source_payment_order_id(order.id)
        if existing is not None:
            premium_event(
                "premium_entitlement_reused",
                entitlement_public_id=existing.public_id,
                payment_order_fingerprint=order_fingerprint(order.public_id),
                outcome="reused",
            )
            return existing, False
        starts_at = order.paid_at
        expires_at = starts_at + timedelta(seconds=order.entitlement_duration_seconds)
        if expires_at <= starts_at:
            raise PremiumEntitlementInvariantError()
        row = PremiumEntitlement(
            id=uuid.uuid4(),
            public_id=uuid.uuid4(),
            anonymous_client_id=order.anonymous_client_id,
            source_payment_order_id=order.id,
            product_code=order.product_code,
            starts_at=starts_at,
            expires_at=expires_at,
            created_at=now,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush()
        except IntegrityError:
            existing = await self.get_by_source_payment_order_id(order.id)
            if existing is None:
                raise PremiumEntitlementInvariantError() from None
            premium_event(
                "premium_entitlement_reused",
                entitlement_public_id=existing.public_id,
                payment_order_fingerprint=order_fingerprint(order.public_id),
                outcome="reused",
            )
            return existing, False
        premium_event(
            "premium_entitlement_created",
            entitlement_public_id=row.public_id,
            payment_order_fingerprint=order_fingerprint(order.public_id),
            outcome="created",
        )
        return row, True
