"""Safe outbound HTTP client with manual redirects and re-validation."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from ipaddress import IPv4Address, IPv6Address
from urllib.parse import urljoin, urlsplit

import httpx

from fetchnow.core.config import Settings
from fetchnow.network.models import (
    HttpMethod,
    RedirectHop,
    SafeDocumentResponse,
    SafeDocumentTarget,
    SafeHTTPRequest,
    SafeHTTPResponse,
)
from fetchnow.url.destination import assert_addresses_allowed
from fetchnow.url.dns import DnsResolver, SystemDnsResolver
from fetchnow.url.errors import URLValidationError, raise_network_error
from fetchnow.url.models import URLValidationResult
from fetchnow.url.validate import URLValidator

logger = logging.getLogger("fetchnow.network.http")

DocumentRevalidator = Callable[[str], Awaitable[SafeDocumentTarget]]

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_GET_ACCEPT = (
    "text/html,application/xhtml+xml,application/json;q=0.9,text/plain;q=0.8,*/*;q=0.1"
)
_DOCUMENT_CONTENT_TYPES = frozenset(
    {"text/html", "application/xhtml+xml", "application/json"}
)

# Typed internal reasons (never sent to API clients).
INTERNAL_DNS_SET_MISMATCH = "DNS_SET_MISMATCH"
INTERNAL_DNS_EMPTY_SET = "DNS_EMPTY_SET"


def normalize_content_type(value: str | None) -> str | None:
    """Strip parameters (e.g. charset) and lowercase the MIME type."""
    if value is None:
        return None
    mime = value.split(";", 1)[0].strip().lower()
    return mime or None


def _addr_key_set(addresses: list[IPv4Address | IPv6Address]) -> frozenset[str]:
    return frozenset(str(addr) for addr in addresses)


class SafeHTTPClient:
    """Outbound HTTP probe client with fail-closed redirect handling.

    DNS strategy (best-effort TOCTOU mitigation without disabling TLS):
    1. Validate hostname via :class:`URLValidator` (provider allowlist + DNS).
    2. Immediately before each HTTP call, re-resolve twice (preflight snapshot
       and connect-time snapshot); require both sets public, non-empty, and
       with a non-empty intersection (CDN rotation tolerant).
    3. Perform the request with the normal hostname URL so TLS SNI / cert checks
       remain intact (no IP-literal pinning that would break certificate names).

    Residual risk: an attacker who flips DNS *after* the connect-time
    re-resolution and *during* TCP/TLS handshake is not fully eliminated.
    Intersection is not equivalent to IP pinning.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        validator: URLValidator,
        resolver: DnsResolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._validator = validator
        self._resolver = resolver or SystemDnsResolver(
            timeout_seconds=settings.dns_resolution_timeout_seconds
        )
        self._owns_client = client is None
        timeout = httpx.Timeout(
            connect=settings.outbound_connect_timeout_seconds,
            read=settings.outbound_read_timeout_seconds,
            write=settings.outbound_read_timeout_seconds,
            pool=settings.outbound_connect_timeout_seconds,
        )
        limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
        self._client = client or httpx.AsyncClient(
            follow_redirects=False,
            timeout=timeout,
            limits=limits,
            transport=transport,
            http2=False,
            trust_env=False,
        )
        # Defense in depth: httpx logs full URLs (with query) at INFO.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def probe(
        self, raw_url: str, *, method: HttpMethod = "HEAD"
    ) -> SafeHTTPResponse:
        """Validate ``raw_url`` and perform a bounded HEAD/GET probe."""
        started = time.perf_counter()
        hostname: str | None = None
        provider_id: str | None = None
        error_code: str | None = None
        redirect_count = 0
        body_bytes = 0
        method_used: str = method
        try:
            result = await asyncio.wait_for(
                self._probe_inner(raw_url, method=method),
                timeout=self._settings.outbound_total_timeout_seconds,
            )
        except TimeoutError:
            error_code = "SOURCE_TIMEOUT"
            self._log_probe(
                started,
                method=method_used,
                hostname=hostname,
                provider_id=provider_id,
                error_code=error_code,
                redirect_count=redirect_count,
                body_bytes=body_bytes,
            )
            raise_network_error("SOURCE_TIMEOUT")
        except URLValidationError as exc:
            error_code = exc.code
            self._log_probe(
                started,
                method=method_used,
                hostname=hostname,
                provider_id=provider_id,
                error_code=error_code,
                redirect_count=redirect_count,
                body_bytes=body_bytes,
                internal_reason=exc.internal_reason,
            )
            raise
        except Exception:
            error_code = "INTERNAL_ERROR"
            self._log_probe(
                started,
                method=method_used,
                hostname=hostname,
                provider_id=provider_id,
                error_code=error_code,
                redirect_count=redirect_count,
                body_bytes=body_bytes,
            )
            raise_network_error("INTERNAL_ERROR")
        else:
            self._log_probe(
                started,
                method=result.method_used,
                hostname=result.final_hostname,
                provider_id=result.provider_id,
                error_code=None,
                redirect_count=result.redirect_count,
                body_bytes=result.body_bytes_read,
            )
            return result

    async def fetch_document(
        self,
        validated: SafeDocumentTarget,
        *,
        allowed_hostnames: frozenset[str],
        revalidate: DocumentRevalidator,
    ) -> SafeDocumentResponse:
        """Bounded GET document fetch for wrapper resolvers.

        ``allowed_hostnames`` must be derived by orchestration from the active
        resolver's exact host set (never caller-supplied by the resolver). The
        initial hostname is checked against that set before any DNS lookup or
        HTTP request. Redirects are permitted only within the same set.
        """
        started = time.perf_counter()
        hostname: str | None = validated.hostname
        error_code: str | None = None
        redirect_count = 0
        body_bytes = 0
        try:
            result = await asyncio.wait_for(
                self._fetch_document_inner(
                    validated,
                    allowed_hostnames=allowed_hostnames,
                    revalidate=revalidate,
                ),
                timeout=self._settings.outbound_total_timeout_seconds,
            )
        except TimeoutError:
            error_code = "SOURCE_TIMEOUT"
            self._log_document(
                started,
                hostname=hostname,
                error_code=error_code,
                redirect_count=redirect_count,
                body_bytes=body_bytes,
            )
            raise_network_error("SOURCE_TIMEOUT")
        except URLValidationError as exc:
            error_code = exc.code
            self._log_document(
                started,
                hostname=hostname,
                error_code=error_code,
                redirect_count=redirect_count,
                body_bytes=body_bytes,
                internal_reason=exc.internal_reason,
            )
            raise
        except Exception:
            error_code = "INTERNAL_ERROR"
            self._log_document(
                started,
                hostname=hostname,
                error_code=error_code,
                redirect_count=redirect_count,
                body_bytes=body_bytes,
            )
            raise_network_error("INTERNAL_ERROR")
        else:
            self._log_document(
                started,
                hostname=result.final_hostname,
                error_code=None,
                redirect_count=result.redirect_count,
                body_bytes=result.body_bytes_read,
            )
            return result

    def _log_document(
        self,
        started: float,
        *,
        hostname: str | None,
        error_code: str | None,
        redirect_count: int,
        body_bytes: int,
        internal_reason: str | None = None,
    ) -> None:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        extra: dict[str, object] = {
            "outcome": "error" if error_code else "allow",
            "hostname": hostname,
            "error_code": error_code,
            "duration_ms": duration_ms,
            "redirect_count": redirect_count,
            "body_bytes_read": body_bytes,
            "http_method": "GET",
        }
        if internal_reason is not None:
            extra["internal_reason"] = internal_reason
        logger.info("outbound_document_fetch_complete", extra=extra)

    async def _fetch_document_inner(
        self,
        initial: SafeDocumentTarget,
        *,
        allowed_hostnames: frozenset[str],
        revalidate: DocumentRevalidator,
    ) -> SafeDocumentResponse:
        allowed = frozenset(h.lower().rstrip(".") for h in allowed_hostnames)
        if not allowed:
            raise_network_error("INVALID_REDIRECT", internal_reason="EMPTY_ALLOWLIST")

        initial_host = initial.hostname.lower().rstrip(".")
        # Fail closed before DNS or HTTP when the initial target is outside the
        # orchestration-derived host set (malicious resolver cannot pivot).
        if initial_host not in allowed:
            raise_network_error("INVALID_REDIRECT", internal_reason="HOST_NOT_ALLOWED")

        current = initial
        hops: list[RedirectHop] = []
        seen_urls: set[str] = set()
        max_redirects = min(self._settings.url_max_redirects, 20)

        while True:
            hop_host = current.hostname.lower().rstrip(".")
            if hop_host not in allowed:
                raise_network_error(
                    "INVALID_REDIRECT", internal_reason="HOST_NOT_ALLOWED"
                )

            request_url = current.request_url
            if request_url in seen_urls:
                raise_network_error("REDIRECT_LIMIT_EXCEEDED")
            seen_urls.add(request_url)

            await self._connect_time_dns_check(current.hostname)

            response = await self._send(SafeHTTPRequest(method="GET", url=request_url))
            try:
                status = response.status_code
                if status in _REDIRECT_STATUSES:
                    if len(hops) >= max_redirects:
                        raise_network_error("REDIRECT_LIMIT_EXCEEDED")
                    location = response.headers.get("location")
                    next_raw = self._resolve_redirect(
                        current_url=request_url,
                        location=location,
                        current_scheme=current.scheme,
                    )
                    # Reject out-of-scope redirect host before revalidation/DNS.
                    try:
                        next_host = urlsplit(next_raw).hostname
                    except ValueError:
                        raise_network_error("INVALID_REDIRECT")
                    if next_host is None:
                        raise_network_error("INVALID_REDIRECT")
                    if next_host.lower().rstrip(".") not in allowed:
                        raise_network_error(
                            "INVALID_REDIRECT", internal_reason="HOST_NOT_ALLOWED"
                        )
                    try:
                        nxt = await revalidate(next_raw)
                    except URLValidationError:
                        raise
                    except Exception:
                        # Revalidators may raise domain errors (e.g. wrapper
                        # safety). Treat as fail-closed invalid redirect without
                        # importing higher-level packages.
                        raise_network_error(
                            "INVALID_REDIRECT", internal_reason="REVALIDATE_FAILED"
                        )
                    if nxt.hostname.lower().rstrip(".") not in allowed:
                        raise_network_error(
                            "INVALID_REDIRECT", internal_reason="HOST_NOT_ALLOWED"
                        )
                    hops.append(
                        RedirectHop(
                            from_hostname=current.hostname,
                            to_hostname=nxt.hostname,
                            status_code=status,
                        )
                    )
                    logger.info(
                        "outbound_redirect_hop",
                        extra={
                            "hostname": current.hostname,
                            "redirect_count": len(hops),
                            "http_status": status,
                        },
                    )
                    current = nxt
                    continue

                content_type = normalize_content_type(
                    response.headers.get("content-type")
                )
                if content_type is None or content_type not in _DOCUMENT_CONTENT_TYPES:
                    raise_network_error("UNSUPPORTED_CONTENT_TYPE")
                if status < 200 or status >= 300:
                    raise_network_error("SOURCE_UNAVAILABLE")

                body = await self._read_document_body(response)
                return SafeDocumentResponse(
                    status_code=status,
                    content_type=content_type,
                    body=body,
                    body_bytes_read=len(body),
                    final_scheme=current.scheme,
                    final_hostname=current.hostname,
                    final_path=current.path,
                    final_canonical=current.canonical_without_query,
                    redirect_count=len(hops),
                    redirect_hops=tuple(hops),
                )
            finally:
                await response.aclose()

    def _log_probe(
        self,
        started: float,
        *,
        method: str,
        hostname: str | None,
        provider_id: str | None,
        error_code: str | None,
        redirect_count: int,
        body_bytes: int,
        internal_reason: str | None = None,
    ) -> None:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        extra: dict[str, object] = {
            "outcome": "error" if error_code else "allow",
            "provider_id": provider_id,
            "hostname": hostname,
            "error_code": error_code,
            "duration_ms": duration_ms,
            "redirect_count": redirect_count,
            "body_bytes_read": body_bytes,
            "http_method": method,
        }
        if internal_reason is not None:
            extra["internal_reason"] = internal_reason
        logger.info("outbound_probe_complete", extra=extra)

    async def _probe_inner(
        self,
        raw_url: str,
        *,
        method: HttpMethod,
    ) -> SafeHTTPResponse:
        initial = await self._validator.validate(raw_url)
        origin_provider = initial.provider_id
        # Fallback policy is frozen from the origin provider; redirects must
        # stay on the same provider and inherit this transport policy.
        head_fallback = initial.head_fallback_statuses
        current = initial
        hops: list[RedirectHop] = []
        active_method: HttpMethod = method
        # Track (method, url) so HEAD→GET fallback on the same URL is allowed,
        # while redirect loops still trip the limit.
        seen_attempts: set[tuple[str, str]] = set()
        max_redirects = min(self._settings.url_max_redirects, 20)

        while True:
            request_url = self._request_url(current)
            attempt_key = (active_method, request_url)
            if attempt_key in seen_attempts:
                raise_network_error("REDIRECT_LIMIT_EXCEEDED")
            seen_attempts.add(attempt_key)

            await self._connect_time_dns_check(current.url.hostname)

            response = await self._send(
                SafeHTTPRequest(method=active_method, url=request_url)
            )
            try:
                status = response.status_code
                if status in _REDIRECT_STATUSES:
                    if len(hops) >= max_redirects:
                        raise_network_error("REDIRECT_LIMIT_EXCEEDED")
                    location = response.headers.get("location")
                    next_raw = self._resolve_redirect(
                        current_url=request_url,
                        location=location,
                        current_scheme=current.url.scheme,
                    )
                    nxt = await self._validator.validate(next_raw)
                    if nxt.provider_id != origin_provider:
                        raise_network_error("UNSUPPORTED_PROVIDER")
                    hops.append(
                        RedirectHop(
                            from_hostname=current.url.hostname,
                            to_hostname=nxt.url.hostname,
                            status_code=status,
                        )
                    )
                    logger.info(
                        "outbound_redirect_hop",
                        extra={
                            "hostname": current.url.hostname,
                            "provider_id": origin_provider,
                            "redirect_count": len(hops),
                            "http_status": status,
                        },
                    )
                    current = nxt
                    # Keep active method across redirects, except 303 → GET.
                    if status == 303:
                        active_method = "GET"
                    continue

                if (
                    active_method == "HEAD"
                    and status in head_fallback
                    and status not in _REDIRECT_STATUSES
                ):
                    # Provider-declared transport fallback; 418 is not success.
                    active_method = "GET"
                    continue

                content_type = normalize_content_type(
                    response.headers.get("content-type")
                )
                content_length = self._parse_content_length(
                    response.headers.get("content-length")
                )
                body_bytes = 0
                if active_method == "GET":
                    # Enforce MIME from headers before streaming the body.
                    self._enforce_content_type(content_type, require_present=True)
                    if status >= 400:
                        raise_network_error("SOURCE_UNAVAILABLE")
                    body_bytes = await self._read_body_limited(response)
                else:
                    # HEAD: missing Content-Type is allowed; body is not read.
                    if content_type is not None:
                        self._enforce_content_type(content_type, require_present=False)
                    if status >= 400:
                        raise_network_error("SOURCE_UNAVAILABLE")

                if status < 200 or status >= 300:
                    raise_network_error("SOURCE_UNAVAILABLE")

                return SafeHTTPResponse(
                    status_code=status,
                    content_type=content_type,
                    content_length=content_length,
                    body_bytes_read=body_bytes,
                    final_scheme=current.url.scheme,
                    final_hostname=current.url.hostname,
                    final_path=current.url.path,
                    provider_id=current.provider_id,
                    provider_display_name=current.provider_display_name,
                    redirect_count=len(hops),
                    method_used=active_method,
                    redirect_hops=tuple(hops),
                )
            finally:
                await response.aclose()

    def _request_url(self, validated: URLValidationResult) -> str:
        # Include query for the actual fetch (needed for some providers) but
        # never log it. Canonical API responses still omit query.
        url = validated.url
        query = f"?{url.query}" if url.query else ""
        port = f":{url.port}" if url.port is not None else ""
        return f"{url.scheme}://{url.hostname}{port}{url.path}{query}"

    async def _connect_time_dns_check(self, hostname: str) -> None:
        preflight = await self._resolver.resolve(hostname)
        if not preflight:
            raise_network_error(
                "BLOCKED_DESTINATION", internal_reason=INTERNAL_DNS_EMPTY_SET
            )
        assert_addresses_allowed(preflight)
        connect_time = await self._resolver.resolve(hostname)
        if not connect_time:
            raise_network_error(
                "BLOCKED_DESTINATION", internal_reason=INTERNAL_DNS_EMPTY_SET
            )
        assert_addresses_allowed(connect_time)
        pre_set = _addr_key_set(preflight)
        connect_set = _addr_key_set(connect_time)
        if not (pre_set & connect_set):
            raise_network_error(
                "BLOCKED_DESTINATION", internal_reason=INTERNAL_DNS_SET_MISMATCH
            )

    async def _send(self, request: SafeHTTPRequest) -> httpx.Response:
        accept = (
            _GET_ACCEPT
            if request.method == "GET"
            else ", ".join(self._settings.outbound_allowed_content_types)
        )
        headers = {
            "User-Agent": self._settings.outbound_user_agent,
            "Accept": accept,
            "Accept-Language": "en",
        }
        try:
            return await self._client.send(
                self._client.build_request(
                    request.method,
                    request.url,
                    headers=headers,
                ),
                stream=True,
                follow_redirects=False,
            )
        except httpx.TimeoutException:
            raise_network_error("SOURCE_TIMEOUT")
        except httpx.InvalidURL:
            # httpx may raise while inspecting a malformed Location even when
            # follow_redirects=False.
            raise_network_error("INVALID_REDIRECT")
        except httpx.RemoteProtocolError as exc:
            # httpx may reject malformed Location before we can inspect it.
            if "location" in str(exc).lower():
                raise_network_error("INVALID_REDIRECT")
            raise_network_error("SOURCE_UNAVAILABLE")
        except httpx.ConnectError:
            raise_network_error("SOURCE_UNAVAILABLE")
        except httpx.HTTPError:
            raise_network_error("SOURCE_UNAVAILABLE")

    def _resolve_redirect(
        self,
        *,
        current_url: str,
        location: str | None,
        current_scheme: str,
    ) -> str:
        if location is None or not location.strip():
            raise_network_error("INVALID_REDIRECT")
        # Do not log Location (may contain query tokens).
        loc = location.strip()
        # Absolute-looking Location (scheme-like prefix) must be http(s)
        # with a non-empty authority; reject javascript:, :::garbage, etc.
        authority_candidate = loc.split("/", 1)[0]
        if ":" in authority_candidate:
            try:
                abs_parts = urlsplit(loc)
            except ValueError:
                raise_network_error("INVALID_REDIRECT")
            if abs_parts.scheme.lower() not in {"http", "https"}:
                raise_network_error("INVALID_REDIRECT")
            if not abs_parts.netloc:
                raise_network_error("INVALID_REDIRECT")
        try:
            joined = urljoin(current_url, loc)
            parts = urlsplit(joined)
        except ValueError:
            raise_network_error("INVALID_REDIRECT")
        if not parts.scheme or not parts.netloc:
            raise_network_error("INVALID_REDIRECT")
        if parts.username is not None or parts.password is not None:
            raise_network_error("INVALID_REDIRECT")
        if current_scheme == "https" and parts.scheme.lower() == "http":
            raise_network_error("INVALID_REDIRECT")
        if len(joined) > self._settings.url_max_length:
            raise_network_error("INVALID_REDIRECT")
        return joined

    def _enforce_content_type(
        self,
        content_type: str | None,
        *,
        require_present: bool,
    ) -> None:
        allowed = set(self._settings.outbound_allowed_content_types)
        if content_type is None:
            if require_present:
                raise_network_error("UNSUPPORTED_CONTENT_TYPE")
            return
        if content_type not in allowed:
            raise_network_error("UNSUPPORTED_CONTENT_TYPE")

    @staticmethod
    def _parse_content_length(raw: str | None) -> int | None:
        if raw is None or not raw.strip():
            return None
        try:
            value = int(raw.strip())
        except ValueError:
            return None
        return value if value >= 0 else None

    async def _read_body_limited(self, response: httpx.Response) -> int:
        """Stream body up to the probe soft limit; enforce hard max."""
        soft = self._settings.outbound_probe_body_bytes
        hard = self._settings.outbound_max_response_bytes
        stop_at = min(soft, hard)
        # Bound chunk size so a single read cannot blow past the soft probe cap
        # by hundreds of KiB when the peer sends a large TLS record.
        chunk_size = min(16_384, max(stop_at, 1))
        total = 0
        try:
            async for chunk in response.aiter_bytes(chunk_size):
                total += len(chunk)
                if total > hard:
                    raise_network_error("RESPONSE_TOO_LARGE")
                if total < stop_at:
                    continue
                # Reached soft probe cap.
                if soft < hard:
                    # Typical production: stop early; remaining body is unread.
                    break
                # soft == hard: reaching the ceiling with more bytes left is
                # oversized (keeps RESPONSE_TOO_LARGE tests meaningful).
                async for extra in response.aiter_bytes(1):
                    if extra:
                        raise_network_error("RESPONSE_TOO_LARGE")
                    break
                break
        except URLValidationError:
            raise
        except httpx.TimeoutException:
            raise_network_error("SOURCE_TIMEOUT")
        except httpx.HTTPError:
            raise_network_error("SOURCE_UNAVAILABLE")
        return total

    async def _read_document_body(self, response: httpx.Response) -> bytes:
        """Read a full document body up to the hard outbound byte ceiling.

        The ceiling applies to bytes yielded by ``aiter_bytes`` after httpx
        content decoding (e.g. gzip decompression), i.e. the payload a
        resolver would observe — not the on-wire compressed size alone.
        ``Content-Length`` is only an early reject when the declared size
        already exceeds the hard cap.
        """
        hard = self._settings.outbound_max_response_bytes
        content_length = self._parse_content_length(
            response.headers.get("content-length")
        )
        if content_length is not None and content_length > hard:
            raise_network_error("RESPONSE_TOO_LARGE")
        chunk_size = min(16_384, hard)
        chunks: list[bytes] = []
        total = 0
        try:
            async for chunk in response.aiter_bytes(chunk_size):
                total += len(chunk)
                if total > hard:
                    raise_network_error("RESPONSE_TOO_LARGE")
                chunks.append(chunk)
        except URLValidationError:
            raise
        except httpx.TimeoutException:
            raise_network_error("SOURCE_TIMEOUT")
        except httpx.HTTPError:
            raise_network_error("SOURCE_UNAVAILABLE")
        return b"".join(chunks)
