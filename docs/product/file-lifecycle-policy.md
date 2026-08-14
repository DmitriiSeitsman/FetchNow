# File lifecycle policy

Status: Accepted for PR0B  
Specification only — no job tables or storage models are introduced in this PR.

## States

| State | Meaning |
|---|---|
| `downloading` | Fetching source bytes into temporary storage |
| `processing` | Transcoding / remux / packaging in progress |
| `ready` | Output available for user download within TTL |
| `downloading-by-user` | User is actively pulling the prepared file |
| `expired` | TTL elapsed; object must not be served |
| `deleting` | Cleanup in progress |
| `deleted` | Bytes removed; metadata may retain terminal status briefly |
| `failed` | Terminal failure; cleanup required |
| `quarantined` | Held for abuse/security review; not user-downloadable |

Transitions are fail-closed: unknown or illegal transitions must not re-expose bytes.

## TTL and absolute limits

- **Ready-file TTL** (`READY_FILE_TTL_SECONDS`): maximum time an object may remain downloadable after becoming `ready`.
- **Absolute job TTL** (`JOB_ABSOLUTE_TTL_SECONDS`): wall-clock ceiling from job creation to forced terminal cleanup, regardless of state.
- **Media inspection job TTL** (`MEDIA_JOB_ABSOLUTE_TTL_SECONDS`): wall-clock ceiling for inspection rows (`queued` / `inspecting` / `inspected` / `failed` → `expired`). Expiry clears persisted `result_metadata` and `public_error_code` (privacy/capacity). `completed_at` is kept when already set. The job row is not deleted.
- Download tokens (future) inherit a **short TTL** strictly ≤ ready-file TTL.

Defaults are environment-driven; see `docs/operations/capacity-policy.md`.

## Cleanup triggers

Cleanup (transition toward `deleting` → `deleted`) must run for:

- TTL / absolute job expiry;
- failed jobs;
- user or system cancellation;
- process/worker restart recovery (reconcile orphaned temp objects);
- abuse quarantine disposition that requires destruction;
- low-disk enforcement when operators or automated policy demand eviction of expired/failed first.

**Indefinite retention is forbidden.** There is no “keep forever” product mode in MVP.

## Low disk behavior

When free disk falls below soft/hard thresholds (`TEMP_STORAGE_SOFT_LIMIT_BYTES`, `MIN_FREE_DISK_BYTES`):

- refuse new processing jobs that would write large artifacts (fail closed with `CAPACITY_UNAVAILABLE`);
- prioritize deleting `expired`, `failed`, and abandoned temp paths;
- running jobs that cannot proceed safely must stop without leaving world-readable debris.

## Access and naming

- Prepared files are **not** placed under the public web root and are **not** indexed.
- Objects are **not** reachable via guessable paths or sequential IDs alone.
- Storage keys are **cryptographically random**; the original filename is never the storage key.
- Display names are a sanitized provider title (not a trusted raw filename).
  Pre-download size shown to users is approximate; the ready artifact size is
  exact. No extra CDN request is made just to estimate size.

## Related documents

- [Capacity policy](../operations/capacity-policy.md)
- [Product policy](product-policy.md)
- [ADR 0003](../adr/0003-security-boundaries-before-media-processing.md)
