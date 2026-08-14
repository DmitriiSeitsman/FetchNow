# ADR 0015: Size estimates, artifact bytes, filenames, and download percent

## Status

Accepted (PR12). Feature flags are unchanged: downloads, muxing, delivery, and
the browser flow remain independently off by default.

## Context

Inspection often lacks yt-dlp `filesize` / `filesize_approx`. The UI still
needed a compact size hint without extra CDN HEAD/GET. Delivery used
`fetchnow-<uuid>.<ext>`, which is safe but not descriptive. Stage progress
could not show a real download percentage without a bounded denominator.

## Decision

1. **Approximate size.** Estimate `approxBytes` with this precedence:
   reported `filesize`, then `filesize_approx`, then
   `duration × bounded bitrate / 8` (`tbr` for progressive, `vbr` then `tbr`
   for video-only, `abr` then `tbr` for audio-only), then the bounded sum of
   video+audio for a derived mux option. Invalid, non-finite, overflow, and
   over-limit values are dropped rather than advertised. The estimate is never
   presented as exact. No additional CDN request is made just to learn size.
2. **Exact artifact size.** Public `artifactBytes` is the persisted
   `artifact_bytes` and is returned only for `ready` + `artifactReady=true`,
   where it is required and `> 0`. A non-ready row with a persisted
   `artifact_bytes` is `INTERNAL_ERROR`, not a silent null. A ready row
   without a positive size is also `INTERNAL_ERROR`.
3. **Suggested filename.** The name is derived from a server-sanitized
   provider **title**, not a trusted raw provider filename. It is persisted on
   create (`suggested_filename`). Idempotent recovery returns the stored value
   and does not recompute it from later metadata. Invalid/empty titles and
   legacy `null` rows use `fetchnow-<download-job-id>.<container>`. Delivery
   sends `attachment; filename="<ascii>"; filename*=UTF-8''…` (RFC 8187).
   The browser requires `filename*` to decode to the same `suggestedFilename`,
   pass the same sanitizer, and fail-closes on mismatch, duplicates, or
   malformed percent-encoding. Invalid persisted names are `INTERNAL_ERROR`.
4. **Download percent.** Public `progressPercent` is an integer `0..99` or
   `null`. It exists only during `downloading` +
   `downloading_video` / `downloading_audio` when a bounded denominator is
   known (selected / video / audio `approxBytes`). The worker observes
   workspace bytes without following symlinks and never parses yt-dlp stdout.
   `100` is never stored: verifying, muxing, and publishing still follow.
   Updates require the live `lease_owner` + `fence_token`, are monotonic
   inside a stage, and are throttled. Observed percent and persisted percent
   are distinct: a throttled or failed write is not treated as stored.
   Persistence runs on a coalesced writer so a stuck database cannot block
   size or free-disk termination. A false fenced write stops further percent
   updates without becoming a user catalog error. Percent persisted on a
   disallowed state/stage is `INTERNAL_ERROR`, not a silent null. Unknown
   denominators stay `null`; the UI must not invent a percentage.

## Consequences

- Alembic head becomes `0005_download_file_details` (revises
  `0004_download_observability`). New columns are nullable so a previous PR11
  application can INSERT/UPDATE without them. A BEFORE UPDATE trigger clears
  `progress_percent` when `progress_stage` changes and the percent value is
  unchanged (`NEW IS NOT DISTINCT FROM OLD`). PostgreSQL cannot tell an
  omitted column from an explicit assignment of the same value; both are
  normalized to NULL so a rolled-back PR11 application remains
  CHECK-compatible. An explicit write of a *different* illegal percent still
  fails the CHECK. Downgrade drops the trigger, function, constraints, and
  columns and does not rewrite `public_state`.
- The progress bar stays stage-based. Inside a download stage it interpolates
  using the real percent when present. `(N%)` is observed bytes over an
  approximate expected size, not remaining time.

## Related documents

- [ADR 0014](0014-download-observability-cancellation-progress.md)
- [Browser download flow](../api/browser-download-flow.md)
- [Logging and privacy](../security/logging-and-privacy.md)
- [Threat model](../security/threat-model.md)
