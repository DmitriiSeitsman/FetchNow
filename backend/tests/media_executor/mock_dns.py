#!/usr/bin/env python3
"""Minimal DNS: rebind.test → 8.8.8.8 + 10.0.0.1; mock-origin → configured A."""

from __future__ import annotations

import os
import socket
import struct


def _pack_name(name: str) -> bytes:
    out = bytearray()
    for label in name.rstrip(".").split("."):
        out.append(len(label))
        out.extend(label.encode("ascii"))
    out.append(0)
    return bytes(out)


def _read_name(data: bytes, offset: int) -> tuple[str, int]:
    labels: list[str] = []
    while True:
        length = data[offset]
        if length == 0:
            return ".".join(labels).lower(), offset + 1
        if length & 0xC0 == 0xC0:
            ptr = struct.unpack("!H", data[offset : offset + 2])[0] & 0x3FFF
            name, _ = _read_name(data, ptr)
            if labels:
                return ".".join(labels + name.split(".")) if name else ".".join(labels), offset + 2
            return name, offset + 2
        offset += 1
        labels.append(data[offset : offset + length].decode("ascii", "ignore"))
        offset += length


def _answer(query: bytes, addresses: list[str]) -> bytes:
    txid = query[:2]
    flags = b"\x81\x80"
    counts = struct.pack("!HHHH", 1, len(addresses), 0, 0)
    # copy question
    qname, pos = _read_name(query, 12)
    question = query[12:pos + 4]
    answers = bytearray()
    for ip in addresses:
        answers.extend(b"\xc0\x0c")  # pointer to question name
        answers.extend(struct.pack("!HHIH", 1, 1, 30, 4))
        answers.extend(socket.inet_aton(ip))
    return txid + flags + counts + question + bytes(answers)


def main() -> None:
    origin_ip = os.environ.get("MOCK_ORIGIN_IP", "1.2.3.4")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 53))
    while True:
        data, addr = sock.recvfrom(512)
        try:
            name, _ = _read_name(data, 12)
        except Exception:
            continue
        if name.rstrip(".") == "rebind.test":
            ips = ["8.8.8.8", "10.0.0.1"]
        elif name.rstrip(".") in {"mock-origin", "mock-origin.fetchnow"}:
            ips = [origin_ip]
        else:
            ips = []
        if not ips:
            # NXDOMAIN-ish empty answer
            resp = data[:2] + b"\x81\x83" + data[4:12] + data[12:]
            sock.sendto(resp, addr)
            continue
        sock.sendto(_answer(data, ips), addr)


if __name__ == "__main__":
    main()
