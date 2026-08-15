# Browser download flow (PR8, PR14)

The static Astro page can orchestrate inspection → download → browser-native
delivery when `PUBLIC_MEDIA_FLOW_ENABLED=true` **and** the server flags for jobs,
inspection, downloads, delivery, and browser delivery are also enabled by an
operator.

## Sequence

1. Browser generates a 43-character unpadded base64url access token (32 random bytes, Web Crypto).
2. `POST /api/v1/media/jobs` with `Authorization: Bearer` and `{ "url": "…" }`.
   Responses include top-level `providerCapabilities` (product policy for the
   resolved provider) or `null` when the provider has no public profile. This
   is not stored inside `result`.
3. Poll `GET /api/v1/media/jobs/{id}` until `inspected` / `failed` / `expired`.
4. User selects a `formatOptionId` that is progressive, has video+audio, and `freeTierEligible` (direct file **or** a server-derived muxed option when muxing is enabled).
5. `POST /api/v1/media/jobs/{id}/downloads` with `{ "formatOptionId": "…" }`.
   Download job JSON also carries top-level `providerCapabilities` (same shape).
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
8. When the job is `ready`, the UI calls
   `POST /api/v1/media/download-jobs/{id}/browser-grants` with the parent Bearer
   token and `credentials: "same-origin"`. The response is `{ downloadPath,
   expiresAt }` only; the server also sets `Set-Cookie:
   __Secure-fetchnow_delivery=…` (HttpOnly). While this request is in flight
   the UI shows “Preparing secure download…”.
9. The primary action is a real same-origin anchor:
   `<a href="/api/v1/media/browser-grants/{uuid}/content" download>Download
   file</a>`. Navigation uses the cookie; the parent token is never placed in
   the URL. After the user clicks, the UI shows “Sent to your browser” and keeps
   a “Download again” link. The session is not cleared merely because the link
   was clicked, and the UI does not fake byte progress after browser handoff.
   Grants are reissued near expiry; stale grant responses are ignored when a
   newer generation is active.
10. Optional **Save as…** (Chromium desktop with File System Access API) still
    streams `GET …/download-jobs/{id}/content` with the same Bearer token into
    `showSaveFilePicker`. Credentials are cleared only after a clean FSA close.

The parent token is never placed in the URL, filename, DOM, or logs.

## UI presentation (PR11, PR14)

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
  Unknown denominators stay indeterminate (`…`), never a fake `0%`.
  The browser poller resets backoff when state/stage/percent/attempt/`updatedAt`
  change and caps active download polls at ≤1s on a visible tab; unchanged
  queued responses may back off further. Ready is shown as soon as the
  terminal poll arrives — progress is best-effort, not fake-smooth.
  Verifying, muxing, and publishing are not byte percentages. `ready`
  shows the exact prepared `artifactBytes` without an approximate sign.
  Pre-download quality rows show `≈` size or “Size available after
  preparation”. `ready` reaches 100 when server-side preparation is complete;
  `saving` remains at 100 with an active spinner while the browser writes via
  File System Access. A retry starts a new preparation attempt and may reset the
  stage completion value. Idle, error, cancelled, and expired states never render
  as active progress, and a direct progressive job may go from
  `downloading_video` straight to `publishing`.
- Native download uses the grant `downloadPath` exactly as returned (no query,
  fragment, or extra path). The gateway and delivery reject non-empty query
  strings on the grant content route. Browser fragments are not sent on the
  wire. Save as…
  uses `suggestedFilename` (sanitized title + validated container). The FSA path
  parses `Content-Disposition` `filename*` (RFC 8187) and fail-closes on
  mismatch. This is not a trusted original provider filename.
- The spinner lives inside the progress card and is only rendered while an
  operation is in flight. `prefers-reduced-motion` disables its animation.
- On insecure remote origins (HTTP that is not `localhost` / `127.0.0.1`), the
  UI shows an HTTPS-required message and never claims native download works.

## Why anchor navigation (PR14)

PR14 issues a short-lived HttpOnly cookie scoped to the grant content path.
Same-origin `<a href>` navigation can deliver the artifact without putting the
parent Bearer token in the URL and without buffering the file in JavaScript.

## Why not Blob?

Artifacts may approach `MEDIA_DOWNLOAD_MAX_BYTES` (default 3 GiB). `blob()` / `arrayBuffer()` would buffer the file in JS memory.

## Browser support

See [browser support](../product/browser-support.md).

## Out of scope (PR8)

- No Range/resume UI (PR7 Range behavior is unchanged).
- No cross-replica rate limiting.
- Server flags `MEDIA_JOBS_ENABLED`, `MEDIA_INSPECTION_ENABLED`,
  `MEDIA_DOWNLOADS_ENABLED`, `MEDIA_DELIVERY_ENABLED`, and
  `MEDIA_BROWSER_DELIVERY_ENABLED` stay off until an operator enables them
  independently of `PUBLIC_MEDIA_FLOW_ENABLED`.
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
