# Browser support (PR8)

The landing page works in current evergreen browsers. **Saving a prepared
artifact** currently requires the File System Access API:

`window.showSaveFilePicker`

That API is available in Chromium-based desktop browsers (Chrome, Edge, recent
Opera). It is not used on Safari or Firefox in this PR.

Unsupported browsers:

- can still submit a URL and wait for inspection/download to become ready if
  server flags are on;
- see a clear message instead of a broken save;
- do **not** trigger the content fetch;
- may finish saving later in a supported browser/tab until `expiresAt`.

There is no Blob fallback, no query-token download, and no service-worker
credential proxy in PR8. Save uses `showSaveFilePicker({ suggestedName })`
with the server `suggestedFilename`. Unicode names rely on RFC 8187
`filename*` in `Content-Disposition`; the ASCII `filename=` parameter is a
safe fallback and must not weaken the browser check.
