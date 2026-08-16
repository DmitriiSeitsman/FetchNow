# Browser support (PR8, PR14)

The landing page works in current evergreen browsers. **Downloading a prepared
artifact** uses a same-origin anchor after the server issues a short-lived
HttpOnly delivery cookie (PR14). That path works in any browser that supports
normal same-origin navigation with cookies, including Safari and Firefox.

**Save as…** (optional) uses the File System Access API:

`window.showSaveFilePicker`

That API is available in Chromium-based desktop browsers (Chrome, Edge, recent
Opera). Its absence is not an error: the **Download file** link still works when
browser delivery is enabled and the page is served over HTTPS (or on
`localhost` / `127.0.0.1`).

The ready screen shows the actions the current browser can actually perform:

- **With a save picker** — **Save as…** is the lead button (the user chooses the
  folder) and **Download file** stays available as a secondary action, so a
  cancelled or failed picker never leaves the artifact unreachable;
- **Without a save picker** — **Save as…** is not rendered at all and
  **Download file** is the single primary action. There is no disabled button
  and no "requires a Chromium desktop browser" note, because there is no
  control left to explain.

Unsupported or unavailable contexts:

- **Insecure remote HTTP** — the UI shows an HTTPS-required message and never
  claims download works;
- **No File System Access API** — Save as… is not rendered; Download file is the
  only action;
- users can still submit a URL and wait for inspection/download to become ready
  if server flags are on;
- may finish downloading later in another tab until `expiresAt`.

There is no Blob fallback, no query-token download, and no service-worker
credential proxy. Save as… uses `showSaveFilePicker({ suggestedName })` with the
server `suggestedFilename`. Unicode names rely on RFC 8187 `filename*` in
`Content-Disposition`; the ASCII `filename=` parameter is a safe fallback and
must not weaken the browser check.

Native download never puts the parent Bearer token in the href, query, or
fragment.
