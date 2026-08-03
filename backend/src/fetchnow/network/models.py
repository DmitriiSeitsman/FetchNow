"""Safe outbound HTTP models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

HttpMethod = Literal["HEAD", "GET"]


@dataclass(frozen=True, slots=True)
class SafeHTTPRequest:
    """Server-controlled outbound request (no user headers/body)."""

    method: HttpMethod
    url: str


@dataclass(frozen=True, slots=True)
class RedirectHop:
    """Safe audit record for one redirect hop (no query, no IPs)."""

    from_hostname: str
    to_hostname: str
    status_code: int


@dataclass(frozen=True, slots=True)
class SafeHTTPResponse:
    """Bounded probe response metadata."""

    status_code: int
    content_type: str | None
    content_length: int | None
    body_bytes_read: int
    final_scheme: str
    final_hostname: str
    final_path: str
    provider_id: str
    provider_display_name: str
    redirect_count: int
    method_used: HttpMethod
    redirect_hops: tuple[RedirectHop, ...] = field(default_factory=tuple)
