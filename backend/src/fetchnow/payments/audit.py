"""Sanitized payment audit events."""

from __future__ import annotations

import hashlib
import logging
import uuid

logger = logging.getLogger("fetchnow.payments.audit")


def order_fingerprint(public_id: uuid.UUID) -> str:
    return hashlib.sha256(public_id.bytes).hexdigest()[:16]


def payment_event(
    message: str,
    *,
    public_id: uuid.UUID | None = None,
    provider_invoice_id: int | None = None,
    previous_status: str | None = None,
    new_status: str | None = None,
    outcome: str,
    signature_valid: bool | None = None,
    amount_match: bool | None = None,
    is_test: bool = True,
) -> None:
    extra: dict[str, object] = {
        "outcome": outcome,
        "provider_id": "robokassa",
        "is_test": is_test,
    }
    if public_id is not None:
        extra["payment_order_fingerprint"] = order_fingerprint(public_id)
    if provider_invoice_id is not None:
        extra["provider_invoice_id"] = provider_invoice_id
    if previous_status is not None:
        extra["previous_status"] = previous_status
    if new_status is not None:
        extra["new_status"] = new_status
    if signature_valid is not None:
        extra["signature_valid"] = signature_valid
    if amount_match is not None:
        extra["amount_match"] = amount_match
    logger.info(message, extra=extra)
