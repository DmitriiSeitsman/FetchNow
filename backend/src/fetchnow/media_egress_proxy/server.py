"""Bounded HTTPS CONNECT egress proxy for SEC-09 media-net.

CONNECT :443 only. No TLS interception. No plain HTTP absolute-form forward.
Stdlib only. Does not log URLs, query strings, cookies, or Authorization.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import ipaddress
import json
import os
import select
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from fetchnow.media_egress_proxy.policy import (
    DenyConfig,
    DialTarget,
    GetAddrInfo,
    destination_forbidden,
    dial_tcp,
    normalize_connect_host,
    resolve_dial_target,
)

_MAX_HEADER_BYTES = 8_192
_MAX_CONNECTIONS = 32
_HANDSHAKE_TIMEOUT = 5.0
_CONNECT_TIMEOUT = 10.0
_IDLE_TIMEOUT = 60.0
_TUNNEL_CHUNK = 65_536
_LISTEN_BACKLOG = 16
_DNS_TIMEOUT = 3.0
# Bound in-flight resolver work (subprocess or injected resolver threads).
_DNS_SLOTS = threading.BoundedSemaphore(8)
_DNS_INJECTED_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="egress-dns"
)


@dataclass(frozen=True, slots=True)
class ProxyLimits:
    max_header_bytes: int = _MAX_HEADER_BYTES
    max_connections: int = _MAX_CONNECTIONS
    handshake_timeout: float = _HANDSHAKE_TIMEOUT
    connect_timeout: float = _CONNECT_TIMEOUT
    idle_timeout: float = _IDLE_TIMEOUT
    dns_timeout: float = _DNS_TIMEOUT


class EgressProxy:
    def __init__(
        self,
        *,
        deny: DenyConfig | None = None,
        limits: ProxyLimits | None = None,
        resolve: GetAddrInfo | None = None,
        dial: Callable[..., socket.socket] | None = None,
    ) -> None:
        self.deny = deny or DenyConfig(networks=(), hosts=())
        self.limits = limits or ProxyLimits()
        self._resolve = resolve
        self._dial = dial
        self._connections = threading.BoundedSemaphore(self.limits.max_connections)
        self._stop = threading.Event()

    def handle_client(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        try:
            client.settimeout(self.limits.handshake_timeout)
            request_line, _headers = _read_request(
                client,
                self.limits.max_header_bytes,
                deadline=time.monotonic() + self.limits.handshake_timeout,
            )
            method, target, version = _parse_request_line(request_line)
            if method != "CONNECT" or not version.startswith("HTTP/1."):
                _send_status(client, 405, b"Method Not Allowed")
                return
            host, port = _split_authority(target)
            try:
                dial_target = _resolve_with_deadline(
                    host,
                    port,
                    config=self.deny,
                    resolver=self._resolve,
                    deadline=time.monotonic() + self.limits.dns_timeout,
                )
            except ValueError:
                _send_status(client, 403, b"Forbidden")
                return
            try:
                if self._dial is not None:
                    upstream = self._dial(
                        dial_target, timeout=self.limits.connect_timeout
                    )
                else:
                    upstream = dial_tcp(
                        dial_target, timeout=self.limits.connect_timeout
                    )
            except OSError:
                _send_status(client, 502, b"Bad Gateway")
                return
            _send_status(client, 200, b"Connection Established")
            _tunnel(client, upstream, idle_timeout=self.limits.idle_timeout)
        except (OSError, ValueError):
            with contextlib.suppress(OSError):
                _send_status(client, 400, b"Bad Request")
            return
        finally:
            if upstream is not None:
                with contextlib.suppress(OSError):
                    upstream.close()
            with contextlib.suppress(OSError):
                client.close()

    def serve_forever(self, sock: socket.socket) -> None:
        sock.listen(_LISTEN_BACKLOG)
        sock.settimeout(1.0)
        while not self._stop.is_set():
            try:
                client, _addr = sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            if not self._connections.acquire(blocking=False):
                with contextlib.suppress(OSError):
                    _send_status(client, 503, b"Service Unavailable")
                client.close()
                continue
            threading.Thread(
                target=self._serve_one,
                args=(client,),
                daemon=True,
            ).start()

    def _serve_one(self, client: socket.socket) -> None:
        try:
            self.handle_client(client)
        finally:
            self._connections.release()

    def stop(self) -> None:
        self._stop.set()


def _resolve_with_deadline(
    host: str,
    port: int,
    *,
    config: DenyConfig,
    resolver: GetAddrInfo | None,
    deadline: float,
) -> DialTarget:
    """Bound DNS without mutating process-global socket timeouts.

    Production resolution runs in a killable subprocess so a hung resolver
    cannot accumulate unbounded work. Injected test resolvers use a small
    thread pool with the same slot limit. Dial uses only returned addresses.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValueError("dns_failed")
    acquired = _DNS_SLOTS.acquire(blocking=True, timeout=min(remaining, 1.0))
    if not acquired:
        raise ValueError("dns_failed")
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("dns_failed")
        if resolver is not None:
            future = _DNS_INJECTED_POOL.submit(
                resolve_dial_target,
                host,
                port,
                config=config,
                resolver=resolver,
            )
            try:
                return future.result(timeout=remaining)
            except concurrent.futures.TimeoutError as exc:
                future.cancel()
                raise ValueError("dns_failed") from exc
        return _resolve_via_subprocess(host, port, config=config, timeout=remaining)
    finally:
        _DNS_SLOTS.release()


def _resolve_via_subprocess(
    host: str,
    port: int,
    *,
    config: DenyConfig,
    timeout: float,
) -> DialTarget:
    """Killable child performs getaddrinfo + policy; parent never dials on timeout."""
    payload = json.dumps(
        {
            "host": host,
            "port": port,
            "networks": [str(net) for net in config.networks],
            "hosts": [str(addr) for addr in config.hosts],
        }
    )
    script = (
        "import json,sys,ipaddress\n"
        "from fetchnow.media_egress_proxy.policy import ("
        " DenyConfig, resolve_dial_target)\n"
        "req=json.loads(sys.argv[1])\n"
        "cfg=DenyConfig(\n"
        "  networks=tuple(ipaddress.ip_network(x, strict=False) for x in req['networks']),\n"  # noqa: E501
        "  hosts=tuple(ipaddress.ip_address(x) for x in req['hosts']),\n"
        ")\n"
        "try:\n"
        "  target=resolve_dial_target(req['host'], int(req['port']), config=cfg)\n"
        "except ValueError as exc:\n"
        "  print('ERR\t' + str(exc))\n"
        "  raise SystemExit(2)\n"
        "print('OK\t' + json.dumps([str(a) for a in target.addresses]))\n"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script, payload],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("dns_failed") from exc
    line = ""
    if proc.stdout:
        lines = [row for row in proc.stdout.splitlines() if row.strip()]
        line = lines[-1] if lines else ""
    if proc.returncode != 0 or not line.startswith("OK\t"):
        if line.startswith("ERR\t"):
            code = line.split("\t", 1)[1]
            if code in {
                "destination_forbidden",
                "port_forbidden",
                "dns_failed",
                "host_invalid",
            }:
                raise ValueError(code)
        raise ValueError("dns_failed")
    try:
        addresses = json.loads(line.split("\t", 1)[1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise ValueError("dns_failed") from exc
    if not isinstance(addresses, list) or not addresses:
        raise ValueError("dns_failed")
    parsed = tuple(ipaddress.ip_address(item) for item in addresses)
    # Re-validate in-process so a malformed child cannot widen policy.
    for address in parsed:
        if destination_forbidden(address, config):
            raise ValueError("destination_forbidden")
    return DialTarget(host=normalize_connect_host(host), port=port, addresses=parsed)


def _read_request(
    client: socket.socket, max_bytes: int, *, deadline: float
) -> tuple[str, dict[str, str]]:
    data = bytearray()
    while len(data) <= max_bytes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("header_limit")
        client.settimeout(remaining)
        chunk = client.recv(1024)
        if not chunk:
            break
        data.extend(chunk)
        if b"\r\n\r\n" in data:
            break
    if len(data) > max_bytes or b"\r\n\r\n" not in data:
        raise ValueError("header_limit")
    head, _rest = bytes(data).split(b"\r\n\r\n", 1)
    lines = head.split(b"\r\n")
    if not lines:
        raise ValueError("empty_request")
    request_line = lines[0].decode("ascii", errors="strict")
    headers: dict[str, str] = {}
    for raw in lines[1:]:
        if b":" not in raw:
            raise ValueError("bad_header")
        name, value = raw.split(b":", 1)
        key = name.decode("ascii", errors="strict").strip().lower()
        # Do not retain Authorization / Cookie / Proxy-Authorization values.
        if key in {"authorization", "proxy-authorization", "cookie"}:
            headers[key] = "<redacted>"
            continue
        headers[key] = value.decode("ascii", errors="strict").strip()
    return request_line, headers


def _parse_request_line(line: str) -> tuple[str, str, str]:
    parts = line.split(" ")
    if len(parts) != 3:
        raise ValueError("bad_request_line")
    return parts[0].upper(), parts[1], parts[2]


def _split_authority(target: str) -> tuple[str, int]:
    text = target.strip()
    if not text or "://" in text or "/" in text or " " in text:
        raise ValueError("bad_authority")
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            raise ValueError("bad_authority")
        host = text[1:end]
        rest = text[end + 1 :]
        if not rest.startswith(":"):
            raise ValueError("bad_authority")
        port = int(rest[1:])
        return host, port
    if text.count(":") != 1:
        raise ValueError("bad_authority")
    host, port_s = text.rsplit(":", 1)
    if not host or not port_s.isdigit():
        raise ValueError("bad_authority")
    port = int(port_s)
    if not 1 <= port <= 65535:
        raise ValueError("bad_authority")
    return host, port


def _send_status(client: socket.socket, code: int, reason: bytes) -> None:
    client.settimeout(5.0)
    client.sendall(b"HTTP/1.1 %d %s\r\n\r\n" % (code, reason))


def _send_bounded(sock: socket.socket, data: bytes, *, idle_timeout: float) -> None:
    """Write with select deadlines so a stalled peer cannot hold a slot."""
    view = memoryview(data)
    sock.setblocking(False)
    while view:
        _, writable, erred = select.select([], [sock], [sock], idle_timeout)
        if erred or not writable:
            raise TimeoutError("send_idle")
        try:
            sent = sock.send(view)
        except BlockingIOError:
            continue
        if sent == 0:
            raise OSError("send_closed")
        view = view[sent:]


def _tunnel(left: socket.socket, right: socket.socket, *, idle_timeout: float) -> None:
    left.setblocking(False)
    right.setblocking(False)
    sockets = [left, right]
    while True:
        readable, _w, erred = select.select(sockets, [], sockets, idle_timeout)
        if erred or not readable:
            return
        for source in readable:
            try:
                chunk = source.recv(_TUNNEL_CHUNK)
            except BlockingIOError:
                continue
            except OSError:
                return
            if not chunk:
                return
            dest = right if source is left else left
            try:
                _send_bounded(dest, chunk, idle_timeout=idle_timeout)
            except (OSError, TimeoutError):
                return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="media-egress-proxy")
    parser.add_argument(
        "--listen-host",
        default=os.environ.get("EGRESS_LISTEN_HOST", "0.0.0.0"),
    )
    parser.add_argument(
        "--listen-port",
        type=int,
        default=int(os.environ.get("EGRESS_LISTEN_PORT", "8888")),
    )
    args = parser.parse_args(argv)
    deny = DenyConfig.from_env()
    proxy = EgressProxy(deny=deny)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.listen_host, args.listen_port))
    print(
        f"media-egress-proxy listening on {args.listen_host}:{args.listen_port}",
        flush=True,
    )
    try:
        proxy.serve_forever(sock)
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
