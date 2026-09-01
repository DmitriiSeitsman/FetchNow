"""Stable internal payment error categories."""

from __future__ import annotations


class PaymentError(Exception):
    code = "PAYMENT_ERROR"


class PaymentsDisabledError(PaymentError):
    code = "PAYMENTS_DISABLED"


class PaymentIdentityRequiredError(PaymentError):
    code = "PAYMENT_IDENTITY_REQUIRED"


class UnknownProductError(PaymentError):
    code = "PAYMENT_PRODUCT_UNKNOWN"


class InvalidIdempotencyKeyError(PaymentError):
    code = "PAYMENT_IDEMPOTENCY_KEY_INVALID"


class IdempotencyConflictError(PaymentError):
    code = "PAYMENT_IDEMPOTENCY_CONFLICT"


class PaymentOrderNotFoundError(PaymentError):
    code = "PAYMENT_ORDER_NOT_FOUND"


class InvalidCallbackError(PaymentError):
    code = "PAYMENT_CALLBACK_INVALID"


class PaymentInvariantError(PaymentError):
    code = "PAYMENT_INVARIANT_VIOLATION"
