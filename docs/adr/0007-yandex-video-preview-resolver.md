# ADR 0007 — Yandex Video Preview resolver

Status: Accepted for PR3B (not a deployment claim)

## Context

PR3A shipped a generic wrapper-resolution foundation without production
resolvers. Users paste Yandex Video Preview links that embed a supported
provider URL (VK Video / Rutube). PR3B adds the first production resolver and
wires `POST /api/v1/media/resolve`.

## Decision

### Exact URL contract

Only:

```text
https://yandex.ru/video/preview/<1..32 decimal digits>
```

Rejected without broadening coverage: `www.yandex.ru`, `ya.ru`, regional hosts,
HTTP, credentials, non-default ports, non-numeric IDs, extra path segments,
lookalike hostnames.

Yandex is **not** a `ProviderDescriptor`. It is registered only in
`WrapperResolverRegistry`.

### Live markup (observed 2026-08-11)

Current public preview pages are **not** dups-only:

- `preloadedState.clips.dups` and `preloadedState.viewer.clips.dups` may be empty.
- Preview binding is evidence-derived from a `div[data-state]` JSON blob where
  root `location` equals `/video/preview/<preview_id>` and
  `preloadedState.viewer` is present.
- `preloadedState.viewer.internal.videoId` exists but may be an empty string on
  live pages; when non-empty it must equal the submitted preview ID.
- The stable provider source is inside one trusted Yandex static player iframe:
  - attribute: `iframe[src]`
  - protocol-relative or HTTPS
  - exact host `yastatic.net` (no lookalikes, no suffix matches)
  - path regex (VK only for this correction):
    `^/video-player/(0x[0-9a-fA-F]{8,24})/pages-common/vk/vk.html$`
  - no credentials; default HTTPS port only; HTTP iframe URLs rejected
  - **iframe URL is never fetched** and `yastatic.net` is not added to the
    DocumentFetchPort allowlist
- Payload nesting to the stable source (local string decode only):

```text
iframe[src] fragment → field "counters" → JSON object → "videoUrl"
```

Observed live `videoUrl`: `http://vk.com/video-161264992_456240043` (scheme
upgraded locally to HTTPS; host preserved). Nested fragment `html` may contain
a `/video_ext.php` decoy and must never become the terminal result.

Older script-JSON `dups[preview_id].player.videoUrl` markup remains supported
as a compatibility tier, not as the current live format claim.

### Extraction precedence

1. Exact `dups[preview_id]` / matching `videoId` target record (compatibility).
2. Viewer-page binding via `data-state` (`location` and/or non-empty matching
   `viewer.internal.videoId`).
3. Exactly one trusted `yastatic.net` VK player iframe (duplicates with the same
   structural identity may collapse).
4. Bounded decode of fragment field `counters` → exact `videoUrl`.
5. Existing stable-provider normalization + ProviderRegistry + URLValidator.

If both tiers return the same canonical → success (dups strategy preferred when
both succeed). If they return different stable canonicals →
`wrapper_unresolved` / `AMBIGUOUS_TARGET_RECORD`.

No page-wide URL scan, meta fallback, or “first supported host” selection.

### Viewer-ID / page binding

Parsed with stdlib `HTMLParser(convert_charrefs=True)` under tag/attribute/
attribute-byte budgets. JSON is decoded only for `data-state` values.

Internal reasons (process-local tokens): `TARGET_VIEWER_STATE_NOT_FOUND`,
`TARGET_VIEWER_ID_MISMATCH`, `AMBIGUOUS_VIEWER_STATE`.

### Trusted iframe / payload errors

`TARGET_PLAYER_NOT_FOUND`, `AMBIGUOUS_TARGET_PLAYER`,
`TARGET_PLAYER_PAYLOAD_INVALID`, `TARGET_VIDEO_URL_NOT_FOUND`,
`PARSER_LIMIT_EXCEEDED`.

Trusted iframe URLs must be **query-free**. Any non-empty URL query →
`TARGET_PLAYER_PAYLOAD_INVALID` (query is not parsed or ignored). Payload lives
only in the fragment.

Fragment parsing uses a shared helper with:

```python
parse_qsl(
    fragment,
    keep_blank_values=True,
    strict_parsing=True,
    encoding="utf-8",
    errors="strict",
    max_num_fields=_MAX_QUERY_FIELDS,  # 32
)
```

Exceeding `max_num_fields` → `PARSER_LIMIT_EXCEEDED`. Other `ValueError` /
malformed pairs / empty or illegal field names / control characters in names /
incomplete percent escapes (`%` / `%X`) → `TARGET_PLAYER_PAYLOAD_INVALID`.
Literal `%` (for example CSS `100%`) and non-hex residues such as `%ZZ` are
allowed in non-authoritative fields.

Exactly **one** fragment field named `counters` (case-sensitive). Missing →
`TARGET_VIDEO_URL_NOT_FOUND`. Two or more (even identical) →
`AMBIGUOUS_TARGET_PLAYER`. Variants such as `Counters` / `counters[]` do not
satisfy the contract.

Decode-pass semantics after `parse_qsl`'s single percent-decode:

- HTML entity unescape ≤ `_MAX_DECODE_PASSES` (3)
- nested JSON-string layers ≤ `_MAX_DECODE_PASSES` (3)
- no additional `unquote` loops

`counters` JSON must be a top-level mapping. Exact top-level `videoUrl` only
(no recursive search). Before reading it, recursive structural validation
enforces depth ≤ `_MAX_JSON_DEPTH` (8, root depth=1) and nodes ≤
`_MAX_JSON_NODES` (4096); oversize → `PARSER_LIMIT_EXCEEDED`. Long keys/strings
fail closed. `RecursionError` / `JSONDecodeError` / `UnicodeError` map to typed
resolution errors. Structural identity digests counters bytes (SHA-256 prefix)
and never logs raw fragment/query/video URL.

### Authoritative fields

- Dups tier: `player.videoUrl`, then stable record `url`.
- Iframe tier: fragment `counters.videoUrl` only.

Never terminal: `embedUrl`, `playerUri`, iframe destination, `/video_ext.php`,
signed query player URLs, thumbnails, analytics, related URLs, arbitrary
`href`/`src`/`contentUrl`.

### Stable provider identity

- VK: `/video<owner>_<clip>` or `/video-<owner>_<clip>` (+ optional slash);
  `/video--…` rejected.
- Rutube: `/video/<id>/` (dups-tier / registry only; no speculative Rutube
  iframe path in this correction).

HTTP→HTTPS string upgrade only for authoritative stable provider URLs (no HTTP
fetch). Query/fragment dropped from canonical.

### Strategies / metadata

- `yandex_target_video_url` — dups tier
- `yandex_viewer_iframe_video_url` — viewer + iframe tier

Hop metadata bools only: `target_bound`, `scheme_upgraded`, and
`iframe_payload` when applicable. No IDs/URLs/hosts/field names.

### Structural limits (selected)

HTML tags ≤ 2048, attributes ≤ 8192, attr length ≤ 8192, attr bytes ≤ 256 KiB,
data-states ≤ 16, inspected iframes ≤ 8, iframe src ≤ 4096, fragment/query
fields ≤ 32, field value ≤ 4 KiB, JSON depth ≤ 8, nodes ≤ 4096, decode passes
≤ 3, candidate URL ≤ 2048. Exceeding budgets fails closed.

### Fixture provenance

Offline fixtures minimize public structure for preview
`17386519177293757127`: live-shaped `data-state` + trusted iframe path, plus a
separate dups compatibility fixture. Volatile build/hash/reqid values are
placeholders. Not a full page snapshot; CI stays offline.

### API / lifespan

`POST /api/v1/media/resolve` unchanged in privacy contract. `/media/validate`
and `/media/probe` unchanged. One shared DNS resolver and one `SafeHTTPClient`
per lifespan; fallback must reuse `app.state.dns_resolver` or fail closed.

## Residual risks

- Yandex may change `data-state` / iframe path / fragment field names; CI is
  offline; live smoke is manual-only (`FETCHNOW_LIVE_YANDEX_SMOKE=1`).
- Residual DNS TOCTOU on document fetch (ADR 0005).
- PR3B is not claimed as deployed by this ADR.

## Related

- [ADR 0006 — wrapper resolution foundation](0006-wrapper-resolution-foundation.md)
