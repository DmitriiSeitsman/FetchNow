# ADR 0010: Durable download execution and private artifact boundary

- Status: Accepted
- Date: 2026-08-12
- PR: PR6 (`feat/pr6-download-execution-foundation`)

## Context

PR5 delivered durable media-inspection jobs and sanitized `MediaMetadata` /
`formatOptionId` options. Clients still cannot obtain media bytes. Progressive
download requires a second durable queue, exact option→provider-token binding at
attempt time, and a private on-disk artifact boundary that never becomes a public
file URL in this PR.

yt-dlp already runs behind a hardened subprocess boundary for metadata (ADR 0008).
Downloads reuse that executable path on the worker only, with a download-specific
argv builder (exact `-f <token>`, no merge/ffmpeg, no cookies). API processes must
not receive tool paths or artifact roots.

Disk is scarce and multi-worker reservation is not implemented.
`MEDIA_DOWNLOAD_CONCURRENCY` is therefore fail-closed to exactly **1** (0 and
values ≥2 are rejected). A process-local semaphore is not a cross-worker disk
reservation.

## Decision

### Separate download job table and state machine

Persist `media_download_jobs` with public states `queued` → `downloading` →
`ready` | `failed`, plus `expired`. Idempotency is `UNIQUE(media_job_id,
format_option_id)`. Parent authorization reuses the PR5 client Bearer token via
the parent `media_jobs.credential_hash` — download UUID alone never authorizes.

### Exact re-inspect selection (no closest/best fallback)

At claim time the worker re-runs draft inspection, rebuilds the ephemeral
`formatOptionId` → `provider_format_token` map, and requires an exact match to the
persisted selection snapshot (option id + public properties + free-tier
progressive eligibility). Drift or ineligibility fails closed as
`FORMAT_UNAVAILABLE`. Tokens are never persisted, logged, or included in
`repr`/`str`.

### Private artifact store

Worker-only `MEDIA_DOWNLOAD_TEMP_ROOT` holds attempt workspaces
(`home/`, `cache/`, `output/` at mode 0700) and atomically published units under
`published/{artifact_id}/` (`artifact.<ext>` + `manifest.json` at mode 0600).
API responses expose only `artifactReady` (boolean), never paths, artifact ids,
or tokens. DB-aware orphan reconciliation stays inside the managed root. PR6
does **not** deliver files to clients.

The managed root must already be an absolute normalized directory at exactly
mode 0700 owned by the worker UID, with no symlink in any path component. An
existing operator-provided root is never chmodded: unsafe metadata fails closed
so a mislabeled shared directory cannot be silently adopted. Artifact opens use
`O_NOFOLLOW` relative to a validated output dirfd and repair mode with `fchmod`
on the open descriptor, so an entry swapped for a symlink between listing and
opening can never expose its target.

`MEDIA_DOWNLOAD_ORPHAN_GRACE_SECONDS` has a hard floor of 60 seconds.
Reconciliation is eventually consistent with the database, so a shorter grace
would let one worker delete a publication a peer created moments earlier but
has not yet committed as ready. Symlink entries under the root, job attempt
directories, and `published/` are unlinked themselves after grace — never
followed — so an invalid link cannot persist or redirect deletion.

### Idempotent create coherence

`UNIQUE(media_job_id, format_option_id)` recovery returns the existing row only
when provider identity, canonical URL, media identity, structured
scheme/host/path/port, schema version, max attempts, and the exact
selected-format snapshot all match the requested creation snapshot; any
divergence fails closed as `INTERNAL_ERROR`. Download `expires_at` is bounded to
`min(parent.expires_at, database_now + artifact_ttl)` so a download job can
never outlive the authorization parent that gates every read of it. Persisted
snapshots are strict-decoded and re-encoded before serialization, so stored
JSONB is never echoed to clients.

### Worker integration without starving inspection

`MediaJobWorkerRunner` keeps separate active maps/semaphores:
`worker_concurrency` for inspection and `media_download_concurrency` (exactly 1)
for downloads. Download claims require both `MEDIA_DOWNLOADS_ENABLED` and
`MEDIA_INSPECTION_ENABLED` (re-inspect). Disk headroom is checked before claim;
runtime output-size monitoring terminates oversized process groups before
publication. SIGTERM cancels both queues with the existing grace period.

### Compose / env split

- API: `MEDIA_DOWNLOADS_ENABLED=false` plus enqueue-safe TTL/max-attempts only.
  No `MEDIA_INSPECTION_YTDLP_PATH`, no `MEDIA_DOWNLOAD_TEMP_ROOT`.
- Worker: full `MEDIA_DOWNLOAD_*` plus shared `MEDIA_INSPECTION_YTDLP_PATH`.
- Features default **false**; staging is not enabled by this PR.

### Residual risk

yt-dlp performs its own HTTP outside `SafeHTTPClient` (same residual SSRF class
as ADR 0008). Application-level rate limits remain unimplemented.

## Consequences

- Alembic head becomes `0003_download_jobs` (revises `0002_media_jobs`).
  PR10 revises this to `0004_download_observability`.
- Public codes for download create/status are registered in
  [error-codes.md](../api/error-codes.md).
- Delivery (signed/direct download), ffmpeg mux, and Turbo entitlements remain
  later PRs.

## Related documents

- [ADR 0008 — media inspection tool boundary](0008-media-inspection-and-tool-boundary.md)
- [ADR 0009 — PostgreSQL media-job orchestration](0009-postgresql-media-job-orchestration.md)
- [Capacity policy](../operations/capacity-policy.md)
- [Threat model](../security/threat-model.md)
- [Logging and privacy](../security/logging-and-privacy.md)
