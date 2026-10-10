#!/usr/bin/env python3
"""Tiny TCP origin for CONNECT tunnels: replies PONG to PING."""

from __future__ import annotations

import os
import socket
import threading


def _serve(conn: socket.socket) -> None:
    try:
        data = conn.recv(64)
        if b"PING" in data:
            conn.sendall(b"PONG\n")
    finally:
        conn.close()


def main() -> None:
    host = os.environ.get("MOCK_BIND_HOST", "0.0.0.0")
    port = int(os.environ.get("MOCK_BIND_PORT", "443"))
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(32)
    while True:
        conn, _addr = sock.accept()
        threading.Thread(target=_serve, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
