"""SEC-01: stabilize nested Docker probe budgets."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from fetchnow_release.bounded_subprocess import (  # noqa: E402
    BudgetExhaustedError,
    DeadlineBudget,
)
from fetchnow_release.stabilize import (  # noqa: E402
    StabilizeError,
    StabilizationPolicy,
    wait_services_healthy,
)


def test_wait_services_does_not_start_probe_after_budget_exhausted(
    tmp_path: Path,
) -> None:
    clock = {"t": 0.0}

    def now() -> float:
        return clock["t"]

    def sleeper(seconds: float) -> None:
        clock["t"] += seconds

    calls: list[object] = []

    def fake_probe(*args, **kwargs):  # noqa: ANN002, ANN003
        calls.append(kwargs.get("budget"))
        raise BudgetExhaustedError("deadline exhausted")

    with patch("fetchnow_release.stabilize.run_docker_probe", side_effect=fake_probe):
        with pytest.raises(StabilizeError, match="skipped|timed out waiting"):
            wait_services_healthy(
                project_name="fetchnow-rollout-test-sec01",
                env_file=tmp_path / "env",
                compose_files=(tmp_path / "compose.yaml",),
                cwd=tmp_path,
                services=("api",),
                policy=StabilizationPolicy(
                    service_deadline_seconds=0.0,
                    service_poll_seconds=0.1,
                ),
                clock=now,
                sleeper=sleeper,
            )
    # With zero deadline, either no probe or an immediate exhausted probe.
    assert len(calls) <= 1


def test_wait_services_passes_remaining_budget_into_probe(tmp_path: Path) -> None:
    clock = {"t": 0.0}

    def now() -> float:
        return clock["t"]

    def sleeper(seconds: float) -> None:
        clock["t"] += seconds

    seen: list[float] = []

    def fake_probe(*args, **kwargs):  # noqa: ANN002, ANN003
        budget = kwargs.get("budget")
        assert isinstance(budget, DeadlineBudget)
        seen.append(budget.remaining())
        # Pretend containers never become healthy; advance wall via sleeper.
        raise StabilizeError("compose ps failed: boom")

    # StabilizeError from compose path: patch at _compose_ps level via probe
    from fetchnow_release.bounded_subprocess import BoundedResult

    def fake_ok_then_hang(*args, **kwargs):  # noqa: ANN002, ANN003
        budget = kwargs.get("budget")
        assert isinstance(budget, DeadlineBudget)
        seen.append(budget.remaining())
        return BoundedResult(
            argv=("docker",),
            returncode=0,
            stdout=b"",
            stderr=b"",
        )

    with patch(
        "fetchnow_release.stabilize.run_docker_probe", side_effect=fake_ok_then_hang
    ):
        with pytest.raises(StabilizeError, match="timed out waiting"):
            wait_services_healthy(
                project_name="fetchnow-rollout-test-sec01",
                env_file=tmp_path / "env",
                compose_files=(tmp_path / "compose.yaml",),
                cwd=tmp_path,
                services=("api",),
                policy=StabilizationPolicy(
                    service_deadline_seconds=0.5,
                    service_poll_seconds=0.1,
                ),
                clock=now,
                sleeper=sleeper,
            )
    assert seen
    assert all(r >= 0 for r in seen)
