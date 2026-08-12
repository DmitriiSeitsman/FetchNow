# Capacity policy

Status: Accepted for PR0B  
All limits are **environment-driven**. Business logic must read settings, not embed production numbers.

## Environment variables

```env
WORKER_CONCURRENCY=
METADATA_CONCURRENCY=
MAX_SOURCE_DURATION_SECONDS=
MAX_SOURCE_FILE_BYTES=
MAX_OUTPUT_FILE_BYTES=
TEMP_STORAGE_SOFT_LIMIT_BYTES=
MIN_FREE_DISK_BYTES=
JOB_ABSOLUTE_TTL_SECONDS=
READY_FILE_TTL_SECONDS=
MAX_REDIRECTS=
OUTBOUND_CONNECT_TIMEOUT_SECONDS=
OUTBOUND_READ_TIMEOUT_SECONDS=
```

Also related (foundation): `API_CONCURRENCY_LIMIT`, `TEMP_STORAGE_PATH`, `TEMP_STORAGE_MAX_BYTES`, `ABUSE_CONTACT_EMAIL`.

Development defaults live in `backend/.env.example` and root `.env.example`.

## Fail-closed behavior

1. **Low disk (soft limit):** do not accept new **processing** jobs that would allocate large temp artifacts. Return stable `CAPACITY_UNAVAILABLE`.
2. **Metadata-only work:** may continue only if it does not create large on-disk outputs and still respects concurrency/timeouts.
3. **Hard disk threshold (`MIN_FREE_DISK_BYTES`):** running jobs that need additional durable writes must stop safely (terminal `failed` / cleanup), not continue “best effort” into ENOSPC storms.
4. **API surface:** capacity denials use the error registry — short public message, retryable where appropriate, no internal disk paths in responses.
5. **No hardcoded limits in domain code:** constants in source are only fallback defaults for local bootstraps; production must set env explicitly.

## Concurrency

- `WORKER_CONCURRENCY` caps parallel media jobs on a worker process.
- `METADATA_CONCURRENCY` caps parallel metadata extractions (often cheaper than full download but still abuse-amplifiable).
- Gateway/API timeouts remain independent; workers must not assume unbounded queue latency.

## Size and duration

Reject or fail jobs exceeding `MAX_SOURCE_DURATION_SECONDS`, `MAX_SOURCE_FILE_BYTES`, or `MAX_OUTPUT_FILE_BYTES` with `DURATION_TOO_LONG` / `FILE_TOO_LARGE` as applicable.

PR4 media inspection reads the same duration/byte caps (and
`MEDIA_INSPECTION_MAX_*` / JSON structural bounds) when projecting metadata.
Inspection creates no durable media outputs; it respects timeouts and
stdout/stderr byte limits. PR5 workers enforce `WORKER_CONCURRENCY` for claimed
media-inspection jobs when `MEDIA_JOBS_ENABLED` and `MEDIA_INSPECTION_ENABLED`
are both true. `METADATA_CONCURRENCY` remains an env placeholder and is **not**
bound in Settings. Both job and inspection features remain disabled by default.
Job absolute TTL uses `MEDIA_JOB_ABSOLUTE_TTL_SECONDS` (not the older
`JOB_ABSOLUTE_TTL_SECONDS` placeholder name). Application-level enqueue rate
limits are not enforced in PR5.

PR6 adds `MEDIA_DOWNLOAD_CONCURRENCY` (exactly **1**; 0 and ≥2 rejected) and
`MEDIA_DOWNLOAD_MIN_FREE_BYTES` for progressive download attempts. Multi-worker
disk reservation is **not** implemented — a local semaphore must not be treated
as cross-worker free-space accounting. `MEDIA_DOWNLOADS_ENABLED` defaults false;
PR6 does not deliver files to clients. Download artifact TTL uses
`MEDIA_DOWNLOAD_ARTIFACT_TTL_SECONDS`.
`MEDIA_DOWNLOAD_ORPHAN_GRACE_SECONDS` must be **≥ 60** (default 300): orphan
reconciliation is eventually consistent with the database, so a shorter grace
can delete a publication that a peer worker has not yet committed as ready.

## TTL interaction

Capacity pressure and TTL cleanup reinforce each other: expired ready files and absolute job TTL (`JOB_ABSOLUTE_TTL_SECONDS`, `READY_FILE_TTL_SECONDS`) must be enforced so disk recovers without manual intervention.

## Related documents

- [File lifecycle policy](../product/file-lifecycle-policy.md)
- [Error codes](../api/error-codes.md)
- [Threat model](../security/threat-model.md)
