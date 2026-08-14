# ADR 0005: Safe outbound HTTP and redirects

Status: Accepted (PR2) · Amended for HEAD fallback + DNS intersection  
Date: 2026-08-03

## Context

PR1 validates user URLs (syntax, provider allowlist, DNS destination classification) but does not fetch. FetchNow still needs a bounded outbound HTTP probe for diagnostics and future preflight. Automatic redirect following, unlimited bodies, and trusting a single preflight DNS lookup would reopen SSRF and resource-exhaustion paths.

Live observation (PR2 follow-up): some providers reject HEAD with transport-specific statuses (VK returns HTTP **418** from datacenter clients). Treating every non-2xx HEAD as terminal made the diagnostic probe unusable without weakening SSRF controls.

## Decision

Introduce `backend/src/fetchnow/network/` — a FastAPI-independent safe outbound layer built on `httpx.AsyncClient` with:

1. **Manual redirect handling** (`follow_redirects=False`). Only 301/302/303/307/308 are followed.
2. **Per-hop re-validation** using the same PR1 pipeline: normalize → provider registry → DNS → IP classification. Same-provider policy only; cross-provider redirects are denied unless a future explicit policy says otherwise.
3. **No HTTPS→HTTP downgrade** by default (`INVALID_REDIRECT`).
4. **Response size enforcement** via streaming reads:
   - soft probe cap `OUTBOUND_PROBE_BODY_BYTES` (stop once enough diagnostic bytes are read; do not keep full HTML);
   - hard cap `OUTBOUND_MAX_RESPONSE_BYTES` (must be ≥ probe soft cap; overflow → `RESPONSE_TOO_LARGE`).
5. **Content-Type policy** from Settings (`OUTBOUND_ALLOWED_CONTENT_TYPES`). MIME is normalized without charset. For limited GET, missing Content-Type is **fail-closed** (`UNSUPPORTED_CONTENT_TYPE`). HEAD may omit Content-Type (body is not trusted/read).
6. **Server-controlled request policy**: only HEAD and limited GET; fixed `User-Agent` / `Accept` / `Accept-Language`; no cookies, Authorization, Referer, Range, or user headers. GET uses an HTML-friendly Accept string without browser fingerprint headers.
7. **Provider-specific HEAD→GET fallback** (transport compatibility, not bot-protection bypass):
   - Declared on `ProviderDescriptor.head_fallback_statuses`.
   - VK: `{418, 405, 501}` (418 confirmed live).
   - Rutube: `{405, 501}` only (live real pages return HEAD 200; do not treat 404 as fallback).
   - HTTP 418 is **not** a successful probe result; it only authorizes a bounded GET retry.
   - Fallback GET uses the same redirect engine and validation pipeline.
8. **Connection / DNS strategy (best-effort TOCTOU)**: before each hop’s HTTP call, resolve twice; both sets must be non-empty and fully public; require a **non-empty intersection**. Reject on empty intersection (`BLOCKED_DESTINATION` + internal reason `DNS_SET_MISMATCH`). Mixed public/private remains denied. Hostname URL preserved for TLS SNI/certs (verification stays on). Full IP pinning is not implemented.
9. **Client lifecycle**: one pooled `SafeHTTPClient` per API process; injectable transport/client for tests. Tests must not use the public internet.

## DNS intersection rationale

Strict equality of successive DNS answer sets caused false positives under CDN rotation (public A/AAAA sets that legitimately change between lookups while remaining public). Intersection still rejects a full flip to an unrelated address set (classic rebinding pattern) better than “any public sets”. It is **not** equivalent to connecting to a pinned validated IP.

## Residual DNS TOCTOU

An attacker who changes DNS answers *after* the connect-time re-resolution and *during* TCP/TLS handshake is not fully eliminated. We do **not** claim DNS rebinding is completely solved. We deliberately do **not** disable TLS verification to pin IP literals.

## Why this layer is not for media downloads

Bodies are soft-capped for probes, content types are probe-oriented, and methods exclude media Range/POST flows. Large media retrieval remains out of scope until a dedicated, quota-aware pipeline (and possibly yt-dlp) lands. HEAD fallback does not impersonate browsers or bypass auth walls.

## VK hostname note

Explicit VK video frontends `vkvideo.ru` / `www.vkvideo.ru` / `m.vkvideo.ru` and
official `vk.ru` / `www.vk.ru` / `m.vk.ru` aliases are included in the VK
allowlist so same-provider redirects from `vk.com` remain valid. Auth hosts such
as `login.vk.ru` stay denylisted via unsupported-provider.

## Consequences

- Diagnostic API: `POST /api/v1/media/probe` returns safe metadata only (no query, IPs, body, or full headers), including `http.methodUsed` (`HEAD`|`GET`).
- Security fixtures promote former `redirect_future` cases to `stage=network` (≥50 total cases).
- Logging silences httpx/httpcore INFO and emits only allowlisted FetchNow fields (optional `internal_reason`, never raw URLs/IPs).

## Related

- [ADR 0004](0004-provider-registry-and-dns-validation.md)
- [URL validation policy](../security/url-validation-policy.md)
- [Threat model](../security/threat-model.md)
- [Error codes](../api/error-codes.md)
