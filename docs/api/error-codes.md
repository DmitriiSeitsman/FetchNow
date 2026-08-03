# API error code registry

Status: Accepted for PR0B  
This is a **stable public code namespace**. Runtime handlers for every code are **not** required in this PR.

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
| `SOURCE_UNAVAILABLE` | 502 | Source could not be fetched | soft | yes (limited) | Upstream headers/body |
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

## Related documents

- [URL validation policy](../security/url-validation-policy.md)
- [Capacity policy](../operations/capacity-policy.md)
