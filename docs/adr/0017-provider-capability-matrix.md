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
download-video and quality-selection behavior for VK and Rutube. Audio
extraction, container selection, live streams, and playlists are disabled;
thumbnails are planned because the current extraction contract does not expose
them.

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
- Rutube has the same initial matrix as VK to preserve current behavior. Its
  live download reliability remains unverified and must be evaluated separately
  before any provider-specific product claim.
