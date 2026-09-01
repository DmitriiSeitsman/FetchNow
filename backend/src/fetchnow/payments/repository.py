"""Transactional persistence for Robokassa payment orders."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fetchnow.payments.catalog import PaymentProduct
from fetchnow.payments.errors import (
    IdempotencyConflictError,
    PaymentInvariantError,
)
from fetchnow.payments.models import PaymentOrder
from fetchnow.payments.states import PaymentOrderState, assert_payment_transition


class PaymentOrderRepository:
    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def database_now(self) -> datetime:
        value = await self._session.scalar(select(func.clock_timestamp()))
        if not isinstance(value, datetime):
            raise PaymentInvariantError()
        return value

    async def create_or_get(
        self,
        *,
        anonymous_client_id: uuid.UUID,
        idempotency_hash: bytes,
        product: PaymentProduct,
        receipt_json: str,
        now: datetime,
        ttl_seconds: int,
    ) -> tuple[PaymentOrder, bool]:
        if len(idempotency_hash) != 32:
            raise PaymentInvariantError()
        row = PaymentOrder(
            id=uuid.uuid4(),
            public_id=uuid.uuid4(),
            anonymous_client_id=anonymous_client_id,
            provider="robokassa",
            product_code=product.code,
            amount_minor=product.amount_minor,
            currency=product.currency,
            entitlement_duration_seconds=product.entitlement_duration_seconds,
            status=PaymentOrderState.CREATED.value,
            is_test=True,
            receipt_json=receipt_json,
            creation_idempotency_hash=idempotency_hash,
            created_at=now,
            updated_at=now,
            pending_at=None,
            paid_at=None,
            provider_callback_at=None,
            expires_at=now + timedelta(seconds=ttl_seconds),
            provider_operation_key=None,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush()
            return row, True
        except IntegrityError:
            existing = await self._session.scalar(
                select(PaymentOrder).where(
                    PaymentOrder.anonymous_client_id == anonymous_client_id,
                    PaymentOrder.creation_idempotency_hash == idempotency_hash,
                )
            )
            if existing is None:
                raise PaymentInvariantError() from None
            if not self._immutable_matches(
                existing, product=product, receipt_json=receipt_json
            ):
                raise IdempotencyConflictError() from None
            return existing, False

    @staticmethod
    def _immutable_matches(
        row: PaymentOrder, *, product: PaymentProduct, receipt_json: str
    ) -> bool:
        return (
            row.provider == "robokassa"
            and row.product_code == product.code
            and row.amount_minor == product.amount_minor
            and row.currency == product.currency
            and row.entitlement_duration_seconds == product.entitlement_duration_seconds
            and row.is_test is True
            and row.receipt_json == receipt_json
        )

    async def mark_pending(self, row: PaymentOrder, *, now: datetime) -> None:
        if row.status == PaymentOrderState.PENDING.value:
            return
        assert_payment_transition(row.status, PaymentOrderState.PENDING)
        row.status = PaymentOrderState.PENDING.value
        row.pending_at = now
        row.updated_at = now
        await self._session.flush()

    async def get_by_public_id_for_client(
        self, *, public_id: uuid.UUID, anonymous_client_id: uuid.UUID
    ) -> PaymentOrder | None:
        value = await self._session.scalar(
            select(PaymentOrder).where(
                PaymentOrder.public_id == public_id,
                PaymentOrder.anonymous_client_id == anonymous_client_id,
            )
        )
        return value if isinstance(value, PaymentOrder) else None

    async def get_by_invoice_id(self, inv_id: int) -> PaymentOrder | None:
        value = await self._session.scalar(
            select(PaymentOrder).where(PaymentOrder.provider_invoice_id == inv_id)
        )
        return value if isinstance(value, PaymentOrder) else None

    async def lock_by_invoice_id(self, inv_id: int) -> PaymentOrder | None:
        value = await self._session.scalar(
            select(PaymentOrder)
            .where(PaymentOrder.provider_invoice_id == inv_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return value if isinstance(value, PaymentOrder) else None

    async def mark_paid(self, row: PaymentOrder, *, now: datetime) -> bool:
        if row.status == PaymentOrderState.PAID.value:
            return False
        assert_payment_transition(row.status, PaymentOrderState.PAID)
        row.status = PaymentOrderState.PAID.value
        row.paid_at = now
        row.provider_callback_at = now
        row.updated_at = now
        await self._session.flush()
        return True

    async def expire_unpaid(self, *, now: datetime) -> int:
        result = await self._session.execute(
            update(PaymentOrder)
            .where(
                PaymentOrder.status.in_(
                    [
                        PaymentOrderState.CREATED.value,
                        PaymentOrderState.PENDING.value,
                    ]
                ),
                PaymentOrder.expires_at <= now,
            )
            .values(status=PaymentOrderState.EXPIRED.value, updated_at=now)
        )
        return int(result.rowcount or 0)
