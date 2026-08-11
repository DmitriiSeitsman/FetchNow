# API error code registry

Status: Accepted for PR0B · Runtime codes for validation (PR1) and outbound probe (PR2)

Envelope shape (already used by the API foundation):

```json
{
  "error": {
    "code": "CAPACITY_UNAVAILABLE",
    "message": "Service is temporarily at capacity",
    "request_id": "…"
  }
}
```

## Codes

| Code | HTTP | Public message policy | Retryable | Client should offer retry | Forbidden details to clients |
|---|---:|---|---|---|---|
| `INVALID_URL` | 400 | Short: URL could not be accepted | no | no | Parser internals, raw exception |
| `UNSUPPORTED_SCHEME` | 400 | Only http/https allowed | no | no | Other schemes discovered internally |
| `UNSUPPORTED_PROVIDER` | 422 | Provider/host not supported | no | no | Full allowlist contents |
| `BLOCKED_DESTINATION` | 422 | Destination not allowed | no | no | Resolved private IPs, DNS answers |
| `REDIRECT_LIMIT_EXCEEDED` | 422 | Too many redirects | no | no | Full redirect chain URLs |
| `INVALID_REDIRECT` | 422 | Redirect target not accepted | no | no | Location header, query tokens |
| `UNSUPPORTED_CONTENT_TYPE` | 422 | Response content type not allowed | no | no | Raw Content-Type header bag |
| `RESPONSE_TOO_LARGE` | 413 | Response exceeded allowed size | no | no | Exact byte counters beyond public message |
| `SOURCE_UNAVAILABLE` | 502 | Source could not be fetched | soft | yes (limited) | Upstream headers/body, TLS internals, peer IPs |
| `SOURCE_TIMEOUT` | 504 | Source timed out | soft | yes (limited) | Peer IPs, exact syscall errors |
| `RATE_LIMITED` | 429 | Too many requests | yes | yes (honor Retry-After) | Per-user internal quotas math |
| `CAPACITY_UNAVAILABLE` | 503 | Temporary capacity limit | yes | yes | Disk paths, free-byte counts |
| `FILE_TOO_LARGE` | 413 | File exceeds limit | no | no | Exact configured byte caps (optional high-level OK) |
| `DURATION_TOO_LONG` | 422 | Media longer than allowed | no | no | Internal probe traces |
| `JOB_NOT_FOUND` | 404 | Job not found | no | no | Whether ID existed then deleted |
| `JOB_EXPIRED` | 410 | Job or file expired | no | no | Storage keys |
| `INTERNAL_ERROR` | 500 | Unexpected error | soft | maybe later | Stack traces, hostnames, SQL |

**Retryable** meanings: `yes` = safe to retry same request; `soft` = maybe transient; `no` = change input or stop.

## Message policy

- Prefer fixed, translated-friendly short strings.
- Never echo the user URL back into the error message.
- Never include filesystem paths, internal IPs, or tool stderr dumps.

## Evolution

New codes require a docs update in this registry before public clients depend on them. Renaming published codes is a breaking change.

## Wrapper resolution domain outcomes (PR3A)

Lower-case kinds such as `wrapper_unsupported`, `wrapper_unresolved`,
`resolved_provider_unsupported`, `resolution_loop`,
`resolution_limit_exceeded`, and `unsafe_resolution_target` are **domain**
outcomes inside `fetchnow.resolution`. They are **not** public API error codes
in PR3A. Existing `/media/validate` and `/media/probe` continue to return the
uppercase codes above. API mapping will be added when a production wrapper is
wired.

## Related documents

- [URL validation policy](../security/url-validation-policy.md)
- [ADR 0005 — safe outbound HTTP](../adr/0005-safe-outbound-http-and-redirects.md)
- [ADR 0006 — wrapper resolution foundation](../adr/0006-wrapper-resolution-foundation.md)
- [Capacity policy](../operations/capacity-policy.md)
