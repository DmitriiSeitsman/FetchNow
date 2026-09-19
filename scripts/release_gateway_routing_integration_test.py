#!/usr/bin/env python3
"""Isolated gateway routing resilience integration (Production Reliability A).

Proves api/web zone+resolve IP convergence and variable delivery DNS through a
disposable Docker network and the repository ``deploy/nginx/nginx.conf``, without
recreating or reloading the gateway process.

Uses unique names prefixed ``fetchnow-routing-test-`` only. Does not require
production site availability.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fetchnow_release.redact import redact  # noqa: E402
from fetchnow_release.routing_health import (  # noqa: E402
    HOMEPAGE_MARKER,
    PUBLIC_LIVE_PATH,
    PUBLIC_READY_PATH,
    PublicHttpsGateConfig,
    RoutingHealthError,
    ROUTING_CONVERGENCE_DEADLINE_SECONDS,
    check_public_https_gate,
    validate_public_https_origin,
)

NGINX_CONF = ROOT / "deploy" / "nginx" / "nginx.conf"
NGINX_IMAGE = "nginx:1.31.3-alpine"
PYTHON_IMAGE = "python:3.14-alpine"
BUSYBOX_IMAGE = "busybox:1.37.0"

PROJECT_RE = re.compile(r"^fetchnow-routing-test-[a-z0-9]{8,32}$")
FORBIDDEN = frozenset(
    {
        "",
        "fetchnow",
        "fetchnow-staging",
        "fetchnow-production",
        "fetchnow-prod",
        "fetchnow-routing-test",
        "fetchnow-health-test",
        "fetchnow-rollout-test",
    }
)

TLS_HOST = "routing.fetchnow.test"
TLS_PORT = 18443
DELIVERY_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
DELIVERY_PATH = f"/api/v1/media/download-jobs/{DELIVERY_UUID}/content"
POLL_SECONDS = 1.0


def assert_safe_project(name: str) -> str:
    if name in FORBIDDEN or not PROJECT_RE.fullmatch(name):
        raise SystemExit(f"refusing unsafe project name: {name!r}")
    return name


def make_project() -> str:
    run = re.sub(r"[^0-9]", "", os.environ.get("GITHUB_RUN_ID", ""))
    rand = secrets.token_hex(4)
    suffix = f"{run[-8:]}{rand}" if run else secrets.token_hex(6)
    if len(suffix) > 32:
        suffix = suffix[-32:]
    return assert_safe_project(f"fetchnow-routing-test-{suffix}")


def run(
    cmd: list[str], *, check: bool = True, timeout: float | None = 120
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"{redact(proc.stderr or proc.stdout or '')}"
        )
    return proc


def http_get(url: str, *, timeout: float = 5.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return int(getattr(resp, "status", 200)), resp.read(65536)
    except urllib.error.HTTPError as exc:
        body = exc.read(65536) if exc.fp is not None else b""
        return int(exc.code), body


def write_mock_servers(fixture_dir: Path) -> None:
    """Write stdlib mock upstream servers mounted into containers."""
    (fixture_dir / "mock_upstream.py").write_text(
        '''#!/usr/bin/env python3
"""Generation-aware mock upstream for gateway routing integration."""
from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROLE = os.environ["MOCK_ROLE"]
GENERATION = os.environ.get("GENERATION", "A")
PORT = int(os.environ.get("PORT", "8000"))

DOWNLOAD_RE = re.compile(
    r"^/api/v1/media/download-jobs/[0-9a-fA-F-]{36}/content$"
)
HOMEPAGE = (
    "<!DOCTYPE html><html><body>"
    '<p class="brand">FetchNow</p>'
    f"<!-- gen={GENERATION} -->"
    "</body></html>"
).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: ANN002
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if ROLE == "api":
            if path in ("/api/v1/health/live", "/api/v1/health/ready"):
                payload = json.dumps(
                    {"status": "ok", "generation": GENERATION}
                ).encode("utf-8")
                self._send(200, payload, "application/json")
                return
            self._send(404, b"not-found", "text/plain")
            return
        if ROLE == "web":
            if path == "/":
                self._send(200, HOMEPAGE, "text/html; charset=utf-8")
                return
            self._send(404, b"not-found", "text/plain")
            return
        if ROLE == "delivery":
            if DOWNLOAD_RE.fullmatch(path):
                self._send(200, b"delivery-ok", "text/plain")
                return
            self._send(404, b"not-found", "text/plain")
            return
        self._send(500, b"bad-role", "text/plain")


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
''',
        encoding="utf-8",
    )


class OwnedResources:
    def __init__(self, project: str) -> None:
        self.project = project
        self.network: str | None = None
        self.containers: list[str] = []
        self.temp_dirs: list[Path] = []
        self.tls_server: ThreadingHTTPServer | None = None
        self._getaddrinfo_orig = None

    def track_container(self, name: str) -> str:
        if name not in self.containers:
            self.containers.append(name)
        return name

    def track_temp(self, path: Path) -> Path:
        self.temp_dirs.append(path)
        return path


def container_ip(name: str) -> str:
    proc = run(
        [
            "docker",
            "inspect",
            "-f",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            name,
        ]
    )
    ip = proc.stdout.strip()
    if not ip:
        raise RuntimeError(f"no IP for container {name}")
    return ip


def gateway_identity(name: str) -> tuple[str, str]:
    proc = run(
        [
            "docker",
            "inspect",
            "-f",
            "{{.Id}} {{.State.Pid}}",
            name,
        ]
    )
    parts = proc.stdout.strip().split()
    if len(parts) != 2 or parts[1] in {"0", ""}:
        raise RuntimeError(f"unexpected gateway identity: {proc.stdout!r}")
    return parts[0], parts[1]


def assert_gateway_unchanged(name: str, expected: tuple[str, str]) -> None:
    current = gateway_identity(name)
    if current != expected:
        raise RuntimeError(
            f"gateway identity changed: expected id/pid {expected}, got {current}"
        )


def wait_http_ok(url: str, *, deadline: float = 30.0) -> bytes:
    end = time.monotonic() + deadline
    last = "not attempted"
    while time.monotonic() < end:
        try:
            status, body = http_get(url, timeout=3.0)
            if status == 200:
                return body
            last = f"status={status}"
        except Exception as exc:  # noqa: BLE001
            last = redact(str(exc))
        time.sleep(POLL_SECONDS)
    raise RuntimeError(f"timeout waiting for {url}: {last}")


def poll_api_generation(
    base: str, expected: str, *, deadline: float = ROUTING_CONVERGENCE_DEADLINE_SECONDS
) -> None:
    end = time.monotonic() + deadline
    last = "not attempted"
    while time.monotonic() < end:
        gens: list[str] = []
        try:
            for path in (PUBLIC_LIVE_PATH, PUBLIC_READY_PATH):
                status, body = http_get(f"{base}{path}", timeout=3.0)
                if status != 200:
                    last = f"{path} status={status}"
                    break
                payload = json.loads(body.decode("utf-8"))
                gen = str(payload.get("generation", ""))
                gens.append(gen)
                if gen == "A" and expected == "B":
                    last = f"still generation A on {path}"
                    break
                if gen != expected:
                    last = f"{path} generation={gen!r} want={expected!r}"
                    break
            else:
                if gens == [expected, expected]:
                    print(f"OK: gateway API generation={expected}")
                    return
        except Exception as exc:  # noqa: BLE001
            last = redact(str(exc))
        time.sleep(POLL_SECONDS)
    raise RuntimeError(
        f"API generation did not converge to {expected} within {deadline:.0f}s: {last}"
    )


def poll_web_generation(
    base: str, expected: str, *, deadline: float = ROUTING_CONVERGENCE_DEADLINE_SECONDS
) -> None:
    marker = f"<!-- gen={expected} -->"
    stale = f"<!-- gen={'A' if expected == 'B' else 'B'} -->"
    end = time.monotonic() + deadline
    last = "not attempted"
    while time.monotonic() < end:
        try:
            status, body = http_get(f"{base}/", timeout=3.0)
            if status != 200:
                last = f"status={status}"
            else:
                text = body.decode("utf-8", errors="replace")
                if HOMEPAGE_MARKER not in text:
                    last = "homepage brand marker missing"
                elif stale in text and expected == "B":
                    last = "still generation A homepage"
                elif marker not in text:
                    last = f"generation marker {marker!r} missing"
                else:
                    print(f"OK: gateway web generation={expected}")
                    return
        except Exception as exc:  # noqa: BLE001
            last = redact(str(exc))
        time.sleep(POLL_SECONDS)
    raise RuntimeError(
        f"web generation did not converge to {expected} within {deadline:.0f}s: {last}"
    )


def start_mock(
    owned: OwnedResources,
    *,
    name: str,
    role: str,
    generation: str,
    port: int,
    ip: str,
    aliases: list[str],
    fixture_dir: Path,
    network: str,
) -> str:
    owned.track_container(name)
    cmd = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "--network",
        network,
        "--ip",
        ip,
        "-e",
        f"MOCK_ROLE={role}",
        "-e",
        f"GENERATION={generation}",
        "-e",
        f"PORT={port}",
        "-v",
        f"{fixture_dir}:/fixture:ro",
    ]
    for alias in aliases:
        cmd.extend(["--network-alias", alias])
    cmd.extend([PYTHON_IMAGE, "python", "/fixture/mock_upstream.py"])
    run(cmd)
    # Smoke: container still running.
    inspect = run(["docker", "inspect", "-f", "{{.State.Running}}", name])
    if inspect.stdout.strip() != "true":
        logs = run(["docker", "logs", name], check=False)
        raise RuntimeError(
            f"mock {name} failed to stay running:\n{redact(logs.stderr or logs.stdout)}"
        )
    return name


def stop_remove(name: str) -> None:
    run(["docker", "rm", "-f", name], check=False)


def start_ip_holder(owned: OwnedResources, *, name: str, network: str, ip: str) -> str:
    owned.track_container(name)
    run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "--network",
            network,
            "--ip",
            ip,
            BUSYBOX_IMAGE,
            "sleep",
            "infinity",
        ]
    )
    got = container_ip(name)
    if got != ip:
        raise RuntimeError(f"IP holder {name} got {got}, want {ip}")
    return name


def replace_upstream_ip(
    owned: OwnedResources,
    *,
    old_name: str,
    holder_name: str,
    new_name: str,
    role: str,
    generation: str,
    listen_port: int,
    old_ip: str,
    new_ip: str,
    alias: str,
    fixture_dir: Path,
    network: str,
) -> str:
    stop_remove(old_name)
    if old_name in owned.containers:
        owned.containers.remove(old_name)
    start_ip_holder(owned, name=holder_name, network=network, ip=old_ip)
    start_mock(
        owned,
        name=new_name,
        role=role,
        generation=generation,
        port=listen_port,
        ip=new_ip,
        aliases=[alias],
        fixture_dir=fixture_dir,
        network=network,
    )
    new_got = container_ip(new_name)
    if new_got == old_ip:
        raise RuntimeError(f"new {role} reused old IP {old_ip}")
    if new_got != new_ip:
        raise RuntimeError(f"new {role} IP {new_got} != requested {new_ip}")
    holder_got = container_ip(holder_name)
    if holder_got != old_ip:
        raise RuntimeError(f"holder lost old IP: {holder_got} != {old_ip}")
    print(f"OK: {role} IP replaced {old_ip} -> {new_ip} (holder keeps {old_ip})")
    return new_name


class _TlsHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: ANN002
        return

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/":
            body = (
                "<!DOCTYPE html><html><body>"
                '<p class="brand">FetchNow</p>'
                "<!-- gen=TLS -->"
                "</body></html>"
            ).encode("utf-8")
            ctype = "text/html; charset=utf-8"
        elif path in (PUBLIC_LIVE_PATH, PUBLIC_READY_PATH):
            body = b'{"status":"ok","generation":"TLS"}'
            ctype = "application/json"
        else:
            body = b"forbidden"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def generate_tls_material(tls_dir: Path) -> tuple[Path, Path]:
    cert = tls_dir / "cert.pem"
    key = tls_dir / "key.pem"
    # Prefer OpenSSL CLI for a disposable self-signed leaf with SAN.
    proc = run(
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
            f"/CN={TLS_HOST}",
            "-addext",
            f"subjectAltName=DNS:{TLS_HOST}",
        ],
        check=False,
    )
    if proc.returncode != 0 or not cert.is_file() or not key.is_file():
        raise RuntimeError(
            f"openssl failed to mint TLS material:\n{redact(proc.stderr or proc.stdout)}"
        )
    os.chmod(cert, 0o600)
    os.chmod(key, 0o600)
    return cert, key


def start_tls_stub_host(owned: OwnedResources, cert: Path, key: Path) -> None:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
    server = ThreadingHTTPServer(("127.0.0.1", TLS_PORT), _TlsHandler)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    owned.tls_server = server
    thread = threading.Thread(target=server.serve_forever, name="tls-stub", daemon=True)
    thread.start()
    # Confirm the listener accepts TCP before HTTPS probes.
    end = time.monotonic() + 5.0
    last = "not attempted"
    while time.monotonic() < end:
        try:
            with socket.create_connection(("127.0.0.1", TLS_PORT), timeout=1.0):
                print(f"OK: disposable TLS stub listening on 127.0.0.1:{TLS_PORT}")
                return
        except OSError as exc:
            last = redact(str(exc))
            time.sleep(0.1)
    raise RuntimeError(f"TLS stub not reachable on :{TLS_PORT} ({last})")


def start_tls_stub(
    owned: OwnedResources,
    *,
    cert: Path,
    key: Path,
) -> None:
    start_tls_stub_host(owned, cert, key)


def install_test_dns_alias(
    owned: OwnedResources, host: str, ip: str = "127.0.0.1"
) -> None:
    orig = socket.getaddrinfo

    def patched(name, port, family=0, type=0, proto=0, flags=0):  # noqa: A002, ANN001
        if name == host:
            name = ip
        return orig(name, port, family, type, proto, flags)

    owned._getaddrinfo_orig = orig
    socket.getaddrinfo = patched  # type: ignore[assignment]


def cleanup(owned: OwnedResources) -> None:
    if owned.tls_server is not None:
        try:
            owned.tls_server.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            owned.tls_server.server_close()
        except Exception:  # noqa: BLE001
            pass
        owned.tls_server = None
    if owned._getaddrinfo_orig is not None:
        socket.getaddrinfo = owned._getaddrinfo_orig  # type: ignore[assignment]
        owned._getaddrinfo_orig = None
    for name in list(reversed(owned.containers)):
        stop_remove(name)
    owned.containers.clear()
    if owned.network:
        run(["docker", "network", "rm", owned.network], check=False)
        owned.network = None
    for path in owned.temp_dirs:
        shutil.rmtree(path, ignore_errors=True)
    owned.temp_dirs.clear()


def print_diagnostics(owned: OwnedResources, gateway: str | None) -> None:
    print("DIAG: sanitized failure diagnostics")
    if gateway:
        logs = run(["docker", "logs", "--tail", "80", gateway], check=False)
        print(redact((logs.stdout or "") + (logs.stderr or "")))
    for name in owned.containers:
        insp = run(
            [
                "docker",
                "inspect",
                "-f",
                "{{.Name}} {{.State.Status}} {{.State.Error}}",
                name,
            ],
            check=False,
        )
        print(redact(insp.stdout.strip() or f"(missing {name})"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-file", type=Path, default=None)
    args = parser.parse_args(argv)

    if not NGINX_CONF.is_file():
        raise SystemExit(f"missing gateway config: {NGINX_CONF}")

    project = make_project()
    if args.project_file:
        args.project_file.parent.mkdir(parents=True, exist_ok=True)
        args.project_file.write_text(project + "\n", encoding="utf-8")
        os.chmod(args.project_file, 0o600)

    owned = OwnedResources(project)
    gateway_name = f"{project}-gateway"
    api_a = f"{project}-api-a"
    api_b = f"{project}-api-b"
    api_c = f"{project}-api-c"
    web_a = f"{project}-web-a"
    web_b = f"{project}-web-b"
    web_c = f"{project}-web-c"
    delivery = f"{project}-delivery"
    holder_api_a = f"{project}-hold-api-a"
    holder_api_b = f"{project}-hold-api-b"
    holder_web_a = f"{project}-hold-web-a"
    holder_web_b = f"{project}-hold-web-b"

    # Disposable private /24; keep away from common Docker defaults.
    third = 20 + secrets.randbelow(200)
    subnet = f"10.201.{third}.0/24"
    ip_api_a = f"10.201.{third}.10"
    ip_api_b = f"10.201.{third}.11"
    ip_api_c = f"10.201.{third}.12"
    ip_web_a = f"10.201.{third}.20"
    ip_web_b = f"10.201.{third}.21"
    ip_web_c = f"10.201.{third}.22"
    ip_delivery = f"10.201.{third}.30"

    gateway_host_port = 19000 + secrets.randbelow(1000)
    base = f"http://127.0.0.1:{gateway_host_port}"
    network = project
    gw_id: tuple[str, str] | None = None
    rc = 1

    try:
        assert_safe_project(project)
        print(
            "NOTE: does not require production site availability; "
            f"project={project} subnet={subnet}"
        )

        fixture_dir = owned.track_temp(
            Path(tempfile.mkdtemp(prefix="fetchnow-routing-fixture-"))
        )
        tls_dir = owned.track_temp(
            Path(tempfile.mkdtemp(prefix="fetchnow-routing-tls-"))
        )
        os.chmod(fixture_dir, 0o700)
        os.chmod(tls_dir, 0o700)
        write_mock_servers(fixture_dir)

        for image in (NGINX_IMAGE, PYTHON_IMAGE, BUSYBOX_IMAGE):
            print(f"Ensuring image {image}…")
            run(["docker", "image", "inspect", image], check=False)
            pull = run(["docker", "pull", image], check=False, timeout=300)
            if pull.returncode != 0:
                # inspect may already have succeeded for a local tag
                insp = run(["docker", "image", "inspect", image], check=False)
                if insp.returncode != 0:
                    raise RuntimeError(f"unable to pull/find image {image}")

        owned.network = network
        run(
            [
                "docker",
                "network",
                "create",
                "--driver",
                "bridge",
                "--subnet",
                subnet,
                network,
            ]
        )
        print(f"OK: disposable network {network}")

        # --- Start without delivery DNS ---
        start_mock(
            owned,
            name=api_a,
            role="api",
            generation="A",
            port=8000,
            ip=ip_api_a,
            aliases=["api"],
            fixture_dir=fixture_dir,
            network=network,
        )
        start_mock(
            owned,
            name=web_a,
            role="web",
            generation="A",
            port=8080,
            ip=ip_web_a,
            aliases=["web"],
            fixture_dir=fixture_dir,
            network=network,
        )
        print("OK: api/web generation A started (delivery absent)")

        owned.track_container(gateway_name)
        run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                gateway_name,
                "--network",
                network,
                "-p",
                f"127.0.0.1:{gateway_host_port}:8080",
                "-v",
                f"{NGINX_CONF}:/etc/nginx/nginx.conf:ro",
                NGINX_IMAGE,
                "nginx",
                "-g",
                "daemon off;",
            ]
        )
        gw_id = gateway_identity(gateway_name)
        print(f"OK: gateway started id={gw_id[0][:12]} pid={gw_id[1]}")

        wait_http_ok(f"{base}{PUBLIC_LIVE_PATH}")
        poll_api_generation(base, "A")
        poll_web_generation(base, "A")
        assert_gateway_unchanged(gateway_name, gw_id)
        print("OK: web/API via gateway work without delivery DNS")

        # --- API IP replacement A -> B ---
        recorded_api_ip = container_ip(api_a)
        if recorded_api_ip != ip_api_a:
            raise RuntimeError(f"initial API IP drift: {recorded_api_ip}")
        replace_upstream_ip(
            owned,
            old_name=api_a,
            holder_name=holder_api_a,
            new_name=api_b,
            role="api",
            generation="B",
            listen_port=8000,
            old_ip=ip_api_a,
            new_ip=ip_api_b,
            alias="api",
            fixture_dir=fixture_dir,
            network=network,
        )
        assert_gateway_unchanged(gateway_name, gw_id)
        poll_api_generation(base, "B")
        assert_gateway_unchanged(gateway_name, gw_id)

        # --- Web IP replacement A -> B ---
        recorded_web_ip = container_ip(web_a)
        if recorded_web_ip != ip_web_a:
            raise RuntimeError(f"initial web IP drift: {recorded_web_ip}")
        replace_upstream_ip(
            owned,
            old_name=web_a,
            holder_name=holder_web_a,
            new_name=web_b,
            role="web",
            generation="B",
            listen_port=8080,
            old_ip=ip_web_a,
            new_ip=ip_web_b,
            alias="web",
            fixture_dir=fixture_dir,
            network=network,
        )
        assert_gateway_unchanged(gateway_name, gw_id)
        poll_web_generation(base, "B")
        assert_gateway_unchanged(gateway_name, gw_id)

        # --- Restore previous generation on new IPs ---
        replace_upstream_ip(
            owned,
            old_name=api_b,
            holder_name=holder_api_b,
            new_name=api_c,
            role="api",
            generation="A",
            listen_port=8000,
            old_ip=ip_api_b,
            new_ip=ip_api_c,
            alias="api",
            fixture_dir=fixture_dir,
            network=network,
        )
        replace_upstream_ip(
            owned,
            old_name=web_b,
            holder_name=holder_web_b,
            new_name=web_c,
            role="web",
            generation="A",
            listen_port=8080,
            old_ip=ip_web_b,
            new_ip=ip_web_c,
            alias="web",
            fixture_dir=fixture_dir,
            network=network,
        )
        assert_gateway_unchanged(gateway_name, gw_id)
        poll_api_generation(base, "A")
        poll_web_generation(base, "A")
        assert_gateway_unchanged(gateway_name, gw_id)
        print("OK: routing restored to generation A after second IP change")

        # --- Delivery variable DNS ---
        start_mock(
            owned,
            name=delivery,
            role="delivery",
            generation="D",
            port=8000,
            ip=ip_delivery,
            aliases=["delivery"],
            fixture_dir=fixture_dir,
            network=network,
        )
        assert_gateway_unchanged(gateway_name, gw_id)
        end = time.monotonic() + ROUTING_CONVERGENCE_DEADLINE_SECONDS
        last = "not attempted"
        while time.monotonic() < end:
            try:
                status, body = http_get(f"{base}{DELIVERY_PATH}", timeout=5.0)
                if status == 200 and body == b"delivery-ok":
                    print("OK: delivery content route via variable DNS")
                    break
                last = f"status={status} body={body[:40]!r}"
            except Exception as exc:  # noqa: BLE001
                last = redact(str(exc))
            time.sleep(POLL_SECONDS)
        else:
            raise RuntimeError(f"delivery route failed within deadline: {last}")
        assert_gateway_unchanged(gateway_name, gw_id)

        stop_remove(delivery)
        if delivery in owned.containers:
            owned.containers.remove(delivery)
        time.sleep(1.0)
        poll_api_generation(base, "A", deadline=15.0)
        poll_web_generation(base, "A", deadline=15.0)
        assert_gateway_unchanged(gateway_name, gw_id)
        print("OK: web/API still work through gateway after delivery stop")

        # --- Public HTTPS gate with disposable TLS ---
        try:
            validate_public_https_origin("https://evil.example")
            raise RuntimeError("validate_public_https_origin accepted evil.example")
        except RoutingHealthError:
            print("OK: validate_public_https_origin rejects arbitrary hosts")
        try:
            validate_public_https_origin("https://not-approved.test")
            raise RuntimeError("validate_public_https_origin accepted unapproved .test")
        except RoutingHealthError:
            pass

        cert, key = generate_tls_material(tls_dir)
        install_test_dns_alias(owned, TLS_HOST)
        start_tls_stub(owned, cert=cert, key=key)

        client_ctx = ssl.create_default_context(cafile=str(cert))
        origin = f"https://{TLS_HOST}:{TLS_PORT}"
        gate = check_public_https_gate(
            PublicHttpsGateConfig(
                origin=origin,
                ssl_context=client_ctx,
                allow_test_origin=True,
            )
        )
        if not gate.ok:
            raise RuntimeError(
                "public HTTPS gate failed: "
                + "; ".join(redact(m) for m in gate.messages)
            )
        print("OK: check_public_https_gate passed with disposable TLS")

        # Internal/diagnostic paths must not be public on the TLS fixture.
        for path in ("/api/v1/health/startup", "/metrics", "/nginx_status", "/debug"):
            try:
                opener = urllib.request.build_opener(
                    urllib.request.HTTPSHandler(context=client_ctx)
                )
                req = urllib.request.Request(f"{origin}{path}", method="GET")
                with opener.open(req, timeout=5) as resp:  # noqa: S310
                    status = int(getattr(resp, "status", 200))
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"unexpected TLS diagnostic probe error for {path}: {redact(str(exc))}"
                ) from exc
            if status == 200:
                raise RuntimeError(f"TLS fixture unexpectedly public for {path}")
        print("OK: TLS fixture does not expose internal diagnostic paths")

        assert_gateway_unchanged(gateway_name, gw_id)
        print("OK: gateway routing integration verification passed")
        rc = 0
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        try:
            print_diagnostics(owned, gateway_name if gw_id else None)
        except Exception:  # noqa: BLE001
            pass
        rc = 1
    finally:
        assert_safe_project(project)
        cleanup(owned)
        print(f"OK: isolated routing project cleanup completed ({project})")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
