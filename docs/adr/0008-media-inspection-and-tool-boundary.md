# ADR 0008: Media inspection foundation and yt-dlp tool boundary

- Status: Accepted (hardened correction)
- Date: 2026-08-12
- PR: PR4 (`feat/pr4-media-inspection-foundation`)

## Context

FetchNow can validate, probe, and resolve VK/Rutube (and Yandex Preview → provider)
URLs, but still must not download media. Backend/worker code needs a way to obtain
**bounded metadata and server-controlled quality options** before job queue work
(PR5) and download/ffmpeg pipelines (later).

yt-dlp is powerful: it follows redirects, selects extractors, and can write files.
Treating a once-validated URL as sufficient SSRF protection is unsafe because the
tool performs its own network I/O outside `SafeHTTPClient`. Hostname equality alone
is also insufficient: same-host results for a different media identity must fail
closed.

## Decision

1. Add an internal package `fetchnow.media_inspection` with immutable models,
   typed errors, a **sealed** provider-scoped extractor registry (factory-only
   construction, `MappingProxyType` indexes, frozen binding snapshots), and
   `MediaInspectionService` that accepts only a consistent `ResolutionResult`
   validated snapshot (never a raw unvalidated URL).

2. Bind inspection to stable provider media identity (VK `/video(-?)owner_id`,
   Rutube `/video/<id>/`). yt-dlp output must present authoritative
   `webpage_url` / `original_url` values that resolve to the **same** identity;
   payload `id` must agree. Cross-alias hosts for the same identity may be
   accepted; different media on the same host is rejected. Final canonical URLs
   always come from the trusted target, never from extractor substitution.

3. Invoke yt-dlp **only** as a pinned CLI dependency via a hardened subprocess
   adapter (`shell=False`, fixed argv, no user flags, metadata-only /
   `--skip-download`, no playlists/cookies/netrc/config/plugins, isolated
   temp/cache, sanitized env, bounded stdout/stderr, structural JSON limits,
   hard timeout with process-group kill and explicit child reap). Prefer the CLI
   process boundary over importing yt-dlp library code into the API process.

4. Keep the adapter **disabled by default** (`MEDIA_INSPECTION_ENABLED=false`).
   Production must set a trusted absolute executable path that is a regular,
   non-symlink, non-group/world-writable file owned by root or the process
   user. Residual validate/exec TOCTOU remains documented.

5. Do **not** add a public inspection/download HTTP endpoint in PR4. API request
   handlers must not run yt-dlp inline. Manual smoke lives in
   `backend/scripts/media_inspection_live_smoke.py` behind
   `FETCHNOW_LIVE_MEDIA_INSPECTION=1`.

6. Format option IDs are order-independent digests of normalized properties
   (including a domain-separated digest of the provider format token). Free tier
   remains progressive A/V at ≤720p only. `METADATA_CONCURRENCY` is **not**
   enforced in PR4 (deferred to worker wiring).

7. Public-safe `MediaMetadata` / `MediaFormat` models never carry direct/signed
   CDN URLs, thumbnails, descriptions, raw yt-dlp payloads, or provider format
   tokens.

## Consequences

- CI remains offline: tests use a fake process runner and minimized fixtures.
- Compose staging contracts are unchanged; inspection env is documented in
  `.env.example` files but not required for current API/worker boot.
- Successful extraction fails closed if workspace cleanup cannot complete.
  Cleanup failure must not replace an already-propagating
  `InspectionError`, `CancelledError`, or other primary exception.
- `MediaInspectionService` re-parses `validated.url.canonical` offline and
  requires exact coherence with structured `NormalizedMediaURL` fields before
  registry resolution or subprocess invocation. Query/fragment on public or
  structured canonical is rejected without strip-and-accept.
- Failure paths always SIGKILL the captured process group even when the direct
  child already has a returncode (descendants may still hold inherited pipes).
  Residual risk: after the group is gone, kernel PID/PGID reuse could make a
  late `killpg` theoretically target an unrelated group; we only kill the pgid
  captured at spawn and only on failure paths.
- Worker/job wiring, download tickets, ffmpeg muxing, and public quality APIs
  remain subsequent PRs.
