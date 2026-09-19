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


# ---------------------------------------------------------------------------
# Homepage vs JSON body-limit classification (Reliability A.1 hotfix)
# ---------------------------------------------------------------------------


@pytest.fixture()
def disposable_tls_origin(tmp_path: Path):
    """Local HTTPS server with a disposable self-signed cert (test-only)."""
    import http.server
    import socket
    import ssl
    import subprocess
    import threading
    from fetchnow_release.routing_health import (
        PUBLIC_HTTPS_HOMEPAGE_MAX_BODY_BYTES,
        PUBLIC_HTTPS_JSON_MAX_BODY_BYTES,
    )

    host = "localhost"
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    proc = subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-days",
            "1",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            f"/CN={host}",
            "-addext",
            f"subjectAltName=DNS:{host}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    state: dict[str, object] = {
        "home_body": f"<html><p {HOMEPAGE_MARKER}</html>".encode(),
        "home_content_length": None,  # None => use len(body)
        "live_body": b'{"status":"ok"}',
        "ready_body": b'{"status":"ok"}',
        "live_content_length": None,
        "bytes_written": 0,
        "requests": [],
    }

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:  # noqa: ANN002
            return

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            state["requests"].append(path)
            if path == "/":
                body = state["home_body"]
                assert isinstance(body, bytes)
                declared = state["home_content_length"]
                cl = len(body) if declared is None else int(declared)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(cl))
                self.end_headers()
                # Only write what was asked via bounded client reads; track writes.
                # If declared CL is huge, still only write the real body (or nothing
                # extra) so a buggy unbounded client would hang waiting — we assert
                # early rejection via bytes_written staying at real body size.
                self.wfile.write(body)
                state["bytes_written"] = int(state["bytes_written"]) + len(body)
                return
            if path == "/api/v1/health/live":
                body = state["live_body"]
                assert isinstance(body, bytes)
                declared = state["live_content_length"]
                cl = len(body) if declared is None else int(declared)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(cl))
                self.end_headers()
                self.wfile.write(body)
                state["bytes_written"] = int(state["bytes_written"]) + len(body)
                return
            if path == "/api/v1/health/ready":
                body = state["ready_body"]
                assert isinstance(body, bytes)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
                return
            self.send_response(404)
            self.end_headers()

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    # Wait until the port accepts TCP.
    end = __import__("time").monotonic() + 5.0
    while __import__("time").monotonic() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            __import__("time").sleep(0.05)
    else:
        httpd.shutdown()
        raise RuntimeError("disposable TLS fixture failed to listen")

    client_ctx = ssl.create_default_context()
    client_ctx.check_hostname = False
    client_ctx.verify_mode = ssl.CERT_NONE
    # Prefer loading the disposable CA for stricter verification path.
    client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_ctx.check_hostname = True
    client_ctx.verify_mode = ssl.CERT_REQUIRED
    client_ctx.load_verify_locations(cafile=str(cert))

    config = PublicHttpsGateConfig(
        origin=f"https://{host}:{port}",
        ssl_context=client_ctx,
        allow_test_origin=True,
    )
    yield config, state, {
        "homepage_limit": PUBLIC_HTTPS_HOMEPAGE_MAX_BODY_BYTES,
        "json_limit": PUBLIC_HTTPS_JSON_MAX_BODY_BYTES,
    }
    httpd.shutdown()
    httpd.server_close()


def test_homepage_body_between_4kib_and_64kib_passes(disposable_tls_origin) -> None:
    config, state, limits = disposable_tls_origin
    # ~14 KiB style payload: larger than JSON cap, under homepage cap.
    filler = "x" * (limits["json_limit"] + 1024)
    state["home_body"] = f"<html><p {HOMEPAGE_MARKER}{filler}</html>".encode()
    assert limits["json_limit"] < len(state["home_body"]) <= limits["homepage_limit"]
    clock, sleeper = _advancing_clock()
    result = check_public_https_path(
        config,
        "/",
        expect_json=False,
        require_homepage_marker=True,
        deadline_seconds=5.0,
        retry_delay=0.0,
        clock=clock,
        sleeper=sleeper,
    )
    assert result.ok
    assert result.detail == "ok"


def test_homepage_body_over_64kib_fails(disposable_tls_origin) -> None:
    config, state, limits = disposable_tls_origin
    over = limits["homepage_limit"] + 1
    state["home_body"] = (f"<html><p {HOMEPAGE_MARKER}" + ("y" * over)).encode()[
        : over + 50
    ]
    # Ensure actual body exceeds homepage limit.
    state["home_body"] = (
        f"<html><p {HOMEPAGE_MARKER}".encode() + b"z" * (limits["homepage_limit"])
    )
    assert len(state["home_body"]) > limits["homepage_limit"]
    state["home_content_length"] = None  # force bounded-read path
    clock, sleeper = _advancing_clock()
    result = check_public_https_path(
        config,
        "/",
        expect_json=False,
        require_homepage_marker=True,
        deadline_seconds=5.0,
        retry_delay=0.0,
        clock=clock,
        sleeper=sleeper,
    )
    assert not result.ok
    assert "too large" in result.detail
    assert HOMEPAGE_MARKER not in result.detail  # sanitized / no body dump


def test_homepage_declared_content_length_over_64kib_fails_early(
    disposable_tls_origin,
) -> None:
    config, state, limits = disposable_tls_origin
    # Real body is small/valid; declared CL exceeds homepage cap → early reject.
    state["home_body"] = f"<html><p {HOMEPAGE_MARKER}</html>".encode()
    state["home_content_length"] = limits["homepage_limit"] + 1
    before = int(state["bytes_written"])
    clock, sleeper = _advancing_clock()
    result = check_public_https_path(
        config,
        "/",
        expect_json=False,
        require_homepage_marker=True,
        deadline_seconds=5.0,
        retry_delay=0.0,
        clock=clock,
        sleeper=sleeper,
    )
    assert not result.ok
    assert "too large" in result.detail
    # Server only wrote the small real body (or nothing more). Early reject must
    # not require reading an unbounded payload from the client side.
    assert int(state["bytes_written"]) - before <= len(state["home_body"])


def test_missing_content_length_cannot_bypass_bounded_read(
    tmp_path: Path,
) -> None:
    """Oversized homepage without Content-Length still fails via bounded read."""
    import http.server
    import socket
    import ssl
    import subprocess
    import threading

    from fetchnow_release.routing_health import PUBLIC_HTTPS_HOMEPAGE_MAX_BODY_BYTES

    host = "localhost"
    cert = tmp_path / "nocl-cert.pem"
    key = tmp_path / "nocl-key.pem"
    proc = subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-days",
            "1",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            f"/CN={host}",
            "-addext",
            f"subjectAltName=DNS:{host}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    big = f"<html><p {HOMEPAGE_MARKER}".encode() + b"q" * (
        PUBLIC_HTTPS_HOMEPAGE_MAX_BODY_BYTES + 100
    )

    class NoCLHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:  # noqa: ANN002
            return

        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(big)

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), NoCLHandler)
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    server_ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
    httpd.socket = server_ctx.wrap_socket(httpd.socket, server_side=True)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    end = __import__("time").monotonic() + 5.0
    while __import__("time").monotonic() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            __import__("time").sleep(0.05)
    else:
        httpd.shutdown()
        raise RuntimeError("NoCL TLS fixture failed to listen")

    client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_ctx.check_hostname = True
    client_ctx.verify_mode = ssl.CERT_REQUIRED
    client_ctx.load_verify_locations(cafile=str(cert))
    config = PublicHttpsGateConfig(
        origin=f"https://{host}:{port}",
        ssl_context=client_ctx,
        allow_test_origin=True,
    )
    try:
        clock, sleeper = _advancing_clock()
        result = check_public_https_path(
            config,
            "/",
            expect_json=False,
            require_homepage_marker=True,
            deadline_seconds=5.0,
            retry_delay=0.0,
            clock=clock,
            sleeper=sleeper,
        )
        assert not result.ok
        assert "too large" in result.detail
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_json_live_ready_still_capped_at_4kib(disposable_tls_origin) -> None:
    config, state, limits = disposable_tls_origin
    state["live_body"] = b'{"status":"ok","pad":"' + (
        b"p" * (limits["json_limit"] + 64)
    ) + b'"}'
    assert len(state["live_body"]) > limits["json_limit"]
    clock, sleeper = _advancing_clock()
    live = check_public_https_path(
        config,
        "/api/v1/health/live",
        expect_json=True,
        deadline_seconds=5.0,
        retry_delay=0.0,
        clock=clock,
        sleeper=sleeper,
    )
    assert not live.ok
    assert "too large" in live.detail

    # Ready path with oversized JSON.
    state["ready_body"] = state["live_body"]
    clock, sleeper = _advancing_clock()
    ready = check_public_https_path(
        config,
        "/api/v1/health/ready",
        expect_json=True,
        deadline_seconds=5.0,
        retry_delay=0.0,
        clock=clock,
        sleeper=sleeper,
    )
    assert not ready.ok
    assert "too large" in ready.detail


def test_homepage_limit_constants_are_distinct() -> None:
    from fetchnow_release import HEALTH_MAX_BODY_BYTES
    from fetchnow_release.routing_health import (
        PUBLIC_HTTPS_HOMEPAGE_MAX_BODY_BYTES,
        PUBLIC_HTTPS_JSON_MAX_BODY_BYTES,
        PUBLIC_HTTPS_MAX_BODY_BYTES,
    )

    assert PUBLIC_HTTPS_JSON_MAX_BODY_BYTES == HEALTH_MAX_BODY_BYTES == 4096
    assert PUBLIC_HTTPS_HOMEPAGE_MAX_BODY_BYTES == 64 * 1024
    assert PUBLIC_HTTPS_MAX_BODY_BYTES == PUBLIC_HTTPS_JSON_MAX_BODY_BYTES
    assert PUBLIC_HTTPS_HOMEPAGE_MAX_BODY_BYTES > PUBLIC_HTTPS_JSON_MAX_BODY_BYTES


def test_production_cli_cannot_override_body_limits() -> None:
    from fetchnow_release.cli import build_parser

    help_text = build_parser().format_help()
    for banned in (
        "allow-test-origin",
        "allow_test_origin",
        "public-https-test",
        "test-origin",
        "max-body",
        "homepage-max-body",
        "PUBLIC_HTTPS_HOMEPAGE_MAX_BODY_BYTES",
        "body-limit",
    ):
        assert banned not in help_text
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            [
                "health",
                "--project-name",
                "fetchnow-staging",
                "--env-file",
                "/tmp/env",
                "--expected-revision",
                "a" * 40,
                "--homepage-max-body",
                "999999",
            ]
        )


def test_public_https_shared_deadline_remains_bounded(
    disposable_tls_origin,
) -> None:
    config, state, _limits = disposable_tls_origin
    # Force permanent failure so retries would otherwise spin.
    state["home_body"] = b"<html>no marker</html>"
    now = {"t": 0.0}
    result = check_public_https_path(
        config,
        "/",
        expect_json=False,
        require_homepage_marker=True,
        deadline_seconds=30.0,
        retry_delay=1.0,
        clock=lambda: now["t"],
        sleeper=lambda s: now.__setitem__("t", now["t"] + s),
    )
    assert not result.ok
    assert "marker" in result.detail
    assert now["t"] == 0.0  # permanent → no retry churn
