"""PostgreSQL idempotency and callback locking for PRD2-A1.

Skipped unless FETCHNOW_TEST_DATABASE_URL is configured.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fetchnow.core.config import Settings
from fetchnow.db.session import create_engine, create_session_factory
from fetchnow.payments.catalog import get_product
from fetchnow.payments.errors import IdempotencyConflictError
from fetchnow.payments.repository import PaymentOrderRepository
from fetchnow.payments.robokassa import serialize_receipt, sign_callback
from fetchnow.payments.service import PaymentService
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


def _settings(url: str) -> Settings:
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
    assert token_hash is not None
    row = await repo.create_client(token_hash=token_hash, now=now, ttl_seconds=86_400)
    return row.id


@pytest.mark.asyncio
async def test_database_allocates_unique_positive_inv_id_and_idempotent_order(
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
        first, created = await repo.create_or_get(
            anonymous_client_id=identity_id,
            idempotency_hash=b"a" * 32,
            product=product,
            receipt_json=receipt,
            now=now,
            ttl_seconds=3_600,
        )
        await repo.mark_pending(first, now=now)
        second, created_again = await repo.create_or_get(
            anonymous_client_id=identity_id,
            idempotency_hash=b"a" * 32,
            product=product,
            receipt_json=receipt,
            now=now,
            ttl_seconds=3_600,
        )
        other, other_created = await repo.create_or_get(
            anonymous_client_id=identity_id,
            idempotency_hash=b"b" * 32,
            product=product,
            receipt_json=receipt,
            now=now,
            ttl_seconds=3_600,
        )
        assert created is True
        assert created_again is False
        assert second.id == first.id
        assert other_created is True
        assert first.provider_invoice_id > 0
        assert other.provider_invoice_id > 0
        assert other.provider_invoice_id != first.provider_invoice_id
        with pytest.raises(IdempotencyConflictError):
            await repo.create_or_get(
                anonymous_client_id=identity_id,
                idempotency_hash=b"a" * 32,
                product=replace(product, amount_minor=2_000),
                receipt_json=receipt,
                now=now,
                ttl_seconds=3_600,
            )
        await session.rollback()
    async with sessions() as session:
        await _cleanup(session)


@pytest.mark.asyncio
async def test_concurrent_valid_callbacks_make_one_paid_transition(
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
        await _cleanup(session)


@pytest.mark.asyncio
async def test_expiry_never_changes_paid_truth(
    sessions: async_sessionmaker[AsyncSession], migrated_database: str
) -> None:
    settings = _settings(migrated_database)
    async with sessions() as session:
        await _cleanup(session)
        identity_id = await _identity(session)
        created = await PaymentService(settings).create_order(
            anonymous_client_id=identity_id,
            product_code="premium_24h",
            idempotency_key="B" * 32,
            session=session,
        )
        repo = PaymentOrderRepository(session)
        now = await repo.database_now()
        await repo.mark_paid(created.order, now=now)
        created.order.expires_at = now
        await session.commit()
        assert await repo.expire_unpaid(now=now) == 0
        assert created.order.status == "paid"
        await session.commit()
        await _cleanup(session)
