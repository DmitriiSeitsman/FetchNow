"""Server-authoritative A1 payment product catalog."""

from __future__ import annotations

from dataclasses import dataclass

from fetchnow.core.config import Settings
from fetchnow.payments.errors import UnknownProductError

PRODUCT_CODE = "premium_24h"
PRODUCT_DESCRIPTION = "Доступ к функциям FetchNow на 24 часа"


@dataclass(frozen=True, slots=True)
class PaymentProduct:
    code: str
    amount_minor: int
    currency: str
    entitlement_duration_seconds: int
    receipt_name: str
    receipt_tax: str
    receipt_payment_method: str
    receipt_payment_object: str = "service"


def get_product(settings: Settings, product_code: str) -> PaymentProduct:
    if product_code != PRODUCT_CODE:
        raise UnknownProductError()
    if settings.robokassa_mode != "test" or settings.robokassa_test_amount_minor <= 0:
        raise UnknownProductError()
    return PaymentProduct(
        code=PRODUCT_CODE,
        amount_minor=settings.robokassa_test_amount_minor,
        currency="RUB",
        entitlement_duration_seconds=86_400,
        receipt_name=PRODUCT_DESCRIPTION,
        receipt_tax=settings.robokassa_receipt_tax,
        receipt_payment_method=settings.robokassa_receipt_payment_method,
    )
