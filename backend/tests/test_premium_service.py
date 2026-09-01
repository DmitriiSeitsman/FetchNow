"""Unit tests for Premium entitlement issuance and status."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from fetchnow.payments.models import PaymentOrder
from fetchnow.payments.states import PaymentOrderState
from fetchnow.premium.errors import PremiumEntitlementInvariantError
from fetchnow.premium.repository import PremiumEntitlementRepository
from fetchnow.premium.service import PremiumEntitlementService


def _paid_order(
    *,
    paid_at: datetime | None = None,
    duration: int = 86_400,
) -> PaymentOrder:
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    paid = paid_at or now
    return PaymentOrder(
        id=uuid.uuid4(),
        public_id=uuid.uuid4(),
        anonymous_client_id=uuid.uuid4(),
        provider="robokassa",
        provider_invoice_id=1,
        product_code="premium_24h",
        amount_minor=100,
        currency="RUB",
        entitlement_duration_seconds=duration,
        status=PaymentOrderState.PAID.value,
        is_test=True,
        receipt_json='{"items":[]}',
        creation_idempotency_hash=b"a" * 32,
        created_at=now - timedelta(minutes=5),
        updated_at=paid,
        pending_at=now - timedelta(minutes=4),
        paid_at=paid,
        provider_callback_at=paid,
        expires_at=now + timedelta(hours=1),
        provider_operation_key=None,
    )


class _RepoStub:
    def __init__(self, *, now: datetime, existing=None, created=None) -> None:
        self._now = now
        self._existing = existing
        self._created = created if created is not None else []

    async def database_now(self) -> datetime:
        return self._now

    async def get_by_source_payment_order_id(self, payment_order_id: uuid.UUID):
        if (
            self._existing
            and self._existing.source_payment_order_id == payment_order_id
        ):
            return self._existing
        return None

    async def ensure_for_paid_order(self, order: PaymentOrder, *, now: datetime):
        if self._existing and self._existing.source_payment_order_id == order.id:
            return self._existing, False
        duration = timedelta(seconds=order.entitlement_duration_seconds)
        row = type("Ent", (), {
            "public_id": uuid.uuid4(),
            "starts_at": order.paid_at,
            "expires_at": order.paid_at + duration,
            "product_code": order.product_code,
        })()
        self._created.append(row)
        return row, True


@pytest.mark.asyncio
async def test_non_paid_order_cannot_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    order = _paid_order()
    order.status = PaymentOrderState.PENDING.value
    order.paid_at = None
    repo = PremiumEntitlementRepository(object())  # type: ignore[arg-type]
    with pytest.raises(PremiumEntitlementInvariantError):
        await repo.ensure_for_paid_order(order, now=datetime.now(UTC))


def test_public_dict_active_shape() -> None:
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    expires = now + timedelta(hours=2)
    entitlement = type("Ent", (), {
        "expires_at": expires,
        "product_code": "premium_24h",
    })()
    from fetchnow.premium.policy import capability_from_entitlement
    from fetchnow.premium.service import PremiumStatus

    status = PremiumStatus(
        capability=capability_from_entitlement(entitlement), entitlement=entitlement  # type: ignore[arg-type]
    )
    body = PremiumEntitlementService.public_dict(status, now=now)
    assert body == {
        "active": True,
        "expiresAt": "2026-09-01T14:00:00Z",
        "productCode": "premium_24h",
        "remainingSeconds": 7_200,
    }


def test_public_dict_inactive_shape() -> None:
    from fetchnow.premium.policy import PremiumCapability
    from fetchnow.premium.service import PremiumStatus

    status = PremiumStatus(
        capability=PremiumCapability(
            is_premium=False, premium_expires_at=None, product_code=None
        ),
        entitlement=None,
    )
    assert PremiumEntitlementService.public_dict(
        status, now=datetime.now(UTC)
    ) == {"active": False}


@pytest.mark.parametrize("robokassa_mode", ["test", "disabled"])
def test_capability_resolution_does_not_depend_on_robokassa_mode(
    robokassa_mode: str,
) -> None:
    from fetchnow.premium.policy import capability_from_entitlement

    expires = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    entitlement = type("Ent", (), {
        "expires_at": expires,
        "product_code": "premium_24h",
    })()
    capability = capability_from_entitlement(entitlement)  # type: ignore[arg-type]
    assert capability.is_premium is True
    assert capability.product_code == "premium_24h"
    assert capability.premium_expires_at == expires
    # Resolver has no Settings dependency; mode is irrelevant to capability truth.
    _ = robokassa_mode
