# ADR 0011: Authenticated private artifact delivery boundary

## Status

Accepted (PR7). Staging remains **disabled** by default.

## Context

PR6 publishes private artifacts under a worker-managed root and exposes only
create/status APIs. Clients still need a way to download ready bytes without
giving the general `api` process filesystem access to those artifacts.

## Decision

Introduce a dedicated internal `delivery` process that shares the trusted
backend image but mounts the artifact volume **read-only** and receives only
delivery-safe settings. The shared image still contains the `yt-dlp` binary;
delivery code never invokes it and does not receive a configured tool path.
`DATABASE_URL` is the shared application credential (not an enforced read-only
DB role).

### Authorization

`GET|HEAD /api/v1/media/download-jobs/{download_job_id}/content` requires:

`Authorization: Bearer <parent MediaJob accessToken>`

UUID alone never authorizes. Missing, malformed, wrong Bearer, and unknown IDs
are indistinguishable (`DOWNLOAD_JOB_NOT_FOUND` / 404) and must not query the
database or emit exception logs for token-shape failures. Authenticated but
non-ready jobs return `DOWNLOAD_NOT_READY` / 409. Expired parent/job/TTL return
`DOWNLOAD_EXPIRED` / 410. Ready but incoherent storage returns
`DOWNLOAD_STORAGE_UNAVAILABLE` / 503 without revealing whether a directory
exists.

No query-string tokens, cookies, or redirects carry credentials.

### ArtifactReader / FD lifecycle

`ArtifactReader` is read-only and separate from mutating `ArtifactStore`.

- Root must be absolute, already normalized, mode `0700`, owned by the delivery
  UID. Symlinks in any path component are rejected by a **descriptor-relative
  walk from `/`**: each component is opened with `O_DIRECTORY | O_NOFOLLOW`
  via `dir_fd`, then validated with `fstat` on the final root FD (never a
  pathname `is_symlink` pre-check followed by reopening the absolute path).
- Published/artifact children open relative to that validated root FD.
- File opens use `O_NOFOLLOW` and `O_NONBLOCK` so FIFO/device opens cannot hang
  the worker.
- Manifest and artifact are validated (mode `0600`, `nlink=1`, schema, sizes,
  container/MIME, job/format/fence/expiry coherence, SHA-256 hex grammar).
- Bytes stream from the already-open FD via bounded `os.pread` offloaded to a
  thread. Early EOF before the promised interval aborts as a storage failure
  (not a clean completion). Disconnect/cancel before stream ownership transfer
  closes any open FD and releases the process-local semaphore exactly once;
  after ownership transfer only the stream body `finally` releases them.
- Unlink-after-open (worker TTL cleanup) does not break an in-flight stream.
- PR7 never deletes artifacts after GET and never marks them consumed: ASGI
  cannot prove the client received every byte.

### Integrity model

PR7 verifies strict DB ↔ manifest ↔ file metadata coherence and SHA-256
**grammar** from the private manifest. It does **not** re-hash the full payload
on every download (would double I/O). Residual risk: bit-rot that preserves size
could be streamed until TTL; operators may add offline scans later.

### Range policy

Single `bytes=` ranges are supported (`start-end`, `start-`, `-suffix`) with
`206` / `Content-Range` / `416`. Multiple ranges, malformed units, and oversized
headers are rejected. Header parsing is length-bounded.

### Cache / security headers

Successful responses set `Cache-Control: private, no-store`, `Pragma: no-cache`,
`X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, a restrictive
CSP, and a server-generated ASCII
`Content-Disposition: attachment; filename="fetchnow-<id>.<container>"`.

### Compose isolation

| Process | Artifact volume | yt-dlp invocation |
|---|---|---|
| `api` | **none** | never (no configured path) |
| `worker` | `tmp` read-write | yes when downloads/inspection enabled |
| `delivery` | `tmp` **read-only** | never (no configured path; shared image still contains the binary) |
| `gateway` | none | n/a |

Gateway proxies only the exact content path regex to `delivery`; other `/api/`
traffic stays on `api`. No new host port. Feature flag
`MEDIA_DELIVERY_ENABLED` defaults to `false`. That route resolves `delivery`
per request instead of through an `upstream` block: names in an `upstream` are
resolved when the config loads, so an absent or crash-looping delivery would
otherwise keep the gateway — and with it the frontend — from starting. The
failure is confined to a 502 on the content route.

### Concurrency

`MEDIA_DELIVERY_CONCURRENCY` is a **process-local** semaphore only. It is not a
cross-replica rate limit.

## Consequences

- Clients can download ready artifacts without expanding the API attack surface.
- Delivery code does not mkdir/chmod/unlink/reconcile or invoke yt-dlp, and does
  not receive a configured yt-dlp path. The shared backend image still contains
  the binary; a lean delivery image is a follow-up.
- Shared `DATABASE_URL` is not an enforced read-only DB role; a dedicated
  read-only credential is a follow-up.
- Operators must point `MEDIA_DELIVERY_ROOT` at the same published root the
  worker uses, already normalized, mode 0700.

## Residual risks

1. Stolen parent Bearer still yields bytes until TTL.
2. No application-layer download rate limiting across replicas.
3. Full-file digest is not recomputed per request (see integrity model).
4. Shared volume semantics still require worker reconciliation grace floors.
5. Gateway must be rebuilt whenever nginx routing changes.
6. Shared image contains `yt-dlp` even though delivery never invokes it.
7. Delivery uses the shared writable DB role (mutation not prevented by role).

## Alternatives considered

- Serve from `api` with a shared volume — rejected; violates private artifact
  boundary.
- `X-Accel-Redirect` to a filesystem path — rejected; leaks storage topology
  into the gateway and complicates authorization.
- Query download tokens — rejected; tokens in URLs leak via logs/referrers.
- Separate lean delivery image / read-only DB role — deferred; document as
  residual risk until enforced and validated.