"""Explicit, validated resource budget for media activation.

Does not approve production defaults. Aggregate slice ceiling, per-container
limits, and concurrency are distinct fields. A shared ceiling that is lower
than the sum of hard limits is allowed only when explicitly declared.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class ResourceBudgetError(ValueError):
    """Invalid or unapproved resource budget."""


@dataclass(frozen=True)
class ResourceBudget:
    """Validated activation budget (bytes / milliCPU / counts)."""

    approved: bool
    aggregate_memory_max_bytes: int
    aggregate_cpu_quota_milli: int
    aggregate_tasks_max: int
    per_container_memory_bytes: dict[str, int]
    concurrency: int
    shared_ceiling: bool
    notes: str
    memory_swap_max_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "aggregate_memory_max_bytes": self.aggregate_memory_max_bytes,
            "aggregate_cpu_quota_milli": self.aggregate_cpu_quota_milli,
            "aggregate_tasks_max": self.aggregate_tasks_max,
            "per_container_memory_bytes": dict(self.per_container_memory_bytes),
            "concurrency": self.concurrency,
            "shared_ceiling": self.shared_ceiling,
            "notes": self.notes,
            "memory_swap_max_bytes": self.memory_swap_max_bytes,
        }


_REQUIRED_CONTAINERS = frozenset(
    {"media-executor", "media-net-executor", "egress-proxy"}
)


def parse_resource_budget(raw: object) -> ResourceBudget:
    if not isinstance(raw, dict):
        raise ResourceBudgetError("resource_budget must be an object")
    try:
        approved = bool(raw["approved"])
        agg_mem = int(raw["aggregate_memory_max_bytes"])
        agg_cpu = int(raw["aggregate_cpu_quota_milli"])
        agg_tasks = int(raw["aggregate_tasks_max"])
        per_raw = raw["per_container_memory_bytes"]
        concurrency = int(raw["concurrency"])
        shared = bool(raw["shared_ceiling"])
        notes = str(raw.get("notes") or "")
    except (KeyError, TypeError, ValueError) as exc:
        raise ResourceBudgetError(
            f"resource_budget missing/invalid fields: {exc}"
        ) from exc
    if type(raw["approved"]) is not bool:
        raise ResourceBudgetError("approved must be a boolean")
    if type(raw["shared_ceiling"]) is not bool:
        raise ResourceBudgetError("shared_ceiling must be a boolean")
    swap = raw.get("memory_swap_max_bytes", 0)
    if type(swap) is not int or swap != 0:
        raise ResourceBudgetError("memory_swap_max_bytes must be integer zero")
    if not isinstance(per_raw, dict) or not per_raw:
        raise ResourceBudgetError(
            "per_container_memory_bytes must be a non-empty object"
        )
    per: dict[str, int] = {}
    for key, value in per_raw.items():
        if not isinstance(key, str):
            raise ResourceBudgetError("per_container keys must be strings")
        if type(value) is not int or isinstance(value, bool) or value <= 0:
            raise ResourceBudgetError(f"invalid per-container memory for {key}")
        per[key] = value
    missing = _REQUIRED_CONTAINERS - set(per)
    if missing:
        raise ResourceBudgetError(
            f"per_container_memory_bytes missing services: {sorted(missing)}"
        )
    if agg_mem <= 0 or agg_cpu <= 0 or agg_tasks <= 0 or concurrency <= 0:
        raise ResourceBudgetError("aggregate/concurrency values must be positive")
    if concurrency > 2:
        raise ResourceBudgetError(
            "executor concurrency exceeds the supported maximum of 2"
        )
    if set(per) != _REQUIRED_CONTAINERS:
        raise ResourceBudgetError(
            "per-container services must exactly match the three media services"
        )
    for key in (
        "aggregate_memory_max_bytes",
        "aggregate_cpu_quota_milli",
        "aggregate_tasks_max",
        "concurrency",
    ):
        if type(raw[key]) is not int:
            raise ResourceBudgetError(f"{key} must be an exact integer")
    total = sum(per.values())
    if not shared and total > agg_mem:
        raise ResourceBudgetError(
            "sum of per-container memory exceeds aggregate_memory_max_bytes; "
            "set shared_ceiling=true only when an explicit shared ceiling is intended"
        )
    if shared and total <= agg_mem and "shared" not in notes.lower():
        # Shared ceiling with sum <= aggregate is fine; require a note when
        # sum exceeds aggregate so the oversubscription is intentional.
        pass
    if shared and total > agg_mem and not notes.strip():
        raise ResourceBudgetError(
            "shared_ceiling oversubscription requires a non-empty notes rationale"
        )
    if not approved:
        raise ResourceBudgetError(
            "resource_budget.approved must be true for media activation "
            "(production defaults are not auto-approved)"
        )
    return ResourceBudget(
        approved=approved,
        aggregate_memory_max_bytes=agg_mem,
        aggregate_cpu_quota_milli=agg_cpu,
        aggregate_tasks_max=agg_tasks,
        per_container_memory_bytes=per,
        concurrency=concurrency,
        shared_ceiling=shared,
        notes=notes,
    )


def disposable_test_budget(
    *,
    aggregate_memory_max_bytes: int = 96 * 1024 * 1024,
    media_executor_bytes: int = 64 * 1024 * 1024,
    media_net_bytes: int = 64 * 1024 * 1024,
    egress_proxy_bytes: int = 32 * 1024 * 1024,
) -> ResourceBudget:
    """Explicit disposable-test budget (not a production approval)."""
    return parse_resource_budget(
        {
            "approved": True,
            "aggregate_memory_max_bytes": aggregate_memory_max_bytes,
            "aggregate_cpu_quota_milli": 500,
            "aggregate_tasks_max": 256,
            "per_container_memory_bytes": {
                "media-executor": media_executor_bytes,
                "media-net-executor": media_net_bytes,
                "egress-proxy": egress_proxy_bytes,
            },
            "concurrency": 1,
            "shared_ceiling": True,
            "notes": "disposable test shared ceiling; not production budget",
        }
    )
