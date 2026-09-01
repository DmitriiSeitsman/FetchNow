"""Server-authoritative payment creation, projection, and callback handling."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from fetchnow.core.config import Settings
from fetchnow.payments.catalog import PRODUCT_CODE, get_product
from fetchnow.payments.errors import (
    InvalidCallbackError,
    PaymentInvariantError,
    PaymentOrderNotFoundError,
    PaymentsDisabledError,
)
from fetchnow.payments.idempotency import hash_idempotency_key
from fetchnow.payments.models import PaymentOrder
from fetchnow.payments.repository import PaymentOrderRepository
from fetchnow.payments.robokassa import (
    PAYMENT_ACTION,
    encode_receipt_json,
    format_out_sum,
    parse_callback_out_sum,
    serialize_receipt,
    sign_callback,
    sign_initiation,
    signature_matches,
)
from fetchnow.payments.states import PaymentOrderState


@dataclass(frozen=True, slots=True)
class CreatedPayment:
    order: PaymentOrder
    fields: dict[str, str]
    created: bool


@dataclass(frozen=True, slots=True)
class CallbackResult:
    inv_id: int
    transitioned: bool


class PaymentService:
    __slots__ = ("_settings",)

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _require_test_mode(self) -> None:
        if self._settings.robokassa_mode != "test":
            raise PaymentsDisabledError()

    def ensure_available(self) -> None:
        self._require_test_mode()

    async def create_order(
        self,
        *,
        anonymous_client_id: uuid.UUID,
        product_code: str,
        idempotency_key: str,
        session: AsyncSession,
    ) -> CreatedPayment:
        self._require_test_mode()
        product = get_product(self._settings, product_code)
        receipt = serialize_receipt(product)
        repo = PaymentOrderRepository(session)
        now = await repo.database_now()
        row, created = await repo.create_or_get(
            anonymous_client_id=anonymous_client_id,
            idempotency_hash=hash_idempotency_key(idempotency_key),
            product=product,
            receipt_json=receipt.json_text,
            now=now,
            ttl_seconds=self._settings.robokassa_order_ttl_seconds,
        )
        if row.status == PaymentOrderState.CREATED.value:
            await repo.mark_pending(row, now=now)
        elif row.status != PaymentOrderState.PENDING.value:
            raise PaymentInvariantError()
        return CreatedPayment(row, self._form_fields(row), created)

    def _form_fields(self, row: PaymentOrder) -> dict[str, str]:
        self._require_test_mode()
        if (
            row.provider != "robokassa"
            or row.product_code != PRODUCT_CODE
            or row.currency != "RUB"
            or row.is_test is not True
            or row.provider_invoice_id <= 0
        ):
            raise PaymentInvariantError()
        out_sum = format_out_sum(row.amount_minor)
        encoded_receipt = encode_receipt_json(row.receipt_json)
        signature = sign_initiation(
            merchant_login=self._settings.robokassa_merchant_login,
            out_sum=out_sum,
            inv_id=row.provider_invoice_id,
            encoded_receipt=encoded_receipt,
            password1=self._settings.robokassa_test_password1.get_secret_value(),
        )
        return {
            "MerchantLogin": self._settings.robokassa_merchant_login,
            "OutSum": out_sum,
            "InvId": str(row.provider_invoice_id),
            "Description": "Доступ FetchNow на 24 часа",
            "SignatureValue": signature,
            "IsTest": "1",
            "Receipt": encoded_receipt,
            "Culture": "ru",
        }

    async def get_order(
        self,
        *,
        public_id: uuid.UUID,
        anonymous_client_id: uuid.UUID,
        session: AsyncSession,
    ) -> PaymentOrder:
        self._require_test_mode()
        row = await PaymentOrderRepository(session).get_by_public_id_for_client(
            public_id=public_id, anonymous_client_id=anonymous_client_id
        )
        if row is None:
            raise PaymentOrderNotFoundError()
        return row

    async def accept_callback(
        self,
        *,
        raw_out_sum: str,
        inv_id: int,
        supplied_signature: str,
        session: AsyncSession,
    ) -> CallbackResult:
        self._require_test_mode()
        repo = PaymentOrderRepository(session)
        snapshot = await repo.get_by_invoice_id(inv_id)
        if snapshot is None:
            raise InvalidCallbackError()
        expected = sign_callback(
            raw_out_sum=raw_out_sum,
            inv_id=inv_id,
            password2=self._settings.robokassa_test_password2.get_secret_value(),
        )
        if not signature_matches(supplied=supplied_signature, expected=expected):
            raise InvalidCallbackError()
        if parse_callback_out_sum(raw_out_sum) != snapshot.amount_minor:
            raise InvalidCallbackError()
        self._validate_callback_snapshot(snapshot)

        row = await repo.lock_by_invoice_id(inv_id)
        if row is None:
            raise InvalidCallbackError()
        if parse_callback_out_sum(raw_out_sum) != row.amount_minor:
            raise InvalidCallbackError()
        self._validate_callback_snapshot(row)
        now = await repo.database_now()
        transitioned = await repo.mark_paid(row, now=now)
        return CallbackResult(inv_id=inv_id, transitioned=transitioned)

    @staticmethod
    def _validate_callback_snapshot(row: PaymentOrder) -> None:
        if (
            row.provider != "robokassa"
            or row.product_code != PRODUCT_CODE
            or row.currency != "RUB"
            or row.is_test is not True
            or row.entitlement_duration_seconds != 86_400
            or row.status
            not in {PaymentOrderState.PENDING.value, PaymentOrderState.PAID.value}
        ):
            raise InvalidCallbackError()

    @staticmethod
    def public_dict(row: PaymentOrder) -> dict[str, Any]:
        def timestamp(value: datetime | None) -> str | None:
            return (
                value.isoformat().replace("+00:00", "Z") if value is not None else None
            )

        return {
            "status": row.status.upper(),
            "productCode": row.product_code,
            "amountMinor": row.amount_minor,
            "currency": row.currency,
            "createdAt": timestamp(row.created_at),
            "paidAt": timestamp(row.paid_at),
        }

    @staticmethod
    def creation_dict(value: CreatedPayment) -> dict[str, Any]:
        return {
            "orderId": str(value.order.public_id),
            "status": value.order.status.upper(),
            "paymentForm": {
                "action": PAYMENT_ACTION,
                "method": "POST",
                "fields": value.fields,
            },
        }
