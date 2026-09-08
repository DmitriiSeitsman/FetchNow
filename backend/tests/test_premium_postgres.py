"""PostgreSQL entitlement issuance, concurrency, and reconciliation for PRD2-A2."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fetchnow.api.main import create_app
from fetchnow.core.config import Settings
from fetchnow.db.session import create_engine, create_session_factory
from fetchnow.payments.catalog import get_product
from fetchnow.payments.repository import PaymentOrderRepository
from fetchnow.payments.robokassa import serialize_receipt, sign_callback
from fetchnow.payments.service import PaymentService
from fetchnow.premium.models import PremiumEntitlement
from fetchnow.premium.reconcile import reconcile_paid_orders_without_entitlement
from fetchnow.premium.repository import PremiumEntitlementRepository
from fetchnow.premium.service import PremiumEntitlementService
from fetchnow.quota.repository import QuotaRepository
from fetchnow.quota.tokens import generate_anonymous_token, hash_anonymous_token

_TEST_URL = os.environ.get("FETCHNOW_TEST_DATABASE_URL", "").strip()
_BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _require_db() -> str:
    if not _TEST_URL:
        pytest.skip("FETCHNOW_TEST_DATABASE_URL not set")
    return _TEST_URL


def _run_alembic(*args: str, url: str) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = url
    env["APP_ENV"] = "test"
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_BACKEND_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stdout + proc.stderr)


@pytest.fixture(scope="module")
def database_url() -> str:
    return _require_db()


@pytest.fixture(scope="module")
def migrated_database(database_url: str) -> str:
    _run_alembic("upgrade", "head", url=database_url)
    return database_url


@pytest.fixture
async def engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(Settings(APP_ENV="test", DATABASE_URL=migrated_database))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def sessions(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


def _settings(url: str, *, robokassa_mode: str = "test") -> Settings:
    if robokassa_mode == "disabled":
        return Settings(APP_ENV="test", DATABASE_URL=url, ROBOKASSA_MODE="disabled")
    return Settings(
        APP_ENV="test",
        DATABASE_URL=url,
        ROBOKASSA_MODE="test",
        ROBOKASSA_MERCHANT_LOGIN="demo",
        ROBOKASSA_SIGNATURE_ALGORITHM="sha256",
        ROBOKASSA_TEST_PASSWORD1="password-one",
        ROBOKASSA_TEST_PASSWORD2="password-two",
        ROBOKASSA_TEST_AMOUNT_MINOR=1_000,
        ROBOKASSA_RECEIPT_TAX="none",
        ROBOKASSA_RECEIPT_PAYMENT_METHOD="full_payment",
        PREMIUM_TEST_CHECKOUT_VISIBLE=True,
    )


async def _cleanup(session: AsyncSession) -> None:
    await session.execute(text("DELETE FROM premium_entitlements"))
    await session.execute(text("DELETE FROM payment_orders"))
    await session.execute(text("DELETE FROM free_download_quota_entries"))
    await session.execute(text("DELETE FROM anonymous_clients"))
    await session.commit()


async def _identity(session: AsyncSession) -> uuid.UUID:
    repo = QuotaRepository(session)
    now = await repo.database_now()
    token_hash = hash_anonymous_token(generate_anonymous_token())
    row = await repo.create_client(token_hash=token_hash, now=now, ttl_seconds=86_400)
    return row.id


def _result_form(*, raw_out_sum: str, inv_id: int, signature: str) -> str:
    """Synthetic ResultURL with the field names observed in production."""
    return urlencode(
        [
            ("OutSum", raw_out_sum),
            ("InvId", str(inv_id)),
            ("SignatureValue", signature),
            ("EMail", "buyer@example.invalid"),
            ("Fee", "0.10"),
            ("IncCurrLabel", "BankCard"),
            ("IncSum", raw_out_sum),
            ("IsTest", "1"),
            ("PaymentMethod", "BankCard"),
            ("crc", "synthetic"),
            ("inv_id", "synthetic"),
            ("out_summ", "synthetic"),
        ]
    )


async def _post_result(
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    *,
    raw_out_sum: str,
    inv_id: int,
    signature: str,
) -> Response:
    app = create_app(settings)
    app.state.settings = settings
    app.state.payment_service = PaymentService(settings)
    app.state.session_factory = sessions
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        return await client.post(
            "/api/v1/payments/robokassa/result",
            content=_result_form(
                raw_out_sum=raw_out_sum,
                inv_id=inv_id,
                signature=signature,
            ),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )


async def _seed_pending_order(
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    *,
    idempotency_key: str,
) -> int:
    async with sessions() as seed:
        await _cleanup(seed)
        identity_id = await _identity(seed)
        created = await PaymentService(settings).create_order(
            anonymous_client_id=identity_id,
            product_code="premium_24h",
            idempotency_key=idempotency_key,
            session=seed,
        )
        inv_id = created.order.provider_invoice_id
        await seed.commit()
        return inv_id


@pytest.mark.asyncio
async def test_resulturl_with_provider_metadata_is_paid_and_idempotent(
    sessions: async_sessionmaker[AsyncSession], migrated_database: str
) -> None:
    settings = _settings(migrated_database)
    inv_id = await _seed_pending_order(
        settings, sessions, idempotency_key="R" * 32
    )
    signature = sign_callback(
        raw_out_sum="10.000000", inv_id=inv_id, password2="password-two"
    )

    first = await _post_result(
        settings,
        sessions,
        raw_out_sum="10.000000",
        inv_id=inv_id,
        signature=signature,
    )
    duplicate = await _post_result(
        settings,
        sessions,
        raw_out_sum="10.000000",
        inv_id=inv_id,
        signature=signature,
    )

    assert first.status_code == 200
    assert first.text == f"OK{inv_id}"
    assert duplicate.status_code == 200
    assert duplicate.text == f"OK{inv_id}"
    async with sessions() as session:
        order = await PaymentOrderRepository(session).get_by_invoice_id(inv_id)
        assert order is not None
        assert order.status == "paid"
        assert len((await session.scalars(select(PremiumEntitlement))).all()) == 1
        await _cleanup(session)


@pytest.mark.asyncio
async def test_concurrent_resulturls_with_provider_metadata_create_one_entitlement(
    sessions: async_sessionmaker[AsyncSession], migrated_database: str
) -> None:
    settings = _settings(migrated_database)
    inv_id = await _seed_pending_order(
        settings, sessions, idempotency_key="S" * 32
    )
    signature = sign_callback(
        raw_out_sum="10.000000", inv_id=inv_id, password2="password-two"
    )

    responses = await asyncio.gather(
        _post_result(
            settings,
            sessions,
            raw_out_sum="10.000000",
            inv_id=inv_id,
            signature=signature,
        ),
        _post_result(
            settings,
            sessions,
            raw_out_sum="10.000000",
            inv_id=inv_id,
            signature=signature,
        ),
    )

    assert [response.status_code for response in responses] == [200, 200]
    assert [response.text for response in responses] == [f"OK{inv_id}"] * 2
    async with sessions() as session:
        assert len((await session.scalars(select(PremiumEntitlement))).all()) == 1
        await _cleanup(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["invalid_signature", "amount_mismatch"])
async def test_invalid_resulturl_with_provider_metadata_does_not_mutate(
    sessions: async_sessionmaker[AsyncSession],
    migrated_database: str,
    failure: str,
) -> None:
    settings = _settings(migrated_database)
    inv_id = await _seed_pending_order(
        settings, sessions, idempotency_key="T" * 32
    )
    raw_out_sum = "11.000000" if failure == "amount_mismatch" else "10.000000"
    signature = (
        sign_callback(
            raw_out_sum=raw_out_sum,
            inv_id=inv_id,
            password2="password-two",
        )
        if failure == "amount_mismatch"
        else "0" * 64
    )

    response = await _post_result(
        settings,
        sessions,
        raw_out_sum=raw_out_sum,
        inv_id=inv_id,
        signature=signature,
    )

    assert response.status_code == 400
    assert response.text == "ERROR"
    async with sessions() as session:
        order = await PaymentOrderRepository(session).get_by_invoice_id(inv_id)
        assert order is not None
        assert order.status == "pending"
        assert (await session.scalars(select(PremiumEntitlement))).all() == []
        await _cleanup(session)


@pytest.mark.asyncio
async def test_first_valid_resulturl_creates_exactly_one_entitlement(
    sessions: async_sessionmaker[AsyncSession], migrated_database: str
) -> None:
    settings = _settings(migrated_database)
    async with sessions() as seed:
        await _cleanup(seed)
        identity_id = await _identity(seed)
        created = await PaymentService(settings).create_order(
            anonymous_client_id=identity_id,
            product_code="premium_24h",
            idempotency_key="A" * 32,
            session=seed,
        )
        inv_id = created.order.provider_invoice_id
        await seed.commit()
    signature = sign_callback(
        raw_out_sum="10.000000", inv_id=inv_id, password2="password-two"
    )
    async with sessions() as session:
        result = await PaymentService(settings).accept_callback(
            raw_out_sum="10.000000",
            inv_id=inv_id,
            supplied_signature=signature,
            session=session,
        )
        await session.commit()
        assert result.transitioned is True
    async with sessions() as session:
        entitlements = (
            await session.scalars(select(PremiumEntitlement))
        ).all()
        assert len(entitlements) == 1
        row = entitlements[0]
        order = await PaymentOrderRepository(session).get_by_invoice_id(inv_id)
        assert order is not None
        assert row.source_payment_order_id == order.id
        assert row.anonymous_client_id == identity_id
        assert row.starts_at == order.paid_at
        assert row.expires_at == order.paid_at + timedelta(seconds=86_400)
        assert row.product_code == "premium_24h"
        await _cleanup(session)


@pytest.mark.asyncio
async def test_concurrent_valid_callbacks_create_one_entitlement(
    sessions: async_sessionmaker[AsyncSession], migrated_database: str
) -> None:
    settings = _settings(migrated_database)
    async with sessions() as seed:
        await _cleanup(seed)
        identity_id = await _identity(seed)
        created = await PaymentService(settings).create_order(
            anonymous_client_id=identity_id,
            product_code="premium_24h",
            idempotency_key="B" * 32,
            session=seed,
        )
        inv_id = created.order.provider_invoice_id
        await seed.commit()
    signature = sign_callback(
        raw_out_sum="10.000000", inv_id=inv_id, password2="password-two"
    )

    async def invoke() -> bool:
        async with sessions() as session:
            result = await PaymentService(settings).accept_callback(
                raw_out_sum="10.000000",
                inv_id=inv_id,
                supplied_signature=signature,
                session=session,
            )
            await session.commit()
            return result.transitioned

    results = await asyncio.gather(invoke(), invoke())
    assert sorted(results) == [False, True]
    async with sessions() as session:
        assert len((await session.scalars(select(PremiumEntitlement))).all()) == 1
        await _cleanup(session)


@pytest.mark.asyncio
async def test_duplicate_callback_after_paid_reuses_entitlement(
    sessions: async_sessionmaker[AsyncSession], migrated_database: str
) -> None:
    settings = _settings(migrated_database)
    async with sessions() as seed:
        await _cleanup(seed)
        identity_id = await _identity(seed)
        created = await PaymentService(settings).create_order(
            anonymous_client_id=identity_id,
            product_code="premium_24h",
            idempotency_key="C" * 32,
            session=seed,
        )
        inv_id = created.order.provider_invoice_id
        await seed.commit()
    signature = sign_callback(
        raw_out_sum="10.000000", inv_id=inv_id, password2="password-two"
    )
    for _ in range(2):
        async with sessions() as session:
            result = await PaymentService(settings).accept_callback(
                raw_out_sum="10.000000",
                inv_id=inv_id,
                supplied_signature=signature,
                session=session,
            )
            await session.commit()
            assert result.inv_id == inv_id
    async with sessions() as session:
        rows = (await session.scalars(select(PremiumEntitlement))).all()
        assert len(rows) == 1
        await _cleanup(session)


@pytest.mark.asyncio
async def test_reconciliation_is_idempotent(
    sessions: async_sessionmaker[AsyncSession], migrated_database: str
) -> None:
    settings = _settings(migrated_database)
    product = get_product(settings, "premium_24h")
    receipt = serialize_receipt(product).json_text
    async with sessions() as session:
        await _cleanup(session)
        identity_id = await _identity(session)
        repo = PaymentOrderRepository(session)
        now = await repo.database_now()
        order, _ = await repo.create_or_get(
            anonymous_client_id=identity_id,
            idempotency_hash=b"d" * 32,
            product=product,
            receipt_json=receipt,
            now=now,
            ttl_seconds=3_600,
        )
        await repo.mark_pending(order, now=now)
        paid_at = now
        await repo.mark_paid(order, now=paid_at)
        await session.commit()
    async with sessions() as session:
        first = await reconcile_paid_orders_without_entitlement(session)
        await session.commit()
        second = await reconcile_paid_orders_without_entitlement(session)
        await session.commit()
        assert first.created == 1
        assert first.scanned == 1
        assert second.created == 0
        assert second.scanned == 0
        rows = (await session.scalars(select(PremiumEntitlement))).all()
        assert len(rows) == 1
        assert rows[0].starts_at == paid_at
        await _cleanup(session)


@pytest.mark.asyncio
async def test_unique_source_payment_order_id_enforced(
    sessions: async_sessionmaker[AsyncSession], migrated_database: str
) -> None:
    async with sessions() as session:
        await _cleanup(session)
        identity_id = await _identity(session)
        product = get_product(_settings(migrated_database), "premium_24h")
        receipt = serialize_receipt(product).json_text
        repo = PaymentOrderRepository(session)
        now = await repo.database_now()
        order, _ = await repo.create_or_get(
            anonymous_client_id=identity_id,
            idempotency_hash=b"e" * 32,
            product=product,
            receipt_json=receipt,
            now=now,
            ttl_seconds=3_600,
        )
        await repo.mark_pending(order, now=now)
        await repo.mark_paid(order, now=now)
        premium_repo = PremiumEntitlementRepository(session)
        await premium_repo.ensure_for_paid_order(order, now=now)
        with pytest.raises(IntegrityError):
            duplicate = PremiumEntitlement(
                id=uuid.uuid4(),
                public_id=uuid.uuid4(),
                anonymous_client_id=identity_id,
                source_payment_order_id=order.id,
                product_code="premium_24h",
                starts_at=now,
                expires_at=now + timedelta(hours=24),
                created_at=now,
            )
            session.add(duplicate)
            await session.flush()
        await session.rollback()
        await _cleanup(session)


@pytest.mark.asyncio
async def test_active_resolution_uses_server_time(
    sessions: async_sessionmaker[AsyncSession], migrated_database: str
) -> None:
    settings = _settings(migrated_database)
    async with sessions() as session:
        await _cleanup(session)
        identity_id = await _identity(session)
        product = get_product(settings, "premium_24h")
        receipt = serialize_receipt(product).json_text
        repo = PaymentOrderRepository(session)
        now = await repo.database_now()
        order, _ = await repo.create_or_get(
            anonymous_client_id=identity_id,
            idempotency_hash=b"f" * 32,
            product=product,
            receipt_json=receipt,
            now=now,
            ttl_seconds=3_600,
        )
        await repo.mark_pending(order, now=now)
        paid_at = now
        await repo.mark_paid(order, now=paid_at)
        await PremiumEntitlementRepository(session).ensure_for_paid_order(
            order, now=paid_at
        )
        service = PremiumEntitlementService()
        active = await service.status_for_client(
            anonymous_client_id=identity_id, session=session
        )
        assert active.capability.is_premium is True
        entitlement = (await session.scalars(select(PremiumEntitlement))).one()
        before = await PremiumEntitlementRepository(session).get_active_for_client(
            anonymous_client_id=identity_id, now=paid_at + timedelta(hours=1)
        )
        after = await PremiumEntitlementRepository(session).get_active_for_client(
            anonymous_client_id=identity_id,
            now=entitlement.expires_at,
        )
        assert before is not None
        assert after is None
        await _cleanup(session)


@pytest.mark.asyncio
async def test_another_client_cannot_see_entitlement(
    sessions: async_sessionmaker[AsyncSession], migrated_database: str
) -> None:
    settings = _settings(migrated_database)
    async with sessions() as session:
        await _cleanup(session)
        owner = await _identity(session)
        other = await _identity(session)
        product = get_product(settings, "premium_24h")
        receipt = serialize_receipt(product).json_text
        repo = PaymentOrderRepository(session)
        now = await repo.database_now()
        order, _ = await repo.create_or_get(
            anonymous_client_id=owner,
            idempotency_hash=b"g" * 32,
            product=product,
            receipt_json=receipt,
            now=now,
            ttl_seconds=3_600,
        )
        await repo.mark_pending(order, now=now)
        await repo.mark_paid(order, now=now)
        await PremiumEntitlementRepository(session).ensure_for_paid_order(
            order, now=now
        )
        service = PremiumEntitlementService()
        owner_status = await service.status_for_client(
            anonymous_client_id=owner, session=session
        )
        other_status = await service.status_for_client(
            anonymous_client_id=other, session=session
        )
        assert owner_status.capability.is_premium is True
        assert other_status.capability.is_premium is False
        await _cleanup(session)


@pytest.mark.asyncio
async def test_reconciliation_does_not_depend_on_robokassa_mode(
    sessions: async_sessionmaker[AsyncSession], migrated_database: str
) -> None:
    settings = _settings(migrated_database, robokassa_mode="disabled")
    product = get_product(
        _settings(migrated_database, robokassa_mode="test"), "premium_24h"
    )
    receipt = serialize_receipt(product).json_text
    async with sessions() as session:
        await _cleanup(session)
        identity_id = await _identity(session)
        repo = PaymentOrderRepository(session)
        now = await repo.database_now()
        order, _ = await repo.create_or_get(
            anonymous_client_id=identity_id,
            idempotency_hash=b"h" * 32,
            product=product,
            receipt_json=receipt,
            now=now,
            ttl_seconds=3_600,
        )
        await repo.mark_pending(order, now=now)
        paid_at = now
        await repo.mark_paid(order, now=paid_at)
        await session.commit()
    async with sessions() as session:
        result = await reconcile_paid_orders_without_entitlement(session)
        await session.commit()
        assert result.created == 1
        row = (await session.scalars(select(PremiumEntitlement))).one()
        assert row.starts_at == paid_at
        assert row.expires_at == paid_at + timedelta(seconds=86_400)
        status = await PremiumEntitlementService().status_for_client(
            anonymous_client_id=identity_id, session=session
        )
        assert status.capability.is_premium is True
        assert settings.robokassa_mode == "disabled"
        await _cleanup(session)


@pytest.mark.asyncio
async def test_historical_reconciliation_uses_paid_at_not_reconcile_time(
    sessions: async_sessionmaker[AsyncSession], migrated_database: str
) -> None:
    """Simulates production InvId=1: PAID long ago reconciles as already expired."""
    settings = _settings(migrated_database, robokassa_mode="disabled")
    product = get_product(
        _settings(migrated_database, robokassa_mode="test"), "premium_24h"
    )
    receipt = serialize_receipt(product).json_text
    async with sessions() as session:
        await _cleanup(session)
        identity_id = await _identity(session)
        repo = PaymentOrderRepository(session)
        reconcile_now = await repo.database_now()
        paid_at = reconcile_now - timedelta(days=2)
        order, _ = await repo.create_or_get(
            anonymous_client_id=identity_id,
            idempotency_hash=b"i" * 32,
            product=product,
            receipt_json=receipt,
            now=paid_at - timedelta(hours=1),
            ttl_seconds=3_600,
        )
        await repo.mark_pending(order, now=paid_at - timedelta(minutes=30))
        await repo.mark_paid(order, now=paid_at)
        await session.commit()
    async with sessions() as session:
        result = await reconcile_paid_orders_without_entitlement(session)
        await session.commit()
        assert result.created == 1
        row = (await session.scalars(select(PremiumEntitlement))).one()
        assert row.starts_at == paid_at
        assert row.expires_at == paid_at + timedelta(seconds=86_400)
        assert row.expires_at < reconcile_now
        status = await PremiumEntitlementService().status_for_client(
            anonymous_client_id=identity_id, session=session
        )
        assert status.capability.is_premium is False
        assert settings.robokassa_mode == "disabled"
        await _cleanup(session)
