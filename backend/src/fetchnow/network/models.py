"""Safe outbound HTTP models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

HttpMethod = Literal["HEAD", "GET"]


@dataclass(frozen=True, slots=True, repr=False)
class SafeDocumentTarget:
    """Safety-validated HTTPS document target (network-layer contract).

    Lives in the network package so ``SafeHTTPClient`` does not depend on the
    higher-level resolution domain. Query and request URL are excluded from
    ``repr``.
    """

    scheme: str
    hostname: str
    port: int | None
    path: str
    query: str = field(repr=False)
    canonical_without_query: str
    request_url: str = field(repr=False)

    def __repr__(self) -> str:
        return (
            "SafeDocumentTarget("
            f"scheme={self.scheme!r}, "
            f"hostname={self.hostname!r}, "
            f"canonical_without_query={self.canonical_without_query!r})"
        )


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


@dataclass(frozen=True, slots=True, repr=False)
class SafeDocumentResponse:
    """Bounded wrapper-document response (body retained for extraction only)."""

    status_code: int
    content_type: str
    body: bytes = field(repr=False)
    body_bytes_read: int
    final_scheme: str
    final_hostname: str
    final_path: str
    final_canonical: str
    redirect_count: int
    redirect_hops: tuple[RedirectHop, ...] = field(default_factory=tuple)

    def __repr__(self) -> str:
        return (
            "SafeDocumentResponse("
            f"status_code={self.status_code!r}, "
            f"content_type={self.content_type!r}, "
            f"body_bytes_read={self.body_bytes_read!r}, "
            f"final_canonical={self.final_canonical!r}, "
            f"redirect_count={self.redirect_count!r})"
        )
