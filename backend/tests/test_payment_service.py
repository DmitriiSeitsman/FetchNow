"""Payment-service transaction and trust-boundary tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from fetchnow.core.config import Settings
from fetchnow.payments.catalog import get_product
from fetchnow.payments.errors import (
    InvalidCallbackError,
    InvalidIdempotencyKeyError,
    UnknownProductError,
)
from fetchnow.payments.idempotency import hash_idempotency_key
from fetchnow.payments.models import PaymentOrder
from fetchnow.payments.robokassa import (
    parse_callback_out_sum,
    serialize_receipt,
    sign_callback,
)
from fetchnow.payments.service import PaymentService
from fetchnow.payments.states import PaymentOrderState, assert_payment_transition


@pytest.fixture(autouse=True)
def _stub_premium_issuance(monkeypatch: pytest.MonkeyPatch) -> None:
    from fetchnow.payments import service as service_module

    async def _noop_ensure_for_paid_order(
        *_args: object, **_kwargs: object
    ) -> tuple[None, bool]:
        return None, False

    monkeypatch.setattr(
        service_module.PremiumEntitlementService,
        "ensure_for_paid_order",
        _noop_ensure_for_paid_order,
    )


def _settings() -> Settings:
    return Settings(
        APP_ENV="test",
        ROBOKASSA_MODE="test",
        ROBOKASSA_MERCHANT_LOGIN="demo",
        ROBOKASSA_SIGNATURE_ALGORITHM="sha256",
        ROBOKASSA_TEST_PASSWORD1="password-one",
        ROBOKASSA_TEST_PASSWORD2="password-two",
        ROBOKASSA_TEST_AMOUNT_MINOR=1_000,
        ROBOKASSA_RECEIPT_TAX="none",
        ROBOKASSA_RECEIPT_PAYMENT_METHOD="full_payment",
    )


def _order(*, state: str = "pending", amount: int = 1_000) -> PaymentOrder:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    product = get_product(_settings(), "premium_24h")
    return PaymentOrder(
        id=uuid.uuid4(),
        public_id=uuid.uuid4(),
        anonymous_client_id=uuid.uuid4(),
        provider="robokassa",
        provider_invoice_id=123,
        product_code="premium_24h",
        amount_minor=amount,
        currency="RUB",
        entitlement_duration_seconds=86_400,
        status=state,
        is_test=True,
        receipt_json=serialize_receipt(product).json_text,
        creation_idempotency_hash=b"i" * 32,
        created_at=now,
        updated_at=now,
        pending_at=now if state != "created" else None,
        paid_at=now if state == "paid" else None,
        provider_callback_at=now if state == "paid" else None,
        expires_at=now + timedelta(hours=1),
        provider_operation_key=None,
    )


def test_payment_form_is_server_generated_test_only_and_secret_safe() -> None:
    service = PaymentService(_settings())
    fields = service._form_fields(_order())
    assert fields["IsTest"] == "1"
    assert fields["OutSum"] == "10.00"
    assert fields["InvId"] == "123"
    assert fields["Receipt"].startswith("%7B%22items%22")
    assert "password-one" not in repr(fields)
    assert "password-two" not in repr(fields)
    assert len(fields["SignatureValue"]) == 64
    receipt = serialize_receipt(get_product(_settings(), "premium_24h"))
    assert fields["Receipt"] == receipt.url_encoded
    assert '"sum":10.00' in receipt.json_text
    assert parse_callback_out_sum(fields["OutSum"]) == 1_000


def test_unknown_sku_is_rejected_and_catalog_owns_price() -> None:
    settings = _settings()
    with pytest.raises(UnknownProductError):
        get_product(settings, "client_price_1")
    product = get_product(settings, "premium_24h")
    assert product.amount_minor == settings.robokassa_test_amount_minor == 1_000
    assert product.currency == "RUB"
    assert product.entitlement_duration_seconds == 86_400


def test_idempotency_key_is_domain_hashed_and_never_stored_raw() -> None:
    key = "A" * 32
    digest = hash_idempotency_key(key)
    assert len(digest) == 32
    assert key.encode() not in digest
    assert digest == hash_idempotency_key(key)
    with pytest.raises(InvalidIdempotencyKeyError):
        hash_idempotency_key("short")


class _CallbackRepo:
    row: PaymentOrder
    lock_calls = 0

    def __init__(self, _session: object) -> None:
        pass

    async def get_by_invoice_id(self, inv_id: int) -> PaymentOrder | None:
        assert inv_id == 123
        return self.row

    async def lock_by_invoice_id(self, inv_id: int) -> PaymentOrder | None:
        assert inv_id == 123
        type(self).lock_calls += 1
        return self.row

    async def database_now(self) -> datetime:
        return datetime(2026, 9, 1, 0, 1, tzinfo=UTC)

    async def mark_paid(self, row: PaymentOrder, *, now: datetime) -> bool:
        if row.status == "paid":
            return False
        assert_payment_transition(row.status, PaymentOrderState.PAID)
        row.status = "paid"
        row.paid_at = now
        row.provider_callback_at = now
        return True


@pytest.mark.asyncio
async def test_valid_callback_transitions_once_and_duplicate_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fetchnow.payments import service as service_module

    repo = _CallbackRepo
    repo.row = _order()
    repo.lock_calls = 0
    monkeypatch.setattr(service_module, "PaymentOrderRepository", repo)
    service = PaymentService(_settings())
    signature = sign_callback(
        raw_out_sum="10.000000", inv_id=123, password2="password-two"
    )
    first = await service.accept_callback(
        raw_out_sum="10.000000",
        inv_id=123,
        supplied_signature=signature,
        session=object(),  # type: ignore[arg-type]
    )
    second = await service.accept_callback(
        raw_out_sum="10.000000",
        inv_id=123,
        supplied_signature=signature,
        session=object(),  # type: ignore[arg-type]
    )
    assert first.transitioned is True
    assert second.transitioned is False
    assert repo.row.status == "paid"
    assert repo.lock_calls == 2


@pytest.mark.asyncio
async def test_invalid_replay_against_paid_is_still_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fetchnow.payments import service as service_module

    repo = _CallbackRepo
    repo.row = _order(state="paid")
    repo.lock_calls = 0
    monkeypatch.setattr(service_module, "PaymentOrderRepository", repo)
    with pytest.raises(InvalidCallbackError):
        await PaymentService(_settings()).accept_callback(
            raw_out_sum="10.00",
            inv_id=123,
            supplied_signature="0" * 64,
            session=object(),  # type: ignore[arg-type]
        )
    assert repo.lock_calls == 0


@pytest.mark.asyncio
async def test_password1_cannot_authenticate_resulturl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fetchnow.payments import service as service_module

    repo = _CallbackRepo
    repo.row = _order()
    repo.lock_calls = 0
    monkeypatch.setattr(service_module, "PaymentOrderRepository", repo)
    wrong = sign_callback(raw_out_sum="10.00", inv_id=123, password2="password-one")
    with pytest.raises(InvalidCallbackError):
        await PaymentService(_settings()).accept_callback(
            raw_out_sum="10.00",
            inv_id=123,
            supplied_signature=wrong,
            session=object(),  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_valid_signature_with_wrong_amount_is_rejected_before_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fetchnow.payments import service as service_module

    repo = _CallbackRepo
    repo.row = _order()
    repo.lock_calls = 0
    monkeypatch.setattr(service_module, "PaymentOrderRepository", repo)
    signature = sign_callback(raw_out_sum="11.00", inv_id=123, password2="password-two")
    with pytest.raises(InvalidCallbackError):
        await PaymentService(_settings()).accept_callback(
            raw_out_sum="11.00",
            inv_id=123,
            supplied_signature=signature,
            session=object(),  # type: ignore[arg-type]
        )
    assert repo.lock_calls == 0


@pytest.mark.parametrize(
    ("source", "target", "allowed"),
    [
        ("created", PaymentOrderState.PENDING, True),
        ("created", PaymentOrderState.EXPIRED, True),
        ("pending", PaymentOrderState.PAID, True),
        ("pending", PaymentOrderState.EXPIRED, True),
        ("paid", PaymentOrderState.EXPIRED, False),
        ("expired", PaymentOrderState.PAID, False),
    ],
)
def test_state_machine_expiry_and_paid_terminal(
    source: str, target: PaymentOrderState, allowed: bool
) -> None:
    if allowed:
        assert_payment_transition(source, target)
    else:
        with pytest.raises(ValueError):
            assert_payment_transition(source, target)
