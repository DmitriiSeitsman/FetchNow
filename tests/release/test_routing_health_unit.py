"""Unit tests for gateway routing and public HTTPS gates."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fetchnow_release.health import HealthInput, HealthResult  # noqa: E402
from fetchnow_release.http_health import HttpCheckResult  # noqa: E402
from fetchnow_release.routing_health import (  # noqa: E402
    HOMEPAGE_MARKER,
    PublicHttpsGateConfig,
    RoutingHealthError,
    check_public_https_gate,
    check_public_https_path,
    is_transient_routing_failure,
    public_https_config_for_real_project,
    validate_public_https_origin,
    wait_gateway_routing_ready,
)
from fetchnow_release.stabilize import (  # noqa: E402
    StabilizeError,
    StabilizationPolicy,
    resolve_public_https_config,
    stabilize_full_health,
)


def test_validate_origin_accepts_approved() -> None:
    assert (
        validate_public_https_origin("https://fetchnow.online")
        == "https://fetchnow.online"
    )
    assert (
        validate_public_https_origin("https://staging.fetchnow.online/")
        == "https://staging.fetchnow.online"
    )


@pytest.mark.parametrize(
    "origin",
    [
        "http://fetchnow.online",
        "https://evil.example",
        "https://user:pass@fetchnow.online",
        "https://fetchnow.online/path",
        "https://fetchnow.online?x=1",
        "https://fetchnow.online#frag",
        "https://fetchnow.online:8443",
        "https://127.0.0.1",
        "https://[::1]",
        "ftp://fetchnow.online",
        "",
        "https://fetchnow.online:443/",
        "https://fetchnow.online/admin",
    ],
)
def test_validate_origin_rejects_unsafe(origin: str) -> None:
    with pytest.raises(RoutingHealthError):
        validate_public_https_origin(origin)


def test_validate_test_origin_allowlist() -> None:
    assert (
        validate_public_https_origin(
            "https://gateway.routing.test", allow_test_origin=True
        )
        == "https://gateway.routing.test"
    )
    assert (
        validate_public_https_origin(
            "https://gateway.routing.test:18443", allow_test_origin=True
        )
        == "https://gateway.routing.test:18443"
    )
    with pytest.raises(RoutingHealthError):
        validate_public_https_origin("https://evil.example", allow_test_origin=True)
    with pytest.raises(RoutingHealthError):
        validate_public_https_origin(
            "https://gateway.routing.test:443", allow_test_origin=True
        )


def test_real_project_origin() -> None:
    cfg = public_https_config_for_real_project("fetchnow-production")
    assert cfg.origin == "https://fetchnow.online"
    assert cfg.ssl_context is None
    assert cfg.allow_test_origin is False


def _advancing_clock(start: float = 0.0, step: float = 0.02):
    state = {"t": start}

    def clock() -> float:
        return state["t"]

    def sleeper(_seconds: float) -> None:
        state["t"] += step

    return clock, sleeper


def test_public_https_rejects_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a, **_k):  # noqa: ANN001
        raise RoutingHealthError("unexpected redirect HTTP 301")

    monkeypatch.setattr(
        "fetchnow_release.routing_health._fetch_public",
        boom,
    )
    clock, sleeper = _advancing_clock()
    result = check_public_https_path(
        PublicHttpsGateConfig(origin="https://fetchnow.online"),
        "/api/v1/health/live",
        expect_json=True,
        deadline_seconds=0.05,
        retry_delay=0.0,
        clock=clock,
        sleeper=sleeper,
    )
    assert not result.ok
    assert "redirect" in result.detail


def test_public_https_tls_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a, **_k):  # noqa: ANN001
        raise RoutingHealthError("TLS verification failed: certificate verify failed")

    monkeypatch.setattr(
        "fetchnow_release.routing_health._fetch_public",
        boom,
    )
    clock, sleeper = _advancing_clock()
    result = check_public_https_path(
        PublicHttpsGateConfig(origin="https://fetchnow.online"),
        "/api/v1/health/live",
        expect_json=True,
        deadline_seconds=0.05,
        retry_delay=0.0,
        clock=clock,
        sleeper=sleeper,
    )
    assert not result.ok
    assert "TLS" in result.detail


def test_public_https_oversized_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a, **_k):  # noqa: ANN001
        raise RoutingHealthError("response body too large")

    monkeypatch.setattr(
        "fetchnow_release.routing_health._fetch_public",
        boom,
    )
    clock, sleeper = _advancing_clock()
    result = check_public_https_path(
        PublicHttpsGateConfig(origin="https://fetchnow.online"),
        "/",
        expect_json=False,
        require_homepage_marker=True,
        deadline_seconds=0.05,
        retry_delay=0.0,
        clock=clock,
        sleeper=sleeper,
    )
    assert not result.ok
    assert "too large" in result.detail


def test_public_https_homepage_marker_required(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_fetch(*_a, **_k):  # noqa: ANN001
        return 200, b"<html><body>no brand</body></html>"

    monkeypatch.setattr(
        "fetchnow_release.routing_health._fetch_public",
        fake_fetch,
    )
    clock, sleeper = _advancing_clock()
    result = check_public_https_path(
        PublicHttpsGateConfig(origin="https://fetchnow.online"),
        "/",
        expect_json=False,
        require_homepage_marker=True,
        deadline_seconds=0.05,
        retry_delay=0.0,
        clock=clock,
        sleeper=sleeper,
    )
    assert not result.ok
    assert "marker" in result.detail


def test_public_https_gate_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_fetch(url: str, **_k):  # noqa: ANN001
        if url.endswith("/"):
            return 200, f"<html><p {HOMEPAGE_MARKER}</html>".encode()
        return 200, b'{"status":"ok"}'

    monkeypatch.setattr(
        "fetchnow_release.routing_health._fetch_public",
        fake_fetch,
    )
    result = check_public_https_gate(
        PublicHttpsGateConfig(origin="https://fetchnow.online"),
        clock=lambda: 0.0,
        sleeper=lambda _s: None,
    )
    assert result.ok


def test_routing_wait_retries_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake_check(base: str, path: str, **_k):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] < 3:
            return HttpCheckResult(path, 1, 502, False, "status=502")
        return HttpCheckResult(path, 1, 200, True, "ok")

    monkeypatch.setattr(
        "fetchnow_release.routing_health.check_endpoint",
        fake_check,
    )
    now = {"t": 0.0}

    def clock() -> float:
        return now["t"]

    def sleeper(seconds: float) -> None:
        now["t"] += seconds

    result = wait_gateway_routing_ready(
        "http://127.0.0.1:8091",
        deadline_seconds=5.0,
        poll_seconds=0.5,
        clock=clock,
        sleeper=sleeper,
    )
    assert result.ok
    assert calls["n"] >= 4  # live fail, live+ready success path


def test_routing_wait_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "fetchnow_release.routing_health.check_endpoint",
        lambda *_a, **_k: HttpCheckResult("/x", 1, 502, False, "status=502"),
    )
    now = {"t": 0.0}

    def clock() -> float:
        return now["t"]

    def sleeper(seconds: float) -> None:
        now["t"] += max(seconds, 0.6)

    result = wait_gateway_routing_ready(
        "http://127.0.0.1:8091",
        deadline_seconds=1.0,
        poll_seconds=0.5,
        clock=clock,
        sleeper=sleeper,
    )
    assert not result.ok
    assert "converge" in result.messages[0]


def test_loopback_contract_not_weakened() -> None:
    now = {"t": 0.0}

    def clock() -> float:
        return now["t"]

    def sleeper(seconds: float) -> None:
        now["t"] += max(seconds, 0.02)

    result = wait_gateway_routing_ready(
        "https://fetchnow.online",
        deadline_seconds=0.05,
        poll_seconds=0.02,
        clock=clock,
        sleeper=sleeper,
    )
    assert not result.ok
    assert "loopback" in result.messages[0].lower() or "converge" in result.messages[0]


def test_transient_classifier() -> None:
    assert is_transient_routing_failure("connection error: refused")
    assert is_transient_routing_failure("status=502")
    assert not is_transient_routing_failure("JSON status field is not 'ok'")


def test_resolve_public_https_real_vs_isolated(tmp_path: Path) -> None:
    env = tmp_path / "env"
    env.write_text("x=1\n", encoding="utf-8")
    real = HealthInput(
        project_name="fetchnow-staging",
        env_file=env,
        compose_files=(tmp_path / "c.yaml",),
        expected_revision="a" * 40,
        repo_root=tmp_path,
    )
    cfg = resolve_public_https_config(real)
    assert cfg is not None
    assert cfg.origin == "https://staging.fetchnow.online"

    isolated = HealthInput(
        project_name="fetchnow-rollout-test-abcdef12",
        env_file=env,
        compose_files=(tmp_path / "c.yaml",),
        expected_revision="a" * 40,
        repo_root=tmp_path,
    )
    assert resolve_public_https_config(isolated) is None


def test_stabilize_public_failure_blocks_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = tmp_path / "env"
    env.write_text("x=1\n", encoding="utf-8")
    inp = HealthInput(
        project_name="fetchnow-production",
        env_file=env,
        compose_files=(tmp_path / "c.yaml",),
        expected_revision="a" * 40,
        repo_root=tmp_path,
        public_https=PublicHttpsGateConfig(origin="https://fetchnow.online"),
    )
    monkeypatch.setattr(
        "fetchnow_release.stabilize.wait_gateway_routing_ready",
        lambda *_a, **_k: MagicMock(ok=True, messages=("OK: routing",)),
    )
    monkeypatch.setattr(
        "fetchnow_release.stabilize.collect_restart_counts",
        lambda **_k: {"api": 0},
    )
    monkeypatch.setattr(
        "fetchnow_release.stabilize.run_health",
        lambda _inp: HealthResult(True, ("OK: health gate passed",)),
    )
    monkeypatch.setattr(
        "fetchnow_release.stabilize.check_public_https_gate",
        lambda *_a, **_k: MagicMock(
            ok=False, messages=("FAIL: public HTTPS /: TLS verification failed",)
        ),
    )
    with pytest.raises(StabilizeError, match="public HTTPS"):
        stabilize_full_health(
            inp,
            policy=StabilizationPolicy(
                consecutive_successes=1,
                interval_seconds=0.0,
                window_seconds=1.0,
            ),
            clock=lambda: 0.0,
            sleeper=lambda _s: None,
        )


def test_stabilize_requires_routing_before_health(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = tmp_path / "env"
    env.write_text("x=1\n", encoding="utf-8")
    inp = HealthInput(
        project_name="fetchnow-rollout-test-abcdef12",
        env_file=env,
        compose_files=(tmp_path / "c.yaml",),
        expected_revision="a" * 40,
        repo_root=tmp_path,
    )
    order: list[str] = []

    def routing(*_a, **_k):  # noqa: ANN001
        order.append("routing")
        return MagicMock(ok=True, messages=("OK: routing",))

    def health(_inp):  # noqa: ANN001
        order.append("health")
        return HealthResult(True, ("OK: health gate passed",))

    monkeypatch.setattr(
        "fetchnow_release.stabilize.wait_gateway_routing_ready", routing
    )
    monkeypatch.setattr(
        "fetchnow_release.stabilize.collect_restart_counts",
        lambda **_k: {"api": 0},
    )
    monkeypatch.setattr("fetchnow_release.stabilize.run_health", health)
    result = stabilize_full_health(
        inp,
        policy=StabilizationPolicy(
            consecutive_successes=1, interval_seconds=0.0, window_seconds=1.0
        ),
        clock=lambda: 0.0,
        sleeper=lambda _s: None,
    )
    assert result.ok
    assert order == ["routing", "health"]


def test_routing_permanent_json_error_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def fake_check(_base: str, path: str, **_k):  # noqa: ANN001
        calls["n"] += 1
        return HttpCheckResult(path, 1, 200, False, "JSON status field is not 'ok'")

    monkeypatch.setattr(
        "fetchnow_release.routing_health.check_endpoint",
        fake_check,
    )
    now = {"t": 0.0}

    def clock() -> float:
        return now["t"]

    def sleeper(seconds: float) -> None:
        now["t"] += seconds

    result = wait_gateway_routing_ready(
        "http://127.0.0.1:8091",
        deadline_seconds=30.0,
        poll_seconds=1.0,
        clock=clock,
        sleeper=sleeper,
    )
    assert not result.ok
    assert "permanent" in result.messages[0]
    assert calls["n"] == 1
    assert now["t"] == 0.0


def test_routing_deadline_accounts_for_nested_probe_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = {"t": 0.0}
    probes = {"n": 0}

    def clock() -> float:
        return now["t"]

    def sleeper(seconds: float) -> None:
        now["t"] += seconds

    def fake_check(_base: str, path: str, **kwargs):  # noqa: ANN001
        probes["n"] += 1
        # Nested probe duration is charged against the shared convergence clock.
        now["t"] += float(kwargs.get("deadline_seconds", 1.0))
        return HttpCheckResult(path, 1, 502, False, "status=502")

    monkeypatch.setattr(
        "fetchnow_release.routing_health.check_endpoint",
        fake_check,
    )
    result = wait_gateway_routing_ready(
        "http://127.0.0.1:8091",
        deadline_seconds=2.0,
        poll_seconds=0.1,
        http_timeout=1.0,
        clock=clock,
        sleeper=sleeper,
    )
    assert not result.ok
    assert "converge" in result.messages[0]
    assert now["t"] >= 2.0
    assert probes["n"] >= 1
    assert probes["n"] <= 4


def test_stabilize_production_infers_public_https(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = tmp_path / "env"
    env.write_text("x=1\n", encoding="utf-8")
    inp = HealthInput(
        project_name="fetchnow-production",
        env_file=env,
        compose_files=(tmp_path / "c.yaml",),
        expected_revision="a" * 40,
        repo_root=tmp_path,
    )
    assert inp.public_https is None
    cfg = resolve_public_https_config(inp)
    assert cfg is not None
    assert cfg.origin == "https://fetchnow.online"
    assert cfg.allow_test_origin is False

    public_calls: list[PublicHttpsGateConfig] = []

    monkeypatch.setattr(
        "fetchnow_release.stabilize.wait_gateway_routing_ready",
        lambda *_a, **_k: MagicMock(ok=True, messages=("OK: routing",)),
    )
    monkeypatch.setattr(
        "fetchnow_release.stabilize.collect_restart_counts",
        lambda **_k: {"api": 0},
    )
    monkeypatch.setattr(
        "fetchnow_release.stabilize.run_health",
        lambda _inp: HealthResult(True, ("OK: health gate passed",)),
    )

    def capture_public(config: PublicHttpsGateConfig, **_k):  # noqa: ANN001
        public_calls.append(config)
        return MagicMock(ok=True, messages=("OK: public HTTPS gate passed",))

    monkeypatch.setattr(
        "fetchnow_release.stabilize.check_public_https_gate",
        capture_public,
    )
    result = stabilize_full_health(
        inp,
        policy=StabilizationPolicy(
            consecutive_successes=1, interval_seconds=0.0, window_seconds=1.0
        ),
        clock=lambda: 0.0,
        sleeper=lambda _s: None,
    )
    assert result.ok
    assert len(public_calls) == 1
    assert public_calls[0].origin == "https://fetchnow.online"
    assert public_calls[0].allow_test_origin is False


def test_public_https_permanent_error_not_retried_as_churn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def boom(*_a, **_k):  # noqa: ANN001
        calls["n"] += 1
        raise RoutingHealthError("JSON status field is not 'ok'")

    monkeypatch.setattr(
        "fetchnow_release.routing_health._fetch_public",
        boom,
    )
    now = {"t": 0.0}
    result = check_public_https_path(
        PublicHttpsGateConfig(origin="https://fetchnow.online"),
        "/api/v1/health/live",
        expect_json=True,
        deadline_seconds=30.0,
        retry_delay=1.0,
        clock=lambda: now["t"],
        sleeper=lambda s: now.__setitem__("t", now["t"] + s),
    )
    assert not result.ok
    assert calls["n"] == 1
    assert now["t"] == 0.0
