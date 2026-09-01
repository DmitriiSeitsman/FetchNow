"""Static migration and model constraints for PRD2-A2."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from fetchnow.payments.models import PaymentOrder  # noqa: F401
from fetchnow.premium.models import PremiumEntitlement


def test_premium_migration_is_exactly_after_0008_and_reversible() -> None:
    migration = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0009_premium_entitlements.py"
    ).read_text(encoding="utf-8")
    assert 'revision: str = "0009_premium_entitlements"' in migration
    assert 'down_revision: str | None = "0008_payment_orders"' in migration
    assert 'op.drop_table("premium_entitlements")' in migration


def test_premium_model_emits_required_database_constraints() -> None:
    sql = str(
        CreateTable(PremiumEntitlement.__table__).compile(dialect=postgresql.dialect())
    )
    assert "CHECK (expires_at > starts_at)" in sql
    assert "UNIQUE (source_payment_order_id)" in sql
    assert "UNIQUE (public_id)" in sql
    assert "FOREIGN KEY(anonymous_client_id) REFERENCES anonymous_clients" in sql
    assert "FOREIGN KEY(source_payment_order_id) REFERENCES payment_orders" in sql
