"""Sanitized Premium entitlement audit events."""

from __future__ import annotations

import hashlib
import logging
import uuid

logger = logging.getLogger("fetchnow.premium.audit")


def entitlement_fingerprint(public_id: uuid.UUID) -> str:
    return hashlib.sha256(public_id.bytes).hexdigest()[:16]


def premium_event(
    message: str,
    *,
    entitlement_public_id: uuid.UUID | None = None,
    payment_order_fingerprint: str | None = None,
    outcome: str,
) -> None:
    extra: dict[str, object] = {"outcome": outcome}
    if entitlement_public_id is not None:
        extra["entitlement_fingerprint"] = entitlement_fingerprint(
            entitlement_public_id
        )
    if payment_order_fingerprint is not None:
        extra["payment_order_fingerprint"] = payment_order_fingerprint
    logger.info(message, extra=extra)
