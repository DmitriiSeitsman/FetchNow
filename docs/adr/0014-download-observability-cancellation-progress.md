# ADR 0014: Download observability, cancellation, and public progress

## Status

Accepted (PR10). Feature flags are unchanged: downloads, muxing, delivery, and
the browser flow remain independently off by default.

## Context

PR9 exact-token muxing inspected Yandex Preview successfully but download jobs
failed with `DOWNLOAD_TOOL_FAILED` after three attempts and no artifact. Offline
proof showed yt-dlp `parse_bytes` rejecting `--max-filesize` values with a `B`
suffix (`"1024B"` → `None`), so the server path died on the first video
download. A terminal `yt-dlp <URL>` succeeded because it uses default selection
and does not pass that flag.

Operators could not tell which subprocess stage failed. Users could only stop
browser polling (`Start over`); the durable job kept running. The UI showed a
single “Preparing your download…” string.

## Decision

1. **Exact pipeline.** Pass `--max-filesize` as a bare integer byte count.
   Exact provider format tokens remain mandatory. `best`, `bestvideo+bestaudio`,
   and other generic selectors stay forbidden. Format drift is a catalog error.
2. **Allowlisted stage events.** Worker logs named events such as
   `download_video_failed` / `media_mux_failed` with only job ids, attempt,
   fence, stage, duration, retryable, catalog error code, internal failure
   class, process exit category, stdout/stderr **byte counts**, and a
   fingerprint over those categorical fields. URLs, query, Bearer tokens,
   provider format tokens, argv, raw tool text, and filesystem paths are never
   logged. Stderr is classified in memory, then dropped.
3. **Durable cancel.** `POST /api/v1/media/download-jobs/{id}/cancel` uses the
   same parent Bearer boundary as GET. Malformed, unknown, and unauthorized
   remain `DOWNLOAD_JOB_NOT_FOUND`. Queued jobs become `cancelled` without
   starting a tool. Downloading jobs set `cancel_requested_at`; the worker
   heartbeat kills the process group and commits a PR9-readable terminal row
   (`public_state=expired`, `progress_stage=cancelled`,
   `cancel_requested_at` set). Ready / failed / expired cancel is a no-op
   (artifacts already published are not deleted).
   Cancel does not consume retry budget and cannot become `DOWNLOAD_TOOL_FAILED`.
   `cancel_requested_at` records the in-flight request. The public API projects
   that encoding as `state=cancelled` / `progressStage=cancelled`. CHECK
   constraints allow `cancel_requested_at` only for `downloading` (request) or
   the cancelled encoding (`expired` + `progress_stage=cancelled`); the
   encoding requires it. `complete_cancelled()` updates only when owner/fence
   match **and** `cancel_requested_at IS NOT NULL`. Worker shutdown and lease
   loss requeue or leave reclaim — they never synthesize a user cancel.
4. **Public progress.** `progressStage` is a sanitized enum (queued →
   inspecting → downloading_video / downloading_audio → muxing → verifying →
   publishing → retrying / ready / failed / cancelled / expired). No
   percentages. No internal failure class. Fenced transitions; terminal stages
   cannot return to an active stage.
5. **Previous-application compatibility.** `progress_stage` keeps a server
   default for INSERT. A BEFORE INSERT/UPDATE trigger maps an illegal
   `(public_state, progress_stage)` pair from `public_state` only when the
   writer omitted the column (INSERT default `queued`) or left `progress_stage`
   unchanged (PR9 UPDATE). Explicit illegal pairs still fail
   `ck_media_download_jobs_progress_state`. Legal PR10 cancellation encoding
   (`expired` + `cancelled` progress + `cancel_requested_at`) is not rewritten.
   Application rollback to the PR9 image against schema 0004 remains valid:
   PR9 `MediaDownloadJobState` can construct the stored `public_state`.
6. **Browser.** Versioned `fetchnow.media-flow.v2` session; v1 keys are
   discarded. Accessible stage copy, `Cancel task` (server) vs `Start over`
   (local reset), default highest eligible ≤720p. The browser rejects
   incoherent `state`/`progressStage` pairs fail-closed.

## Consequences

- Alembic head becomes `0004_download_observability` (revises `0003_download_jobs`).
- Existing PR6–PR9 rows map `progress_stage` from `public_state`. Cancellation
  is never stored as a new `public_state` value, so downgrade drops the PR10
  columns without a semantic rewrite of a new enum.
- Operators diagnose stage, retryability, attempt count, cancel, and lease
  without reading URLs or stderr. See [logging-and-privacy](../security/logging-and-privacy.md)
  and [logs and diagnostics](../infrastructure/14-logs-and-diagnostics.md).

## Related documents

- [ADR 0010](0010-durable-download-execution-and-private-artifact-boundary.md)
- [ADR 0013](0013-bounded-media-muxing.md)
- [ADR 0012](0012-browser-download-flow.md)
- [Error codes](../api/error-codes.md)
- [Browser download flow](../api/browser-download-flow.md)
