"""Public A1 payment API contracts."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from fetchnow.api.main import create_app
from fetchnow.api.v1 import payments
from fetchnow.core.config import Settings
from fetchnow.payments.callback_diagnostic import CallbackDiagnostic
from fetchnow.payments.models import PaymentOrder
from fetchnow.payments.robokassa import PAYMENT_ACTION
from fetchnow.payments.service import CallbackResult, CreatedPayment, PaymentService
from fetchnow.quota.service import AnonymousIdentity


def _settings(*, enabled: bool = True, visible: bool = True) -> Settings:
    if not enabled:
        return Settings(APP_ENV="test", ROBOKASSA_MODE="disabled")
    return Settings(
        APP_ENV="test",
        ROBOKASSA_MODE="test",
        ROBOKASSA_MERCHANT_LOGIN="demo",
        ROBOKASSA_SIGNATURE_ALGORITHM="sha256",
        ROBOKASSA_TEST_PASSWORD1="test-password-one",
        ROBOKASSA_TEST_PASSWORD2="test-password-two",
        ROBOKASSA_TEST_AMOUNT_MINOR=100,
        ROBOKASSA_RECEIPT_TAX="none",
        ROBOKASSA_RECEIPT_PAYMENT_METHOD="full_payment",
        PREMIUM_TEST_CHECKOUT_VISIBLE=visible,
    )


def _row(*, owner: uuid.UUID | None = None, status: str = "pending") -> PaymentOrder:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    return PaymentOrder(
        id=uuid.uuid4(),
        public_id=uuid.uuid4(),
        anonymous_client_id=owner or uuid.uuid4(),
        provider="robokassa",
        provider_invoice_id=42,
        product_code="premium_24h",
        amount_minor=100,
        currency="RUB",
        entitlement_duration_seconds=86_400,
        status=status,
        is_test=True,
        receipt_json='{"items":[]}',
        creation_idempotency_hash=b"x" * 32,
        created_at=now,
        updated_at=now,
        pending_at=now,
        paid_at=now if status == "paid" else None,
        provider_callback_at=now if status == "paid" else None,
        expires_at=datetime(2026, 9, 1, 1, tzinfo=UTC),
        provider_operation_key=None,
    )


class _IdentityService:
    identity_id = uuid.uuid4()

    def __init__(self, _settings: Settings) -> None:
        pass

    async def require_identity(self, **_kwargs: object) -> AnonymousIdentity:
        return AnonymousIdentity(self.identity_id, datetime(2027, 9, 1, tzinfo=UTC))


async def _client(
    monkeypatch: pytest.MonkeyPatch,
    service: object,
    *,
    settings: Settings | None = None,
    commit_error: Exception | None = None,
) -> tuple[AsyncClient, object]:
    configured = settings or _settings()
    app = create_app(configured)
    app.state.settings = configured
    app.state.payment_service = service
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.commit = AsyncMock(side_effect=commit_error)
    app.state.session_factory = MagicMock(return_value=session)
    monkeypatch.setattr(payments, "QuotaService", _IdentityService)
    return (
        AsyncClient(transport=ASGITransport(app=app), base_url="https://test"),
        app,
    )


class _StubPaymentService:
    def __init__(self, row: PaymentOrder) -> None:
        self.row = row
        self.create_order = AsyncMock(
            return_value=CreatedPayment(
                row,
                {
                    "MerchantLogin": "demo",
                    "OutSum": "1.00",
                    "InvId": "42",
                    "Description": "Доступ FetchNow на 24 часа",
                    "SignatureValue": "a" * 64,
                    "IsTest": "1",
                    "Receipt": "%7B%22items%22%3A%5B%5D%7D",
                },
                True,
            )
        )
        self.test_checkout_available = True
        self.get_order = AsyncMock(return_value=row)
        self.accept_callback = AsyncMock(return_value=CallbackResult(42, True))

    def ensure_checkout_available(self) -> None:
        return None

    def ensure_available(self) -> None:
        pass

    @staticmethod
    def creation_dict(value: CreatedPayment) -> dict[str, object]:
        return PaymentService.creation_dict(value)

    @staticmethod
    def public_dict(row: PaymentOrder) -> dict[str, object]:
        return PaymentService.public_dict(row)


@pytest.mark.asyncio
async def test_creation_disabled_fails_before_identity_or_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(enabled=False)
    client, app = await _client(
        monkeypatch, PaymentService(settings), settings=settings
    )
    async with client:
        response = await client.post(
            "/api/v1/payments/orders",
            json={"productCode": "premium_24h"},
            headers={"Idempotency-Key": "A" * 32},
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "PAYMENTS_DISABLED"
    assert app.state.session_factory.call_count == 0


@pytest.mark.asyncio
async def test_test_checkout_visibility_gate_blocks_new_orders_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(visible=False)
    service = PaymentService(settings)
    client, app = await _client(monkeypatch, service, settings=settings)
    async with client:
        config = await client.get("/api/v1/payments/config")
        create = await client.post(
            "/api/v1/payments/orders",
            json={"productCode": "premium_24h"},
            headers={"Idempotency-Key": "A" * 32},
        )
    assert config.status_code == 200
    assert config.headers["cache-control"] == "no-store"
    assert config.json() == {"testCheckoutAvailable": False}
    assert create.status_code == 503
    assert create.json()["error"]["code"] == "PAYMENTS_DISABLED"
    assert app.state.session_factory.call_count == 0


@pytest.mark.asyncio
async def test_payment_config_requires_test_mode_and_visibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        APP_ENV="test",
        ROBOKASSA_MODE="disabled",
        PREMIUM_TEST_CHECKOUT_VISIBLE=True,
    )
    client, _app = await _client(
        monkeypatch, PaymentService(settings), settings=settings
    )
    async with client:
        response = await client.get("/api/v1/payments/config")
    assert response.status_code == 200
    assert response.json() == {"testCheckoutAvailable": False}


@pytest.mark.asyncio
async def test_hidden_checkout_keeps_existing_order_status_and_callback_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _row(owner=_IdentityService.identity_id)
    service = _StubPaymentService(row)
    service.test_checkout_available = False
    settings = _settings(visible=False)
    client, _app = await _client(monkeypatch, service, settings=settings)
    async with client:
        status = await client.get(f"/api/v1/payments/orders/{row.public_id}")
        callback = await client.post(
            "/api/v1/payments/robokassa/result",
            content="OutSum=1.00&InvId=42&SignatureValue=" + "a" * 64,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    assert status.status_code == 200
    assert status.json()["status"] == "PENDING"
    assert callback.status_code == 200
    assert callback.text == "OK42"


@pytest.mark.asyncio
async def test_create_returns_fixed_test_post_form_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _StubPaymentService(_row(owner=_IdentityService.identity_id))
    client, _app = await _client(monkeypatch, service)
    async with client:
        response = await client.post(
            "/api/v1/payments/orders",
            json={"productCode": "premium_24h"},
            headers={"Idempotency-Key": "A" * 32},
        )
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    assert data["paymentForm"]["action"] == PAYMENT_ACTION
    assert data["paymentForm"]["method"] == "POST"
    assert data["paymentForm"]["fields"]["IsTest"] == "1"
    raw = response.text.lower()
    assert "password" not in raw
    assert str(service.row.id) not in raw


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["amount", "currency", "duration", "isTest"])
async def test_creation_rejects_client_authority_fields(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    service = _StubPaymentService(_row(owner=_IdentityService.identity_id))
    client, _app = await _client(monkeypatch, service)
    async with client:
        response = await client.post(
            "/api/v1/payments/orders",
            json={"productCode": "premium_24h", field: 1},
            headers={"Idempotency-Key": "A" * 32},
        )
    assert response.status_code == 422
    service.create_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_status_is_no_store_and_hides_provider_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _row(owner=_IdentityService.identity_id)
    service = _StubPaymentService(row)
    client, _app = await _client(monkeypatch, service)
    async with client:
        response = await client.get(f"/api/v1/payments/orders/{row.public_id}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "status": "PENDING",
        "productCode": "premium_24h",
        "amountMinor": 100,
        "currency": "RUB",
        "createdAt": "2026-09-01T00:00:00Z",
        "paidAt": None,
    }
    assert "InvId" not in response.text
    assert "receipt" not in response.text.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("public_id", ["not-a-uuid", str(uuid.uuid4()).upper()])
async def test_malformed_order_reference_is_indistinguishable_not_found(
    monkeypatch: pytest.MonkeyPatch, public_id: str
) -> None:
    service = _StubPaymentService(_row(owner=_IdentityService.identity_id))
    client, _app = await _client(monkeypatch, service)
    async with client:
        response = await client.get(f"/api/v1/payments/orders/{public_id}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PAYMENT_ORDER_NOT_FOUND"


@pytest.mark.asyncio
async def test_valid_callback_has_no_cookie_requirement_and_exact_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _StubPaymentService(_row())
    client, _app = await _client(monkeypatch, service)
    async with client:
        response = await client.post(
            "/api/v1/payments/robokassa/result",
            content="OutSum=1.00&InvId=42&SignatureValue=" + "a" * 64,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    assert response.status_code == 200
    assert response.text == "OK42"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        "OutSum=1.00&InvId=42",
        "OutSum=1.00&InvId=42&InvId=43&SignatureValue=x",
        "OutSum=1.00&InvId=0&SignatureValue=x",
        "OutSum=1.00&InvId=42&SignatureValue=x&Shp_order=1",
        "OutSum=1.00&InvId=42&SignatureValue=x&Unexpected=1",
    ],
)
async def test_malformed_callback_is_generic_and_not_acknowledged(
    monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    service = _StubPaymentService(_row())
    client, _app = await _client(monkeypatch, service)
    async with client:
        response = await client.post(
            "/api/v1/payments/robokassa/result",
            content=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    assert response.status_code == 400
    assert response.text == "ERROR"
    service.accept_callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_diagnostic_reports_names_and_parser_stage_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _StubPaymentService(_row())
    captured: list[CallbackDiagnostic] = []
    monkeypatch.setattr(
        payments, "emit_callback_diagnostic_once", captured.append
    )
    client, _app = await _client(monkeypatch, service)
    async with client:
        response = await client.post(
            "/api/v1/payments/robokassa/result",
            content=(
                "OutSum=1.00&InvId=42&SignatureValue="
                + "a" * 64
                + "&IsTest=1"
            ),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    assert response.status_code == 400
    assert len(captured) == 1
    diagnostic = captured[0]
    assert diagnostic.field_names == (
        "InvId",
        "IsTest",
        "OutSum",
        "SignatureValue",
    )
    assert diagnostic.expected_fields_present is True
    assert diagnostic.unexpected_field_names == ("IsTest",)
    assert diagnostic.parser_entered is True
    assert diagnostic.parser_accepted is False
    assert diagnostic.signature_verification_attempted is False
    assert diagnostic.rejection_stage == "field_schema"
    assert diagnostic.rejection_category == "unexpected_field"
    service.accept_callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_commit_failure_never_returns_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _StubPaymentService(_row())
    client, _app = await _client(
        monkeypatch, service, commit_error=RuntimeError("database unavailable")
    )
    async with client:
        response = await client.post(
            "/api/v1/payments/robokassa/result",
            content="OutSum=1.00&InvId=42&SignatureValue=" + "a" * 64,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    assert response.status_code == 500
    assert response.text == "ERROR"


@pytest.mark.asyncio
async def test_callback_body_limit_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _StubPaymentService(_row())
    client, _app = await _client(monkeypatch, service)
    async with client:
        response = await client.post(
            "/api/v1/payments/robokassa/result",
            content="x=" + "A" * 17_000,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    assert response.status_code == 400
    service.accept_callback.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("success", "Check the server-side order status"),
        ("fail", "The order was not changed"),
    ],
)
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_browser_redirects_are_non_mutating(
    monkeypatch: pytest.MonkeyPatch, path: str, message: str, method: str
) -> None:
    service = _StubPaymentService(_row())
    client, _app = await _client(monkeypatch, service)
    async with client:
        response = await client.request(
            method,
            f"/api/v1/payments/robokassa/{path}",
            params={"OutSum": "1.00", "InvId": "42", "SignatureValue": "fake"},
        )
    assert response.status_code == 200
    assert message in response.text
    assert response.headers["cache-control"] == "no-store"
    service.accept_callback.assert_not_awaited()
