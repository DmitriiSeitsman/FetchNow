# ADR 0009: PostgreSQL media-job orchestration

- Status: Accepted
- Date: 2026-08-12
- PR: PR5 (`feat/pr5-postgres-media-job-orchestration`)

## Context

PR4 introduced an internal, metadata-only media inspection foundation (yt-dlp behind
a hardened subprocess boundary, disabled by default). Clients still need a durable
way to submit a public URL, recover after a lost response, and poll for sanitized
`MediaMetadata` without running yt-dlp inside API request handlers.

FetchNow already standardized on PostgreSQL as the sole durable datastore
(ADR 0001). Introducing Redis or another queue technology would add operational
surface without solving ownership, TTL, or migration contracts already tied to
Postgres.

Anonymous MVP traffic cannot use account sessions. Job UUID alone must not
authorize access. The create response may be lost, so clients need a credential
they generated before the POST and can safely retry.

## Decision

### Why PostgreSQL queue

Persist inspection jobs in `media_jobs` and claim work with
`SELECT … FOR UPDATE SKIP LOCKED`. Leases, fencing, TTL, and idempotency live in
the same transactional store as the schema migrations. No separate broker.

### Anonymous ownership protocol (client-generated token, no server secret)

1. Client generates 32 cryptographically random bytes and encodes them as
   base64url **without padding** (43 characters).
2. Client presents the same value as `Authorization: Bearer …` on POST and GET.
   The API never places or echoes credentials in JSON and marks responses
   `Cache-Control: no-store`.
3. Server never stores the raw token. It stores only:
   - `credential_hash = SHA256(b"fetchnow:v1:media-job:access\0" + raw_32_bytes)`
   - `request_fingerprint = HMAC-SHA256(raw_token, b"fetchnow:v1:media-job:fingerprint\0" + offline_normalized_submitted_url)`
4. `UNIQUE(credential_hash)` provides idempotency identity. Concurrent creates
   insert-or-conflict; matching fingerprints return the same job; mismatched
   fingerprints raise `IDEMPOTENCY_CONFLICT`.
5. Comparisons use constant-time digests (`hmac.compare_digest`). Raw tokens are
   omitted from logs, `JobError` str/repr, and metrics.

**Secret rotation:** there is no server-side job secret to rotate. Compromised
client tokens are mitigated by absolute job TTL and by never logging tokens.
Operators revoke access only by expiring/deleting rows, not by rotating a shared
HMAC key.

### First-response-loss recovery

Because the client generated the token before POST, a retry with the same token
and same normalized request looks up and verifies the existing row before DNS,
HTTP, or wrapper resolution. A lost 202 does not strand ownership even when the
wrapper later changes or becomes unavailable. Keying the fingerprint with the
256-bit token also prevents a DB reader from guessing submitted URLs offline.

### Schema and state machine

Inspection-oriented public states only: `queued` → `inspecting` →
`inspected` | `failed`, plus `expired`. Illegal transitions fail closed in
application code; CHECK constraints bound stored state strings. Persisted fields
are trusted terminal provider targets (no submitted/wrapper URL, no
query/fragment, no direct media URLs, no raw tool output).

### SKIP LOCKED, lease, fencing, recovery

Workers claim with `FOR UPDATE SKIP LOCKED`, set `lease_owner` /
`lease_expires_at`, and bump `fence_token`. Completion and failure UPDATEs require
matching owner + fence, a live lease, and a live absolute TTL. A guarded
heartbeat renews long-running inspection leases; renewal loss cancels the
inspection and discards its result. Expired leases are reclaimed with a fence
bump, and exhausted crash attempts become terminal instead of looping. Queue
correctness timestamps come from PostgreSQL `clock_timestamp()`.

### Retry classification and TTL

Retryable inspection kinds (timeout / tool unavailable / transient media /
internal) reschedule to `queued` with bounded exponential backoff until
`max_attempts`. Permanent failures go terminal `failed`. Absolute TTL
(`MEDIA_JOB_ABSOLUTE_TTL_SECONDS`) expires rows independently of lease state.
The expiry transition stores `public_state=expired` with `result_metadata` and
`public_error_code` NULL so the row satisfies `ck_media_jobs_result_state`.
`completed_at` is preserved when already set (inspected/failed) and set to the
expiry clock when missing (queued/inspecting). Lease fields are cleared and
`fence_token` is incremented once. Terminal inspection payloads are a
privacy/capacity boundary: they must not remain after TTL and must not be
logged when cleared. Leaving inspected/failed payloads in place causes
PostgreSQL to reject the UPDATE, rolls back the worker poll transaction, and
blocks claim of new queued jobs.

### API / worker trust boundary

API runs `ResolutionService` (including bounded wrapper resolution) and persists
trusted terminal fields only. API never constructs `MediaInspectionService` for
execution and does not receive a requirement to set `MEDIA_INSPECTION_YTDLP_PATH`
for job create/status. Worker alone may enable inspection and spawn yt-dlp after
re-validating persisted targets.

### Why this PR does not download media

PR5 stores and returns sanitized metadata/format options only. No download
endpoint, no media bytes on disk for delivery, no ffmpeg mux. Download tickets
remain later PRs.

### Data minimization

Responses expose camelCase public job fields and sanitized `MediaMetadata`.
Forbidden JSON keys (URLs, tokens, stderr, paths, headers, argv) are rejected by
the metadata codec before persistence/load.

### Residual abuse / rate-limit risk

`MEDIA_JOBS_ENABLED` defaults **false**. When enabled, anonymous clients can still
enqueue inspection work up to worker concurrency and provider-side limits.
Application-level per-IP / abuse quotas (`RATE_LIMITED`) are **not** enforced by
this PR. Operators must keep the feature off until capacity and gateway limits
are acceptable, and must treat yt-dlp network I/O as residual SSRF risk outside
`SafeHTTPClient` (ADR 0008).

### Operational / migration implications

- Alembic head becomes `0002_media_jobs` (revises `0001_baseline`).
- Staging/production must run `alembic upgrade head` via the existing migration
  transaction path before enabling the feature.
- Compose forwards `MEDIA_JOBS_*` (default false) to API and worker; worker also
  receives `MEDIA_INSPECTION_*` (still default false).
- Enabling jobs without enabling inspection only maintains queue hygiene (expire /
  reclaim); workers do not claim for yt-dlp until inspection is enabled.

## Consequences

- CI offline tests cover tokens, states, settings, codec, API fail-closed paths,
  SQL claim compilation, target rebuild, and worker disabled/no-claim behavior.
- Optional Postgres integration tests (env `FETCHNOW_TEST_DATABASE_URL`) cover
  concurrent claim, idempotency, fencing, expiry, and Alembic up/down/up.
- Feature remains off by default; staging examples must not enable it casually.
- Download execution, ready-file TTL enforcement for media artifacts, and abuse
  rate limits remain subsequent work.
