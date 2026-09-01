"""Static migration and model constraints for PRD2-A1."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from fetchnow.payments.models import PaymentOrder
from fetchnow.quota.models import AnonymousClient  # noqa: F401


def test_payment_migration_is_exactly_after_0007_and_reversible() -> None:
    migration = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0008_payment_orders.py"
    ).read_text(encoding="utf-8")
    assert 'revision: str = "0008_payment_orders"' in migration
    assert 'down_revision: str | None = "0007_free_download_quota"' in migration
    assert 'op.drop_table("payment_orders")' in migration
    assert 'op.execute("DROP SEQUENCE payment_provider_invoice_id_seq")' in migration


def test_payment_model_emits_required_database_constraints() -> None:
    sql = str(CreateTable(PaymentOrder.__table__).compile(dialect=postgresql.dialect()))
    assert "CHECK (amount_minor > 0)" in sql
    assert "CHECK (provider_invoice_id > 0)" in sql
    assert "CHECK (is_test)" in sql
    assert "UNIQUE (provider_invoice_id)" in sql
    assert "UNIQUE (anonymous_client_id, creation_idempotency_hash)" in sql
    assert "FOREIGN KEY(anonymous_client_id) REFERENCES anonymous_clients" in sql
