"""Free-space preflight (unit-testable decision logic)."""

from __future__ import annotations

from dataclasses import dataclass

from . import FREE_SPACE_MARGIN_BYTES, FREE_SPACE_SIZE_MULTIPLIER


@dataclass(frozen=True)
class FreeSpaceDecision:
    ok: bool
    database_bytes: int
    free_bytes: int
    required_bytes: int
    reason: str


def required_free_bytes(
    database_bytes: int,
    *,
    multiplier: int = FREE_SPACE_SIZE_MULTIPLIER,
    margin_bytes: int = FREE_SPACE_MARGIN_BYTES,
) -> int:
    if database_bytes < 0:
        raise ValueError("database_bytes must be >= 0")
    return database_bytes * multiplier + margin_bytes


def evaluate_free_space(
    *,
    database_bytes: int,
    free_bytes: int,
    multiplier: int = FREE_SPACE_SIZE_MULTIPLIER,
    margin_bytes: int = FREE_SPACE_MARGIN_BYTES,
) -> FreeSpaceDecision:
    required = required_free_bytes(
        database_bytes, multiplier=multiplier, margin_bytes=margin_bytes
    )
    if free_bytes < required:
        return FreeSpaceDecision(
            ok=False,
            database_bytes=database_bytes,
            free_bytes=free_bytes,
            required_bytes=required,
            reason=(
                f"insufficient free space: have {free_bytes} bytes, "
                f"need at least {required} bytes "
                f"(~{multiplier}x DB size + {margin_bytes} margin; "
                "compressed dump size cannot be predicted exactly)"
            ),
        )
    return FreeSpaceDecision(
        ok=True,
        database_bytes=database_bytes,
        free_bytes=free_bytes,
        required_bytes=required,
        reason="ok",
    )
