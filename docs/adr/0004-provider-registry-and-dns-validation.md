# ADR 0004: Provider registry and DNS validation

- Status: Accepted
- Date: 2026-08-03

## Context

PR1 introduces executable URL validation. User input can name arbitrary hosts. Resolving every submitted hostname would turn FetchNow into an open DNS/SSRF probe. Validation must therefore combine a closed provider hostname registry with destination classification.

## Decision

### DNS only after provider allowlist

Pipeline order:

1. syntactic parse / scheme / credentials / ports  
2. hostname normalization (IDNA)  
3. reserved-name and literal-IP classification  
4. **provider registry lookup**  
5. DNS resolution (injectable)  
6. destination classification of **all** answers  

Unknown hosts fail with `UNSUPPORTED_PROVIDER` without a resolver call. This prevents using FetchNow to enumerate internal DNS or resolve attacker-controlled names at validation time.

### Mixed public/private resolution is denied

If any resolved address is blocked (private, loopback, link-local, multicast, CGNAT, metadata, non-global, IPv4-mapped private, …), the hostname is rejected (`BLOCKED_DESTINATION`). Preferring only the public A/AAAA record would enable DNS rebinding style bypasses.

### Validation does not guarantee safe future HTTP connect

PR1 does not open TCP connections or follow redirects. A hostname that is allowlisted and currently resolves only to public addresses may change (TOCTOU) before a later fetch. Future network PRs must re-validate destinations at connect time and after every redirect hop (connection pinning / revalidation).

### DNS results are never returned to clients

Resolved IPs are internal to the worker/API trust boundary. Public responses expose provider id, normalized hostname, path, and a **query-stripped** canonical URL only.

### Canonical URL omits query

PR0B logging/privacy policy treats query strings as potentially secret. The validate API therefore returns `canonical` without query or fragment. Path is retained for product UX; query remains available only inside the domain object for future server-side use and must not be logged.

### Redirect cases deferred

Fixture cases that require following `Location` to a private IP are marked `stage=redirect_future`. They remain documented but are not executed until an outbound HTTP client exists.

## Consequences

- FakeDnsResolver keeps unit/security tests offline.
- SystemDnsResolver uses `asyncio` + `getaddrinfo` (no shell, no subprocess).
- Adding a provider means adding an explicit descriptor with exact hostnames — never `endswith`.
