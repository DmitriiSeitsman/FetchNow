"""Fail-closed payment-order state machine."""

from __future__ import annotations

from enum import StrEnum


class PaymentOrderState(StrEnum):
    CREATED = "created"
    PENDING = "pending"
    PAID = "paid"
    EXPIRED = "expired"


_ALLOWED = {
    PaymentOrderState.CREATED: frozenset(
        {PaymentOrderState.PENDING, PaymentOrderState.EXPIRED}
    ),
    PaymentOrderState.PENDING: frozenset(
        {PaymentOrderState.PAID, PaymentOrderState.EXPIRED}
    ),
    PaymentOrderState.PAID: frozenset(),
    PaymentOrderState.EXPIRED: frozenset(),
}


def assert_payment_transition(source: str, target: PaymentOrderState) -> None:
    try:
        current = PaymentOrderState(source)
    except ValueError as exc:
        raise ValueError("unknown payment order state") from exc
    if target not in _ALLOWED[current]:
        raise ValueError("illegal payment order state transition")
