# ADR 0016 — Browser-native delivery grants

Status: Accepted (PR14) · Amended for capability-led action prominence
Date: 2026-08-14

## Context

PR7 delivery requires `Authorization: Bearer <parent accessToken>`. Native
`<a href>` navigation and the browser download manager cannot attach that
header. Putting the parent token (or a raw delivery ticket) in a URL leaks
through logs, history, referrers, and screenshots.

Earlier drafts considered Chromium’s File System Access API
(`showSaveFilePicker`) as an optional Save path. Making FSA primary produced
Firefox/Safari dead ends; A4.2.1 removes FSA from the product UI entirely.

## Decision

Introduce a short-lived **browser delivery grant**:

1. While a download job is `ready` + `artifactReady`, the browser uses the
   existing parent Bearer token to `POST …/download-jobs/{id}/browser-grants`.
2. The API authorizes exactly as PR7 delivery does, then persists only a
   domain-separated SHA-256 of a fresh 32-byte grant token
   (`fetchnow:v1:browser-delivery-grant\0 || raw`).
3. The raw token is returned solely via
   `Set-Cookie: __Secure-fetchnow_delivery=…; Secure; HttpOnly; SameSite=Strict;
   Path=/api/v1/media/browser-grants/{grant_id}/content` (no `Domain`).
4. The JSON body exposes only a clean same-origin `downloadPath` and
   `expiresAt` — never the raw token, parent Bearer, hash, artifact id, or URL.
5. The user clicks a real `<a href="{downloadPath}">`. The browser performs a
   native GET; delivery authenticates grant UUID + cookie, revalidates
   job/artifact/fence, and streams through the existing `ArtifactReader`.
6. Grant TTL is `min(configured TTL, artifact/job expiry)`. A small bounded
   number of concurrent active grants per job is allowed. Grants are not
   single-use (Range / retries); the short TTL and artifact binding are the
   replay boundary.

**Amendment (A4.2.1).** The READY screen no longer offers File System Access
«Сохранить как…». The grant anchor is the single ordinary download control
(«Скачать бесплатно» / «Скачать снова» / Premium equivalent). Transport,
cookie binding, and grant protocol are unchanged.

## Consequences

- Secure cookies require HTTPS outside isolated local tests. Insecure remote
  origins fail closed in the UI.
- Gateway routes only the exact lowercase canonical UUID4 path
  `/api/v1/media/browser-grants/<uuid4>/content` to delivery; grant issuance
  stays on the API. Malformed IDs never reach the delivery DB layer. Non-empty
  query strings are rejected at the gateway (`$args`) and again in delivery
  before cookie/semaphore/DB. Browser URL fragments are not transmitted on the
  wire and are rejected by frontend `downloadPath` parsing. The grant location
  uses a sanitized access log (`$method $uri …`) that never records `$request`,
  `$args`, Cookie, or Authorization; other routes keep the global `main` format.
- Feature flag `MEDIA_BROWSER_DELIVERY_ENABLED` defaults false on api,
  delivery, and worker.
- Client refresh uses a positive bounded delay derived from remaining TTL so
  a 30s grant (equal to the reissue buffer) cannot `setTimeout(0)`-loop.
  Visibility/focus/pageshow and click handlers fail closed on stale hrefs;
  grant retry re-POSTs without recreating the download job.
- Delivery authorization uses a plain immutable SELECT (no `FOR UPDATE`);
  issuance may still lock when enforcing the active-grant cap. Auth is decided
  before streaming opens the artifact FD (same TOCTOU class as artifact expiry).
- Grant issuance/delivery failure logs are fixed catalog outcomes only — never
  exception text, SQL parameters, raw tokens, or hashes.
- Embedded webviews that block native downloads may need “open in system
  browser”; we do not claim support for hostile webviews.
- Migration `0006_browser_delivery_grants` is online-expand compatible with
  previous applications that ignore the new table.
