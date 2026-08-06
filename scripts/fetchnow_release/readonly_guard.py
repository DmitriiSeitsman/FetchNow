"""Subprocess guard for read-only release tooling."""

from __future__ import annotations

FORBIDDEN_SUBPROCESS_MARKERS = (
    "pg_dump",
    "pg_restore",
    "alembic upgrade",
    "alembic downgrade",
    "alembic stamp",
    " compose up",
    " compose down",
    " compose restart",
    " compose rm",
    " compose run",
    "docker rm",
    "docker build",
    "docker pull",
    " docker tag",
    "insert into",
    "update ",
    "delete from",
    "alter table",
    "create table",
    "drop table",
)


class ReadonlySubprocessError(RuntimeError):
    """Forbidden mutation subprocess."""


def assert_readonly_subprocess(argv: list[str]) -> None:
    joined = " ".join(str(part) for part in argv).lower()
    for marker in FORBIDDEN_SUBPROCESS_MARKERS:
        if marker in joined:
            raise ReadonlySubprocessError(
                f"forbidden subprocess in read-only path: {marker.strip()!r}"
            )
