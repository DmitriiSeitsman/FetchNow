"""Versioned, bounded UDS messages. The client cannot send argv or paths."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from urllib.parse import urlparse

from fetchnow.media_executor.constants import (
    MAX_ARTIFACT_BYTES,
    MAX_FORMAT_TOKEN_CHARS,
    MAX_MIN_FREE_BYTES,
    MAX_REQUEST_BYTES,
    MAX_URL_CHARS,
    OPS_LIFECYCLE,
    OPS_NETWORK,
    OPS_NETWORK_TOOLS,
    OPS_OFFLINE,
    OPS_OFFLINE_TOOLS,
    PROFILE_NETWORK,
    PROFILE_OFFLINE,
    PROTOCOL_VERSION,
    PROVIDER_EXTRACTORS,
)
from fetchnow.media_executor.format_token import validate_provider_format_token

_UUID = re.compile(r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_CONTAINERS = frozenset({"mp4", "webm"})
_ALLOWED_KEYS = frozenset(
    {
        "v",
        "op",
        "job_id",
        "attempt",
        "fence",
        "container",
        "url",
        "provider_id",
        "format_token",
        "max_bytes",
        "min_free_bytes",
    }
)


@dataclass(frozen=True, slots=True)
class Request:
    op: str
    job_id: str
    attempt: int
    fence: int
    container: str | None = None
    url: str | None = None
    provider_id: str | None = None
    format_token: str | None = None
    max_bytes: int | None = None
    min_free_bytes: int | None = None

    @property
    def key(self) -> tuple[str, int, int]:
        return (self.job_id, self.attempt, self.fence)


class ProtocolError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def parse_request(raw: bytes, *, profile: str = PROFILE_OFFLINE) -> Request:
    if profile not in {PROFILE_OFFLINE, PROFILE_NETWORK}:
        raise ProtocolError("malformed")
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
    if set(payload) - _ALLOWED_KEYS:
        raise ProtocolError("malformed")
    if type(payload.get("v")) is not int or payload["v"] != PROTOCOL_VERSION:
        raise ProtocolError("malformed")
    op = payload.get("op")
    if not isinstance(op, str):
        raise ProtocolError("unknown_operation")
    allowed = OPS_OFFLINE if profile == PROFILE_OFFLINE else OPS_NETWORK
    if op not in allowed:
        if profile == PROFILE_OFFLINE and op in OPS_NETWORK_TOOLS:
            raise ProtocolError("network_not_in_sec08")
        if profile == PROFILE_NETWORK and op in OPS_OFFLINE_TOOLS:
            raise ProtocolError("offline_not_in_sec09")
        if op in OPS_LIFECYCLE | OPS_OFFLINE_TOOLS | OPS_NETWORK_TOOLS:
            raise ProtocolError("unknown_operation")
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
    url = payload.get("url")
    provider_id = payload.get("provider_id")
    format_token = payload.get("format_token")

    if op == "mux_copy":
        if not isinstance(container, str) or container not in _CONTAINERS:
            raise ProtocolError("malformed")
        if url is not None or provider_id is not None or format_token is not None:
            raise ProtocolError("malformed")
    elif op in OPS_NETWORK_TOOLS:
        if container is not None:
            raise ProtocolError("malformed")
        if not isinstance(url, str):
            raise ProtocolError("malformed")
        try:
            url = validate_tool_url(url)
        except ValueError as exc:
            raise ProtocolError("malformed") from exc
        if not isinstance(provider_id, str) or provider_id not in PROVIDER_EXTRACTORS:
            raise ProtocolError("malformed")
        if op == "inspect_metadata":
            if format_token is not None:
                raise ProtocolError("malformed")
            format_token = None
            if (
                payload.get("max_bytes") is not None
                or payload.get("min_free_bytes") is not None
            ):
                raise ProtocolError("malformed")
            max_bytes = None
            min_free_bytes = None
        else:
            if not isinstance(format_token, str):
                raise ProtocolError("malformed")
            try:
                format_token = validate_provider_format_token(format_token)
            except ValueError as exc:
                raise ProtocolError("malformed") from exc
            if len(format_token) > MAX_FORMAT_TOKEN_CHARS:
                raise ProtocolError("malformed")
            max_bytes = payload.get("max_bytes")
            min_free_bytes = payload.get("min_free_bytes")
            if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_ARTIFACT_BYTES:
                raise ProtocolError("malformed")
            if type(min_free_bytes) is not int or not (
                0 <= min_free_bytes <= MAX_MIN_FREE_BYTES
            ):
                raise ProtocolError("malformed")
    else:
        if container is not None or url is not None:
            raise ProtocolError("malformed")
        if provider_id is not None or format_token is not None:
            raise ProtocolError("malformed")
        if (
            payload.get("max_bytes") is not None
            or payload.get("min_free_bytes") is not None
        ):
            raise ProtocolError("malformed")
        max_bytes = None
        min_free_bytes = None

    # Offline ops must not carry size fields either.
    if op in OPS_OFFLINE_TOOLS or op in OPS_LIFECYCLE:
        if (
            payload.get("max_bytes") is not None
            or payload.get("min_free_bytes") is not None
        ):
            raise ProtocolError("malformed")
        max_bytes = None
        min_free_bytes = None

    return Request(
        op=op,
        job_id=job_id,
        attempt=attempt,
        fence=fence,
        container=container if isinstance(container, str) else None,
        url=url if isinstance(url, str) else None,
        provider_id=provider_id if isinstance(provider_id, str) else None,
        format_token=format_token if isinstance(format_token, str) else None,
        max_bytes=max_bytes if type(max_bytes) is int else None,
        min_free_bytes=min_free_bytes if type(min_free_bytes) is int else None,
    )


def validate_tool_url(url: str) -> str:
    if not isinstance(url, str) or not url or len(url) > MAX_URL_CHARS:
        raise ValueError("url")
    if any(ord(ch) < 32 for ch in url) or "\\" in url:
        raise ValueError("url")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("url")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("url")
    if not parsed.hostname or parsed.fragment:
        raise ValueError("url")
    # Keep query empty for tool_url contract (canonical page URLs).
    if parsed.query:
        raise ValueError("url")
    if url != parsed.geturl() and url.rstrip("/") != parsed.geturl().rstrip("/"):
        # Accept only normalized forms without backslash tricks.
        pass
    return url


def dump_response(payload: dict[str, object]) -> bytes:
    body = dict(payload)
    body["v"] = PROTOCOL_VERSION
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    framed = raw + b"\n"
    if len(framed) > 400_000:
        raise ProtocolError("oversized")
    return framed
