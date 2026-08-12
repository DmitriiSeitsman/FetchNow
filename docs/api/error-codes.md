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
| `WRAPPER_UNSUPPORTED` | 422 | URL wrapper not supported | no | no | Raw URL, query, HTML |
| `WRAPPER_UNRESOLVED` | 422 | Wrapper could not be resolved | no | no | Document body, candidate URLs |
| `RESOLVED_PROVIDER_UNSUPPORTED` | 422 | Resolved media destination not supported | no | no | Candidate URL, allowlist contents |
| `RESOLUTION_LOOP` | 422 | Wrapper resolution loop | no | no | Fingerprints, query |
| `RESOLUTION_LIMIT_EXCEEDED` | 422 | Wrapper depth exceeded | no | no | Internal depth counters |
| `UNSAFE_RESOLUTION_TARGET` | 422 | Resolution target not allowed | no | no | Resolved IPs, Location, query |
| `RATE_LIMITED` | 429 | Too many requests | yes | yes (honor Retry-After) | Per-user internal quotas math |
| `CAPACITY_UNAVAILABLE` | 503 | Temporary capacity limit | yes | yes | Disk paths, free-byte counts |
| `FILE_TOO_LARGE` | 413 | File exceeds limit | no | no | Exact configured byte caps (optional high-level OK) |
| `DURATION_TOO_LONG` | 422 | Media longer than allowed | no | no | Internal probe traces |
| `JOB_NOT_FOUND` | 404 | Job not found | no | no | Whether ID existed then deleted |
| `JOB_EXPIRED` | 410 | Job or file expired | no | no | Storage keys |
| `IDEMPOTENCY_CONFLICT` | 409 | Access token already used for a different request | no | no | Fingerprints, prior job fields |
| `INVALID_ACCESS_TOKEN` | 400 | Access token is invalid | no | no | Raw token, decode internals |
| `MEDIA_INSPECTION_FAILED` | 422 | Media inspection failed | soft | maybe later | yt-dlp stderr, URLs, tool argv |
| `INTERNAL_ERROR` | 500 | Unexpected error | soft | maybe later | Stack traces, hostnames, SQL |

**Retryable** meanings: `yes` = safe to retry same request; `soft` = maybe transient; `no` = change input or stop.

## Message policy

- Prefer fixed, translated-friendly short strings.
- Never echo the user URL back into the error message.
- Never include filesystem paths, internal IPs, or tool stderr dumps.

## Evolution

New codes require a docs update in this registry before public clients depend on them. Renaming published codes is a breaking change.

## Wrapper resolution domain outcomes (PR3A/PR3B)

Lower-case kinds such as `wrapper_unsupported`, `wrapper_unresolved`,
`resolved_provider_unsupported`, `resolution_loop`,
`resolution_limit_exceeded`, and `unsafe_resolution_target` are **domain**
outcomes inside `fetchnow.resolution`.

PR3B maps them to uppercase public API codes on `POST /api/v1/media/resolve`:

| Domain kind | Public code | HTTP |
|---|---|---:|
| `wrapper_unsupported` | `WRAPPER_UNSUPPORTED` | 422 |
| `wrapper_unresolved` | `WRAPPER_UNRESOLVED` | 422 |
| `resolved_provider_unsupported` | `RESOLVED_PROVIDER_UNSUPPORTED` | 422 |
| `resolution_loop` | `RESOLUTION_LOOP` | 422 |
| `resolution_limit_exceeded` | `RESOLUTION_LIMIT_EXCEEDED` | 422 |
| `unsafe_resolution_target` | `UNSAFE_RESOLUTION_TARGET` | 422 |
| `internal_error` | `INTERNAL_ERROR` | 500 |

Existing `/media/validate` and `/media/probe` continue to return the uppercase
URL/network codes above and are unchanged by wrapper resolution.

## Media inspection jobs (PR5)

Durable job orchestration is **disabled by default** (`MEDIA_JOBS_ENABLED=false`).
When disabled, create returns `CAPACITY_UNAVAILABLE` (503).

| Endpoint | Notes |
|---|---|
| `POST /api/v1/media/jobs` | Body: `url`; requires `Authorization: Bearer <accessToken>` with a client-generated 43-char base64url credential. Resolves only on first creation; an authenticated retry is recovered before network resolution. API does **not** run yt-dlp. |
| `GET /api/v1/media/jobs/{id}` | Requires `Authorization: Bearer <accessToken>`. Missing/wrong credential → `JOB_NOT_FOUND` (indistinguishable). Expired → `JOB_EXPIRED`. |

Ownership uses hashed credentials only (see [ADR 0009](../adr/0009-postgresql-media-job-orchestration.md)).
Worker-side terminal inspection failures surface as `MEDIA_INSPECTION_FAILED` on
the job record; they are not an inline API yt-dlp path.

## Related documents

- [URL validation policy](../security/url-validation-policy.md)
- [ADR 0005 — safe outbound HTTP](../adr/0005-safe-outbound-http-and-redirects.md)
- [ADR 0006 — wrapper resolution foundation](../adr/0006-wrapper-resolution-foundation.md)
- [ADR 0007 — Yandex Video Preview resolver](../adr/0007-yandex-video-preview-resolver.md)
- [ADR 0008 — media inspection tool boundary](../adr/0008-media-inspection-and-tool-boundary.md)
- [ADR 0009 — PostgreSQL media-job orchestration](../adr/0009-postgresql-media-job-orchestration.md)
- [Capacity policy](../operations/capacity-policy.md)
