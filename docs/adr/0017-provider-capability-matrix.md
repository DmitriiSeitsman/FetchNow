# ADR 0017 — Provider capability matrix

Status: Accepted (PR-A, PR-B public projection)
Date: 2026-08-16

## Context

Provider hostname ownership, extractor bindings, and product download policy
previously lived in separate layers. The public API and worker both enforce
download eligibility, but neither had a shared provider-scoped operation
policy. That made a future provider-specific restriction easy to apply at one
boundary and miss at another.

Extractor support is not product support: yt-dlp may report an audio-only
format even when FetchNow does not offer audio extraction. Likewise, a provider
may support quality selection without each media item having every resolution.

## Decision

`fetchnow.capabilities` owns an immutable typed matrix keyed by the existing
`ProviderID` enum:

- `ProviderRegistry` continues to own hostname allowlists and operator
  enablement.
- `InspectionExtractorRegistry` continues to own extractor bindings.
- `ProviderCapabilityRegistry` owns product states for operations, content
  kinds, and metadata fields.

Only `CapabilityState.ENABLED` permits execution. `DISABLED` and `PLANNED`
both fail closed in runtime paths. The initial matrix preserves the existing
download-video and quality-selection behavior for VK, Rutube, and OK.ru. Audio
extraction, container selection, live streams, and playlists are disabled;
OK.ru has no separate clip family so `clip` is disabled for that provider;
thumbnails are planned because the current extraction contract does not expose
them. OK.ru progressive public options currently depend on bounded muxing of
DASH webm A/V (`MEDIA_MUXING_ENABLED`) because named progressive MP4 rows from
the odnoklassniki extractor lack codecs/dimensions.

`DownloadJobService.create` validates `DOWNLOAD_VIDEO` before enqueueing.
`DownloadExecutor._resolve_selection` validates it again after rebuilding the
persisted target and before inspection. The latter check protects jobs queued
before a policy change or claimed by a worker on a different deployment
revision.

Unknown providers and non-enabled operations return
`PROVIDER_CAPABILITY_DISABLED` rather than allowing a fallback path.

### Public projection (PR-B)

Media-inspection and download job responses expose a top-level
`providerCapabilities` field computed at serialization time from the job row’s
`provider_id` via a soft registry lookup (`get`, never `require` on read paths).

- Known providers: a camelCase object with complete `operations`,
  `contentKinds`, and `metadata` maps (`enabled` / `disabled` / `planned`).
  Wire keys are owned by `fetchnow.capabilities.public` and are not a direct
  serialization of internal enums or `ProviderCapabilities`.
- Unknown / unsupported / no public profile: JSON `null` (not an all-disabled
  object — that would imply a known provider with explicitly forbidden
  capabilities).
- The field is never stored in `result` / JSONB. Per-item formats remain in
  `result.formats`.
- Browser-grant, content delivery, and validate/probe/resolve endpoints are
  unchanged. Frontend consumption is deferred to PR-C.

## Consequences

- The matrix is code-owned rather than stored in PostgreSQL: it changes with
  extractor and product code, has no runtime editor, and needs no migration.
- Public job JSON gains an additive `providerCapabilities` key; clients that
  reject unknown keys (current web allowlists) must update in PR-C before
  relying on the public media flow against a PR-B backend.
- Per-media formats and resolutions remain `MediaMetadata.formats`; the static
  matrix never promises a resolution or container for a particular item.
- Rutube keeps the same matrix as VK after a live metadata probe of a public
  video (progressive MP4 present; no audio-only/container choice observed;
  title/duration present; thumbnails still unused by FetchNow). Shorts URLs are
  an identity alias of `/video/{id}/`. Legacy `yappy.media` / `rutube.ru/yappy/…`
  are unsupported (broken or non-media), not a third provider.
- Live end-to-end download reliability on staging remains an operator smoke
  concern and is not claimed by this ADR alone.
