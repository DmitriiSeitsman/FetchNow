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
   `cancelled` / `expired`. The UI shows sanitized `progressStage` copy
   (Checking the media… / Preparing video… / …) without percentages.
7. `Cancel task` calls `POST …/download-jobs/{id}/cancel`. `Start over` is a
   local reset and does **not** cancel the server job.
8. User gesture opens `showSaveFilePicker`, then `GET …/content` with the same Bearer.
9. `response.body` is streamed to disk. Credentials are cleared only after a clean close.

The token is never placed in the URL, filename, DOM, or logs.

## Why not `<a href>`?

PR7 requires a Bearer header. Anchor navigation cannot set it. Query tokens are forbidden.

## Why not Blob?

Artifacts may approach 512 MiB. `blob()` / `arrayBuffer()` would buffer the file in JS memory.

## Browser support

See [browser support](../product/browser-support.md).

## Out of scope (PR8)

- No download progress percentage (backend exposes stage, not byte progress).
- No Range/resume UI (PR7 Range behavior is unchanged).
- No cross-replica rate limiting.
- Server flags `MEDIA_JOBS_ENABLED`, `MEDIA_INSPECTION_ENABLED`,
  `MEDIA_DOWNLOADS_ENABLED`, and `MEDIA_DELIVERY_ENABLED` stay off until an
  operator enables them independently of `PUBLIC_MEDIA_FLOW_ENABLED`.
- Muxing (`MEDIA_MUXING_ENABLED`) is independently off. When it is off, or no
  compatible pair exists, the UI shows a neutral “combined file required”
  hint instead of selectable qualities. Source video/audio tokens are never
  shown.

See [ADR 0013](../adr/0013-bounded-media-muxing.md).

Session recovery uses `fetchnow.media-flow.v2`. Incompatible v1 records are
removed. A restored active task is labelled in the UI.
