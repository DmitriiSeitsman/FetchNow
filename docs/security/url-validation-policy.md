# URL validation policy

Status: Accepted (PR0B spec) · Implemented for parse/registry/DNS in PR1  
Redirect-following and outbound HTTP probe re-validation remain **future** work.

Executable code lives in `backend/src/fetchnow/url/`. Conformance vectors:
`backend/tests/fixtures/url_security_cases.json` (`stage=validate` executed;
`stage=redirect_future` deferred).

## Goals

Treat every user-supplied URL as hostile. Before any outbound connect (probe, yt-dlp, or HTTP client):

1. Parse and normalize strictly.
2. Enforce scheme, port, credentials, and provider allowlist rules.
3. Resolve DNS and classify **every** returned address.
4. Re-validate after redirects, hop by hop (**not in PR1**).

Fail closed on ambiguity.

## Requirements

### Scheme and credentials

- Allow only `http` and `https` (MVP may further prefer `https`-only for some providers).
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

### DNS and redirects

- After DNS resolution, re-run address classification on **all** answers. If any answer is blocked, deny (do not “prefer” a public A record while a private one exists unless a future explicit policy says otherwise — default is deny).
- Follow redirects only up to `MAX_REDIRECTS`.
- Re-validate scheme, host allowlist, ports, and resolved addresses on **each** hop.
- A redirect from an allowed host to a private IP or non-allowlisted host is a hard deny (`BLOCKED_DESTINATION` / `REDIRECT_LIMIT_EXCEEDED` as applicable).

### Ports

- Default allow only `80` and `443` (or empty default ports for http/https).
- Non-standard ports are denied unless a future explicit provider exception is documented and reviewed.

### Provider hostname allowlist

- Only explicitly allowlisted hostnames (and explicitly listed subdomains) are accepted.
- Matching must be **label-boundary safe**:
  - do **not** use naive `endswith("vk.com")`;
  - `vk.com.attacker.example` must not match `vk.com`;
  - `notvk.com` must not match `vk.com`;
  - `evil.vk.com` is allowed only if `evil.vk.com` or a documented `*.vk.com` rule is explicitly configured.
- Username/password or userinfo containing a trusted hostname must not satisfy the allowlist.

### Probe limits

For any preflight/probe HTTP request:

- `OUTBOUND_CONNECT_TIMEOUT_SECONDS` and `OUTBOUND_READ_TIMEOUT_SECONDS`;
- maximum response size;
- redirect cap as above.

## Error mapping (public)

Prefer stable codes from `docs/api/error-codes.md`:

- bad scheme → `UNSUPPORTED_SCHEME`
- parse/credential/port issues → `INVALID_URL`
- host not allowlisted → `UNSUPPORTED_PROVIDER`
- private/metadata/localhost → `BLOCKED_DESTINATION`
- too many redirects → `REDIRECT_LIMIT_EXCEEDED`

Do not return raw resolver internals or internal IPs to clients.

## Test vectors

Machine-readable cases live in:

`backend/tests/fixtures/url_security_cases.json`

Schema is documented in that file’s `schema` field. PR1 executes all `stage=validate` cases against `URLValidator` with `FakeDnsResolver`. Cases marked `stage=redirect_future` remain documented until redirect re-validation ships.

## Related documents

- [Threat model](threat-model.md)
- [Error codes](../api/error-codes.md)
- [ADR 0003](../adr/0003-security-boundaries-before-media-processing.md)
- [ADR 0004](../adr/0004-provider-registry-and-dns-validation.md)
