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
| `DURATION_TOO_LONG` | 422 | Media longer than the configured maximum (public text names the hour count) | no | no | Internal probe traces |
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
Worker-side terminal inspection failures usually surface as `MEDIA_INSPECTION_FAILED`
on the job record. Policy rejects map to the existing catalog codes when known:
`DURATION_TOO_LONG` (duration over `MAX_SOURCE_DURATION_SECONDS`) and
`FILE_TOO_LARGE` when size policy rejects apply. They are not an inline API
yt-dlp path.

## Media download jobs (PR6)

Durable download execution is **disabled by default**
(`MEDIA_DOWNLOADS_ENABLED=false`). When disabled, create returns
`DOWNLOADS_DISABLED` (503). PR6 does **not** deliver media bytes to clients —
that is PR7 (`GET /api/v1/media/download-jobs/{id}/content` on the dedicated
delivery service, disabled by default).
status exposes `artifactReady` only.

| Code | HTTP | Public message policy | Retryable | Client should offer retry | Forbidden details to clients |
|---|---:|---|---|---|---|
| `DOWNLOADS_DISABLED` | 503 | Downloads are not available | yes | later | Feature flags, disk paths |
| `DOWNLOAD_JOB_NOT_FOUND` | 404 | Download job not found | no | no | Whether id existed |
| `PARENT_JOB_NOT_READY` | 409 | Parent media job not ready | soft | yes (poll parent) | Parent internals |
| `FORMAT_NOT_FOUND` | 404 | Format not found | no | no | Full format lists |
| `FORMAT_NOT_ELIGIBLE` | 422 | Format not eligible | no | no | Eligibility math, tokens |
| `FORMAT_UNAVAILABLE` | 422 | Format no longer available | soft | yes (re-inspect) | Provider tokens, drift details |
| `DOWNLOAD_TIMEOUT` | 504 | Download timed out | soft | yes (limited) | Tool stderr, argv |
| `DOWNLOAD_TOO_LARGE` | 413 | Download exceeds allowed size | no | no | Exact byte counters beyond message |
| `DOWNLOAD_TOOL_FAILED` | 422 | Download tool failed | soft | maybe later | stderr, paths, tokens |
| `DOWNLOAD_INVALID_OUTPUT` | 422 | Invalid produced invalid output | no | no | Filesystem paths |
| `DOWNLOAD_STORAGE_UNAVAILABLE` | 503 | Storage temporarily unavailable | yes | yes | Disk paths, free-byte counts |
| `DOWNLOAD_EXPIRED` | 410 | Download job or artifact expired | no | no | Storage keys |
| `DELIVERY_DISABLED` | 503 | Artifact delivery is not available | yes | later | Feature flags, roots |

Browser-native grants (PR14): `POST …/browser-grants` and
`GET|HEAD …/browser-grants/{id}/content` reuse the same catalog. Unknown grant,
missing/wrong/duplicate cookie, and unauthorized shapes map to
`DOWNLOAD_JOB_NOT_FOUND` or `DOWNLOAD_EXPIRED` without revealing whether the
grant UUID exists. When `MEDIA_BROWSER_DELIVERY_ENABLED=false`, issuance and
native content return `DELIVERY_DISABLED`.
| `DOWNLOAD_NOT_READY` | 409 | Download artifact is not ready for delivery | soft | yes (poll status) | Internal job state details beyond catalog |
| `MUXING_UNAVAILABLE` | 422 | Combined video and audio file is not available | no | no | Tokens, pairing math, feature flags |
| `MUXING_FAILED` | 422 | Combining video and audio failed | soft | yes (limited) | argv, stderr, paths |
| `MUXING_TIMEOUT` | 504 | Combining video and audio timed out | soft | yes (limited) | argv, stderr, paths |
| `MUXED_OUTPUT_INVALID` | 422 | Combined file could not be validated | no | no | ffprobe JSON, paths |

| Endpoint | Notes |
|---|---|
| `POST /api/v1/media/jobs/{mediaJobId}/downloads` | Body: `formatOptionId`; requires parent MediaJob `Authorization: Bearer <accessToken>`. UUID alone never authorizes. |
| `GET /api/v1/media/download-jobs/{id}` | Same Bearer as parent. Missing/wrong credential → `DOWNLOAD_JOB_NOT_FOUND`. Public JSON may include `progressStage`, `progressPercent` (integer 0..99 or `null`, only during download stages with a bounded denominator), `artifactBytes` (positive integer only when `ready`), `suggestedFilename`, `attempt`, `maxAttempts`, `nextAttemptAt` (only while retrying), and `cancellable`. Internal failure class is never returned. |
| `POST /api/v1/media/download-jobs/{id}/cancel` | Same Bearer. Idempotent. `Cache-Control: no-store`. UUID alone never authorizes. Unknown and unauthorized are indistinguishable (`DOWNLOAD_JOB_NOT_FOUND`). Queued → `cancelled` without running a tool. Downloading records a cancel request; the worker commits `cancelled`. Ready/failed/expired are unchanged (ready artifacts are not deleted). |

See [ADR 0010](../adr/0010-durable-download-execution-and-private-artifact-boundary.md).

## Bounded muxing (PR9)

Stream-copy muxing is **disabled by default** (`MEDIA_MUXING_ENABLED=false`).
When inspection cannot offer an executable free option (no progressive file
and muxing off or no compatible pair), download create returns
`MUXING_UNAVAILABLE`. ffmpeg/ffprobe paths are worker-only.

See [ADR 0013](../adr/0013-bounded-media-muxing.md).

## Download observability and cancellation (PR10)

Public download JSON may include `cancelled`. Durable `public_state` stays on
the PR9 allowlist; PR10 projects cancellation from `expired` +
`progress_stage=cancelled` + `cancel_requested_at`. Progress is stage-based
(`progressStage`), not a percentage. Catalog errors stay catalog errors;
internal `failure_class` values are logs-only.

PR12 adds public `progressPercent` only for download stages with a bounded
denominator, exact `artifactBytes` only when ready, and `suggestedFilename`
from a sanitized title. See
[ADR 0015](../adr/0015-size-progress-filename-trust-boundary.md).

See [ADR 0014](../adr/0014-download-observability-cancellation-progress.md).

## Browser client (PR8)

The static web UI maps the codes above to short user-safe copy and never
displays `internal_reason`, tokens, URLs, or raw bodies. Unknown codes use a
generic fallback. See [browser download flow](browser-download-flow.md).

## Related documents

- [URL validation policy](../security/url-validation-policy.md)
- [ADR 0005 — safe outbound HTTP](../adr/0005-safe-outbound-http-and-redirects.md)
- [ADR 0006 — wrapper resolution foundation](../adr/0006-wrapper-resolution-foundation.md)
- [ADR 0007 — Yandex Video Preview resolver](../adr/0007-yandex-video-preview-resolver.md)
- [ADR 0008 — media inspection tool boundary](../adr/0008-media-inspection-and-tool-boundary.md)
- [ADR 0009 — PostgreSQL media-job orchestration](../adr/0009-postgresql-media-job-orchestration.md)
- [ADR 0010 — durable download execution](../adr/0010-durable-download-execution-and-private-artifact-boundary.md)
- [ADR 0012 — browser download flow](../adr/0012-browser-download-flow.md)
- [ADR 0013 — bounded media muxing](../adr/0013-bounded-media-muxing.md)
- [ADR 0014 — download observability, cancellation, and progress](../adr/0014-download-observability-cancellation-progress.md)
- [ADR 0015 — size, progress percent, and filename trust boundary](../adr/0015-size-progress-filename-trust-boundary.md)
- [Capacity policy](../operations/capacity-policy.md)
