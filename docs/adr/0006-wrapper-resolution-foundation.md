# ADR 0006 — Wrapper resolution foundation

Status: Accepted for PR3A (foundation only; no production wrapper)

## Context

FetchNow already validates direct media-provider URLs (VK, Rutube) and probes
them with a fail-closed outbound HTTP client. Some user-submitted links will
arrive as **wrapper** pages that embed or redirect toward a supported provider
URL. PR3A introduces a reusable resolution foundation without shipping any
production wrapper (including any Yandex-specific logic).

## Decision

### Provider vs wrapper

| Concern | Owner |
| --- | --- |
| Terminal media identity | `ProviderRegistry` + `URLValidator` |
| Intermediate page → candidate URL | `WrapperResolver` + `WrapperResolverRegistry` |
| Recursion / depth / loops | `ResolutionService` only |
| Document-fetch host allowlist | Orchestration (`ScopedDocumentFetchPort`) |

Wrappers are **not** `ProviderDescriptor`s. They must never enter the provider
allowlist used by `/media/validate` and `/media/probe`.

### Pipeline order

1. Syntactic normalize (no DNS).
2. Block reserved / literal destinations before classification.
3. If hostname is an enabled direct provider → full `URLValidator` → direct
   `ResolutionResult` (empty chain). Wrapper registry is not consulted.
4. Else exact wrapper registry match; if none → `wrapper_unsupported` (no DNS).
5. Wrapper HTTPS-only safety validation + public DNS.
6. Orchestration builds a **resolver-scoped** `DocumentFetchPort` whose allowed
   hosts are taken from the active immutable registration snapshot (never from
   live resolver properties or resolver method arguments).
7. Resolver returns a candidate (optionally via the scoped document port).
8. Candidate is reclassified (provider / nested wrapper / unsupported).

### Resolver-scoped document fetch

- `DocumentFetchPort.fetch_document(validated)` has **no** `allowed_hostnames`
  parameter. Resolvers cannot widen the host set.
- Authoritative host ownership is the immutable
  `WrapperResolverRegistration.exact_hostnames` snapshot captured once when the
  registry is built. After registration, live `resolver.exact_hostnames` is
  never consulted for security policy.
- `SafeDocumentTarget` lives in `fetchnow.network.models` so the network layer
  does not depend on the resolution package.
- `SafeHTTPClient.fetch_document` still accepts an allowlist, but only the
  orchestration adapter supplies it from the registration snapshot.
- Before the first DNS lookup and first HTTP request, the initial hostname must
  be in that server-derived set; empty sets fail closed.
- Each redirect is revalidated under the wrapper HTTPS-only policy and must
  remain inside the same active-registration host set (checked before the next
  request). Cross-wrapper / cross-provider redirects are not automatic
  resolution success.
- Document body caps apply to bytes observed after content decoding (including
  gzip decompression), i.e. the payload a resolver would read.

### HTTPS-only wrapper policy

Direct provider validation keeps configured `http,https`. The wrapper layer
separately requires:

- `https` only;
- default port only (no explicit non-default ports);
- no credentials;
- no HTTPS→HTTP redirects;
- no HTTP candidates from resolvers.

### Chain, depth, loops

- `WRAPPER_RESOLUTION_MAX_DEPTH` (default `3`, hard max `8`) counts wrapper
  transitions.
- Direct providers use depth `0`.
- Terminal provider hops do not consume wrapper depth.
- Loop detection uses a process-local SHA-256 fingerprint of the full effective
  URL (scheme, host, effective port, path, internal query). Fingerprints and
  query strings are never logged or placed in public chain hops.

### Privacy / redaction

- `ResolutionError.__repr__` never includes `internal_reason`.
- `internal_reason` is restricted to short uppercase diagnostic codes;
  arbitrary resolver text is replaced with `REDACTED_INTERNAL_REASON`.
- Public messages always come from a fixed catalog (resolver-supplied messages
  are ignored).
- Hop metadata allows only bounded `bool`/`int` scalars; strings are rejected.
- Metadata is stored as a sorted `tuple` of `(key, value)` pairs for
  deterministic order across process hash seeds.
- `ResolutionHop.__repr__` omits metadata entirely.
- Audit identifiers (`resolver_id`, `wrapper_type`, `strategy`) must be
  lowercase token-shaped values when recorded on a hop; ID/type values used in
  hops come from the registration snapshot.

### Registry immutability

`WrapperResolverRegistry` is fully immutable after construction:

- build only via `from_resolvers()` / `empty()` (public constructor raises);
- attribute reassignment and deletion are rejected;
- host index is a `MappingProxyType`;
- `registrations` is a tuple of frozen `WrapperResolverRegistration` snapshots;
- input lists cannot mutate the built registry.

Direct construction cannot bypass duplicate / overlap checks.

### Errors

Domain kinds are lower-case (`wrapper_unsupported`, `wrapper_unresolved`,
`resolved_provider_unsupported`, `resolution_loop`, `resolution_limit_exceeded`,
`unsafe_resolution_target`). They are **not** public API codes in PR3A.
`/media/validate` and `/media/probe` keep existing uppercase codes. API mapping
lands when the first production resolver is wired.

### Explicit non-goals (PR3A)

- No production wrapper resolver and no Yandex-specific code.
- No public resolution endpoint and no behavior change to validate/probe.
- No cache (no proven load, no TTL/privacy policy for query-bearing keys).
- No subprocess, yt-dlp, Playwright, downloads, media metadata extraction,
  tickets, or DB changes.

## Consequences

- PR3B (or later) can add the first production resolver and optional API wiring.
- Future cache, if any, requires a separate bounded design (entry/byte caps, TTL,
  key canonicalization, no secret-query retention, negative-cache policy).

## Related documents

- [ADR 0004 — provider registry + DNS](0004-provider-registry-and-dns-validation.md)
- [ADR 0005 — safe outbound HTTP](0005-safe-outbound-http-and-redirects.md)
- [URL validation policy](../security/url-validation-policy.md)
- [Logging and privacy](../security/logging-and-privacy.md)
