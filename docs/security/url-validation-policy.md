# URL validation policy

Status: Accepted (PR0B spec) · Parse/registry/DNS in PR1 · Outbound probe + redirects in PR2

Executable code:

- `backend/src/fetchnow/url/` — parse, provider registry, DNS, destination classification
- `backend/src/fetchnow/network/` — safe outbound HTTP with manual redirects
- `backend/src/fetchnow/resolution/` — wrapper resolution foundation (PR3A; no production wrappers)

Conformance vectors: `backend/tests/fixtures/url_security_cases.json`
(`stage=validate` and `stage=network`; no public internet in tests).

## Goals

Treat every user-supplied URL as hostile. Before any outbound connect:

1. Parse and normalize strictly.
2. Enforce scheme, port, credentials, and provider allowlist rules.
3. Resolve DNS and classify **every** returned address.
4. Re-validate after redirects, hop by hop (PR2).

Fail closed on ambiguity.

## Requirements

### Scheme and credentials

- Allow only `http` and `https`.
- Reject `file://`, `ftp://`, `data:`, `javascript:`, and scheme-relative URLs (`//evil.example/...`).
- Reject URLs with embedded credentials (`https://user:pass@host/`).

### Hostname normalization

- Lowercase ASCII labels after IDNA/punycode conversion.
- Apply IDNA (ToASCII) for Unicode hostnames; compare allowlist against ASCII form.
- Reject empty host, invalid host syntax, and percent-encoded hostname tricks that alter authority after decoding.

### Destination blocking (name or resolved address)

Deny destinations that are or resolve to:

- localhost / loopback names and addresses (`127.0.0.0/8`, `::1`, IPv4-mapped loopback);
- RFC1918 private IPv4;
- carrier-grade NAT (`100.64.0.0/10`);
- link-local (`169.254.0.0/16`, `fe80::/10`);
- multicast;
- unspecified (`0.0.0.0`, `::`);
- unique local IPv6 (`fc00::/7`) and other non-global ranges used internally;
- cloud metadata addresses (at minimum `169.254.169.254` and documented equivalents);
- IPv4-mapped IPv6 forms of the above.

Checks apply to **both** IPv4 and IPv6.

### DNS and redirects (PR2)

- After DNS resolution, re-run address classification on **all** answers. If any answer is blocked, deny.
- Follow redirects **manually** only (`follow_redirects=False`); statuses 301/302/303/307/308.
- Cap hops with `URL_MAX_REDIRECTS` (hard ceiling 20 in Settings).
- On each hop: normalize → provider registry → DNS → IP classification → connect-time DNS re-check.
- Do not trust “same hostname” redirects without re-validation.
- Do not follow redirects to unsupported providers (same-provider policy in PR2).
- Reject HTTPS→HTTP downgrade (`INVALID_REDIRECT`).
- Do not return full Location/query or resolved IPs to API clients.
- Connect-time strategy: re-resolve immediately before each request; both answer sets must be public and non-empty with a non-empty **intersection** (CDN rotation tolerant; not IP pinning). Keep hostname for TLS SNI/certs (no TLS verification disable). Residual TOCTOU documented in ADR 0005.
- Provider HEAD→GET fallback statuses live on the provider descriptor (VK: 418/405/501; Rutube: 405/501). Fallback is transport compatibility only; 418 is never itself a successful probe.

### Ports

- Default allow only `80` and `443` (or empty default ports for http/https).
- Non-standard ports are denied unless a future explicit provider exception is documented and reviewed.

### Provider hostname allowlist

- Only explicitly allowlisted hostnames (and explicitly listed subdomains) are accepted.
- Matching must be **label-boundary safe** (no naive `endswith`).

### Probe limits (PR2)

For diagnostic/preflight HTTP (`SafeHTTPClient` / `POST /api/v1/media/probe`):

- timeouts: `OUTBOUND_CONNECT_TIMEOUT_SECONDS`, `OUTBOUND_READ_TIMEOUT_SECONDS`, `OUTBOUND_TOTAL_TIMEOUT_SECONDS`;
- soft body read: `OUTBOUND_PROBE_BODY_BYTES` (stop after enough diagnostic bytes);
- hard body cap: `OUTBOUND_MAX_RESPONSE_BYTES` (fail on overflow; must be ≥ probe soft cap);
- content types: `OUTBOUND_ALLOWED_CONTENT_TYPES` (GET requires Content-Type; missing → deny);
- methods: HEAD and limited GET only (`http.methodUsed` reports which succeeded);
- User-Agent: `OUTBOUND_USER_AGENT`.

This probe is **not** media metadata extraction and must not download full media files.

## Error mapping (public)

Prefer stable codes from `docs/api/error-codes.md`:

- bad scheme → `UNSUPPORTED_SCHEME`
- parse/credential/port issues → `INVALID_URL`
- host not allowlisted → `UNSUPPORTED_PROVIDER`
- private/metadata/localhost → `BLOCKED_DESTINATION`
- too many redirects / loops → `REDIRECT_LIMIT_EXCEEDED`
- bad/missing Location, HTTPS downgrade → `INVALID_REDIRECT`
- oversized body → `RESPONSE_TOO_LARGE`
- disallowed/missing MIME on GET → `UNSUPPORTED_CONTENT_TYPE`
- connect/TLS failures → `SOURCE_UNAVAILABLE`
- timeouts → `SOURCE_TIMEOUT`

Do not return raw resolver internals, exception strings, or internal IPs to clients.

## Wrapper resolution foundation (PR3A)

Domain-only foundation (`fetchnow.resolution`, ADR 0006) plus the PR3B Yandex
Video Preview resolver (ADR 0007): exact
`https://yandex.ru/video/preview/<digits>` only; orchestration-owned document
allowlists (**Yandex document host only** — iframe `yastatic.net` is parsed
locally and never fetched); preview-page binding via `data-state` `location`
and/or matching viewer id; extraction from `dups[id].player.videoUrl` or
trusted VK player iframe fragment `counters.videoUrl`; never `embedUrl` /
`/video_ext.php` / page-wide URL harvest; optional local HTTP→HTTPS string
upgrade for stable provider identities (no HTTP fetch); terminal candidates
must pass the existing ProviderRegistry. **Yandex is not a media provider.**
Public wiring is `POST /api/v1/media/resolve`. `/media/validate` and
`/media/probe` are unchanged.

## Test vectors

`backend/tests/fixtures/url_security_cases.json` — PR1/PR2 execute `validate` and `network` stages with fake DNS and `httpx.MockTransport`.

## Related documents

- [Threat model](threat-model.md)
- [Error codes](../api/error-codes.md)
- [ADR 0004](../adr/0004-provider-registry-and-dns-validation.md)
- [ADR 0005](../adr/0005-safe-outbound-http-and-redirects.md)
- [ADR 0006](../adr/0006-wrapper-resolution-foundation.md)
