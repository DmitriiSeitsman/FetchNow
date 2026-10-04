"""Versioned, bounded UDS messages. The client cannot send argv or paths."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass

from fetchnow.media_executor.constants import (
    MAX_REQUEST_BYTES,
    OPS_DEFERRED,
    OPS_OFFLINE,
    PROTOCOL_VERSION,
)

_UUID = re.compile(r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_CONTAINERS = frozenset({"mp4", "webm"})


@dataclass(frozen=True, slots=True)
class Request:
    op: str
    job_id: str
    attempt: int
    fence: int
    container: str | None = None

    @property
    def key(self) -> tuple[str, int, int]:
        return (self.job_id, self.attempt, self.fence)


class ProtocolError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def parse_request(raw: bytes) -> Request:
    if len(raw) > MAX_REQUEST_BYTES:
        raise ProtocolError("oversized")
    if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        raise ProtocolError("malformed")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("malformed") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("malformed")
    allowed = {"v", "op", "job_id", "attempt", "fence", "container"}
    if set(payload) - allowed:
        raise ProtocolError("malformed")
    if type(payload.get("v")) is not int or payload["v"] != PROTOCOL_VERSION:
        raise ProtocolError("malformed")
    op = payload.get("op")
    if not isinstance(op, str) or op not in OPS_OFFLINE | OPS_DEFERRED:
        raise ProtocolError("unknown_operation")
    job_id = payload.get("job_id")
    if not isinstance(job_id, str) or _UUID.fullmatch(job_id) is None:
        raise ProtocolError("malformed")
    try:
        uuid.UUID(job_id)
    except ValueError as exc:
        raise ProtocolError("malformed") from exc
    attempt = payload.get("attempt")
    fence = payload.get("fence")
    if type(attempt) is not int or not 1 <= attempt <= 1000:
        raise ProtocolError("malformed")
    if type(fence) is not int or not 1 <= fence <= 1_000_000_000:
        raise ProtocolError("malformed")
    container = payload.get("container")
    if op == "mux_copy":
        if not isinstance(container, str) or container not in _CONTAINERS:
            raise ProtocolError("malformed")
    elif container is not None:
        raise ProtocolError("malformed")
    if op in OPS_DEFERRED:
        raise ProtocolError("network_not_in_sec08")
    return Request(
        op=op,
        job_id=job_id,
        attempt=attempt,
        fence=fence,
        container=container if isinstance(container, str) else None,
    )


def dump_response(payload: dict[str, object]) -> bytes:
    body = dict(payload)
    body["v"] = PROTOCOL_VERSION
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    framed = raw + b"\n"
    if len(framed) > 400_000:
        raise ProtocolError("oversized")
    return framed
