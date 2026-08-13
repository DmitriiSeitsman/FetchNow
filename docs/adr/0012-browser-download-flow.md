# ADR 0012: Browser orchestration and authenticated streaming save

## Status

Accepted (PR8). The interactive UI is **disabled by default**
(`PUBLIC_MEDIA_FLOW_ENABLED=false`). Server flags
`MEDIA_JOBS_ENABLED`, `MEDIA_INSPECTION_ENABLED`, `MEDIA_DOWNLOADS_ENABLED`,
and `MEDIA_DELIVERY_ENABLED` remain independently off unless an operator
enables them.

## Context

PR7 streams private artifacts only with `Authorization: Bearer <parent token>`.
A normal `<a href>` navigation cannot attach that header. Putting the token in
a query string, fragment, cookie, or filename would expand the credential
surface. Loading a 512 MiB artifact with `blob()` / `arrayBuffer()` would
buffer the whole file in JavaScript memory.

## Decision

1. The Astro static page hosts a framework-free TypeScript state machine that
   talks only to same-origin `/api/` routes.
2. The browser generates the 32-byte access token with Web Crypto and sends it
   as `Authorization: Bearer`. Lost-202 recovery remains a server property of
   that token; the client does not invent a second credential.
3. Saving uses the File System Access API (`showSaveFilePicker`) from a user
   gesture, then streams `response.body` into `FileSystemWritableFileStream`.
   Chunks are written and dropped. Exact `Content-Length` is required. Early
   EOF and excess bytes fail closed. `writable.abort()` runs on failure.
4. Browsers without File System Access API see an honest unsupported message.
   The prepared artifact is left to expire; the client does **not** fetch it
   and does **not** fall back to Blob.
5. A tab-scoped `sessionStorage` recovery record may hold only bounded fields
   (token, job ids, selected option, phase, `expiresAt`) under
   `fetchnow.media-flow.v2`. Schema v1 keys are discarded. Submitted URLs,
   titles, and canonical provider URLs are never persisted. `localStorage` is
   forbidden.

## Consequences

- Chromium desktop is the current save path. Safari/Firefox users can inspect
  media but cannot finish the save until those browsers ship the API or a later
  PR adds a separately reviewed alternative.
- `sessionStorage` is visible to same-origin XSS. CSP on the gateway HTML
  location allows only `'self'` scripts/connect and no `unsafe-eval`.
- There is no download progress percentage (backend exposes job state, not
  bytes). Range resume UI is out of scope; PR7 Range behavior is unchanged.
- Enabling the UI flag does not enable server-side jobs/delivery.

## Alternatives considered

- Query-string or cookie download tokens — rejected; they leak via logs, Referer,
  and history.
- Service-worker credential proxy — rejected without a dedicated security review.
- `response.blob()` fallback — rejected; it loads the entire artifact into memory.
