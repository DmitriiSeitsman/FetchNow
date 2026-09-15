# Browser support (PR8, PR14, PRD2-A4.2.1)

The landing page works in current evergreen browsers. **Downloading a prepared
artifact** uses a same-origin anchor after the server issues a short-lived
HttpOnly delivery cookie (PR14). That path works in any browser that supports
normal same-origin navigation with cookies, including Safari and Firefox.

A4.2.1 removes the optional Chromium-only File System Access «Сохранить как…»
action. READY exposes a single ordinary browser-download control
(«Скачать бесплатно» / «Скачать снова»). The browser decides whether to save
automatically or ask for a location. Backend Bearer delivery routes remain
available for non-browser clients; the web UI no longer depends on
`showSaveFilePicker`.

Unsupported or unavailable contexts:

- **Insecure remote HTTP** — the UI shows an HTTPS-required message and never
  claims download works;
- users can still submit a URL and wait for inspection/download to become ready
  if server flags are on;
- may finish downloading later in another tab until `expiresAt`.

There is no Blob fallback, no query-token download, and no service-worker
credential proxy. Unicode names rely on RFC 8187 `filename*` in
`Content-Disposition`; the ASCII `filename=` parameter is a safe fallback and
must not weaken the browser check.

Native download never puts the parent Bearer token in the href, query, or
fragment.
