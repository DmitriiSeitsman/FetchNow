#!/opt/venv/bin/python
"""Controlled yt-dlp stand-in for SEC-09 native N1/N2.

Uses the real MEDIA_NET_PROXY_URL CONNECT path against a mock origin.
Not a provider extractor proof — proves launcher → proxy → origin wiring.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse


def _identity() -> dict[str, int]:
    # /proc is deliberately denied to tools. The outside observer captures
    # starttime while this PID is alive, before authorizing growth/cancellation.
    return {"pid": os.getpid()}


def _heartbeat(path: Path) -> None:
    while True:
        with path.open("ab") as stream:
            stream.write(b"x")
        time.sleep(0.1)


def _controlled_writer(path: Path, *, mode: str) -> None:
    """Ignore --max-filesize deliberately; only the executor may stop us."""
    # The production launcher inherits cwd=/, not the writable attempt. Keep
    # every fixture marker and the child's inherited cwd inside that attempt.
    path = path.absolute()
    os.chdir(path.parent)
    child = subprocess.Popen(
        [sys.executable, __file__, "--fixture-child"], start_new_session=True
    )
    try:
        deadline = time.monotonic() + 5
        while not Path("fixture-child.json").exists():
            if child.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("fixture child did not start")
            time.sleep(0.01)
        Path("fixture-parent.json").write_text(json.dumps(_identity()))
        if mode in {"oversize", "stdout"}:
            until = time.monotonic() + 15
            while not Path("fixture-grow").exists():
                if time.monotonic() >= until:
                    raise RuntimeError("harness never authorized growth")
                time.sleep(0.01)
            if mode == "stdout":
                while True:
                    sys.stdout.buffer.write(b"M" * 65_536)
                    sys.stdout.buffer.flush()
            # One MiB > the native test's 64 KiB budget; do not exit normally.
            path.write_bytes(b"M" * 1_048_576)
        else:
            path.write_bytes(b"M" * 4096)
        if mode == "neighbor":
            until = time.monotonic() + 60
            while not Path("fixture-finish").exists():
                if time.monotonic() >= until:
                    raise RuntimeError("neighbor was never released by harness")
                with Path("fixture-parent.hb").open("ab") as stream:
                    stream.write(b"x")
                time.sleep(0.1)
            return
        _heartbeat(Path("fixture-parent.hb"))
    finally:
        # Do not terminate/reap the setsid child here. The production cgroup
        # supervisor must kill and reap it, including after normal parent exit.
        pass


def _connect_via_proxy(proxy_url: str, origin_host: str, origin_port: int) -> None:
    parsed = urlparse(proxy_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.port:
        raise SystemExit("bad_proxy")
    sock = socket.create_connection((parsed.hostname, parsed.port), timeout=5)
    try:
        req = (
            f"CONNECT {origin_host}:{origin_port} HTTP/1.1\r\n"
            f"Host: {origin_host}:{origin_port}\r\n\r\n"
        ).encode("ascii")
        sock.sendall(req)
        data = b""
        while b"\r\n\r\n" not in data and len(data) < 8192:
            chunk = sock.recv(1024)
            if not chunk:
                break
            data += chunk
        if not data.startswith(b"HTTP/1.1 200"):
            raise SystemExit(f"connect_denied:{data[:80]!r}")
        sock.sendall(b"PING\n")
        reply = sock.recv(64)
        if b"PONG" not in reply:
            raise SystemExit("origin_bad_reply")
    finally:
        sock.close()


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw == ["--fixture-child"]:
        Path("fixture-child.json").write_text(json.dumps(_identity()))
        _heartbeat(Path("fixture-child.hb"))
        return 0
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--proxy", default=os.environ.get("MEDIA_NET_PROXY_URL", ""))
    parser.add_argument("--dump-single-json", action="store_true")
    parser.add_argument("-o", "--output", default="")
    parser.add_argument("--max-filesize", default="")
    parser.add_argument("url", nargs="?")
    args, _unknown = parser.parse_known_args(raw)
    url = raw[raw.index("--") + 1] if "--" in raw else args.url or ""

    origin = os.environ.get("MOCK_ORIGIN_HOST", "mock-origin")
    port = int(os.environ.get("MOCK_ORIGIN_PORT", "443"))
    if not args.proxy:
        raise SystemExit("proxy_required")
    _connect_via_proxy(args.proxy, origin, port)

    if args.dump_single_json:
        sys.stdout.write(
            json.dumps(
                {
                    "id": "mock",
                    "title": "mock",
                    "extractor": "mock",
                    "formats": [{"format_id": "fmt", "url": "https://mock/x"}],
                }
            )
            + "\n"
        )
        return 0

    out = args.output or "output-artifact.bin"
    out = out.replace("%(ext)s", "bin").replace("%(id)s", "mock")
    path = Path(out)
    if not path.name.startswith("output-artifact"):
        path = path.parent / f"output-artifact.{path.name}"
    mode = urlparse(url).path.rsplit("/", 1)[-1]
    if mode in {"oversize", "stdout", "blocking", "neighbor"}:
        _controlled_writer(path, mode=mode)
        return 0
    size = 4096
    if args.max_filesize:
        with contextlib.suppress(ValueError):
            size = min(size, int(args.max_filesize))
    path.write_bytes(b"M" * max(1, size))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
