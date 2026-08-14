# Browser download flow (PR8)

The static Astro page can orchestrate inspection → download → authenticated
save when `PUBLIC_MEDIA_FLOW_ENABLED=true` **and** the server flags for jobs,
inspection, downloads, and delivery are also enabled by an operator.

## Sequence

1. Browser generates a 43-character unpadded base64url access token (32 random bytes, Web Crypto).
2. `POST /api/v1/media/jobs` with `Authorization: Bearer` and `{ "url": "…" }`.
3. Poll `GET /api/v1/media/jobs/{id}` until `inspected` / `failed` / `expired`.
4. User selects a `formatOptionId` that is progressive, has video+audio, and `freeTierEligible` (direct file **or** a server-derived muxed option when muxing is enabled).
5. `POST /api/v1/media/jobs/{id}/downloads` with `{ "formatOptionId": "…" }`.
6. Poll `GET /api/v1/media/download-jobs/{id}` until `ready` / `failed` /
   `cancelled` / `expired`. The UI shows sanitized `progressStage` copy.
   A real byte percentage is shown only during download stages when the
   backend supplies `progressPercent` (observed bytes over an approximate
   expected size). Example: `Downloading file (42%)`. Audio stages use
   `Downloading audio (18%)`. If the denominator is unknown, the label has no
   parentheses. The bar stays stage-based and interpolates inside a download
   stage; it is never timer-faked. The percentage is not a remaining-time
   estimate. Verifying, muxing, and publishing are not byte percentages.
7. `Cancel task` calls `POST …/download-jobs/{id}/cancel`. `Start over` is a
   local reset and does **not** cancel the server job.
8. User gesture opens `showSaveFilePicker`, then `GET …/content` with the same Bearer.
9. `response.body` is streamed to disk. Credentials are cleared only after a clean close.

The token is never placed in the URL, filename, DOM, or logs.

## UI presentation (PR11)

- Quality selection is a dedicated `Choose quality` card with one row per
  normalized quality. Rows are native radios, so grouping and keyboard
  behaviour stay standard.
- The response may expose several formats for the same quality. The UI groups
  them by numeric `height`, or by a strictly normalized `qualityLabel` when the
  height is unknown, and shows one representative per group. The ranking is
  eligible first, then the already selected id, then MP4 before WebM, then the
  higher finite FPS, then the smaller known `approxBytes`, then the
  `formatOptionId`. It is independent of the response order. Representative ids
  are always the opaque ids from the response; the UI never builds one.
- Progress is a stage indicator: the card reports which preparation step the
  backend is on. During `downloading_video` / `downloading_audio`, the label
  may include a real `progressPercent` when a bounded size is known
  (`Downloading file (N%)` / `Downloading audio (N%)`). That `(N%)` is
  observed bytes over an approximate expected size, not remaining time.
  Verifying, muxing, and publishing are not byte percentages. `ready`
  shows the exact prepared `artifactBytes` without an approximate sign.
  Pre-download quality rows show `≈` size or “Size available after
  preparation”. `ready` and `completed` reach 100 when server-side
  preparation is complete; `saving` remains at 100 with an active spinner
  while the browser writes that prepared artifact to the user's file. A retry
  starts a new preparation attempt and may reset the stage completion value.
  Idle, error, cancelled, and expired states never render as active progress,
  and a direct progressive job may go from `downloading_video` straight to
  `publishing`.
- Save uses `suggestedFilename` (sanitized title + validated container). The
  browser parses `Content-Disposition` `filename*` (RFC 8187) and fail-closes
  on mismatch. This is not a trusted original provider filename.
- The spinner lives inside the progress card and is only rendered while an
  operation is in flight. `prefers-reduced-motion` disables its animation.

## Why not `<a href>`?

PR7 requires a Bearer header. Anchor navigation cannot set it. Query tokens are forbidden.

## Why not Blob?

Artifacts may approach 512 MiB. `blob()` / `arrayBuffer()` would buffer the file in JS memory.

## Browser support

See [browser support](../product/browser-support.md).

## Out of scope (PR8)

- No Range/resume UI (PR7 Range behavior is unchanged).
- No cross-replica rate limiting.
- Server flags `MEDIA_JOBS_ENABLED`, `MEDIA_INSPECTION_ENABLED`,
  `MEDIA_DOWNLOADS_ENABLED`, and `MEDIA_DELIVERY_ENABLED` stay off until an
  operator enables them independently of `PUBLIC_MEDIA_FLOW_ENABLED`.
- Muxing (`MEDIA_MUXING_ENABLED`) is independently off. When it is off, or no
  compatible pair exists, the UI shows a neutral “combined file required”
  hint instead of selectable qualities. Source video/audio tokens are never
  shown.

PR12 adds `progressPercent`, `artifactBytes`, and `suggestedFilename` on the
public DownloadJob. Pre-download size is approximate; ready size is exact.
Percentage exists only when a bounded denominator is available. No extra CDN
request is made just to estimate size.

See [ADR 0013](../adr/0013-bounded-media-muxing.md) and
[ADR 0015](../adr/0015-size-progress-filename-trust-boundary.md).

Session recovery uses `fetchnow.media-flow.v2`. Incompatible v1 records are
removed. A restored active task is labelled in the UI.
