"""SQLAlchemy declarative base for FetchNow models."""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared metadata root for Alembic and ORM models."""

    pass
