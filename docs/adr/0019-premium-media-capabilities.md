# ADR 0019 — Premium source-native media capabilities

Status: Accepted (PRD2-A3.2)
Date: 2026-09-07

## Decision

`MediaKind` separates the requested artifact semantics from its container:
`normal_video` contains video and audio, `video_only` contains only video, and
`audio_only` contains only audio. Free admits only `normal_video`; an active
Premium entitlement admits all three when the inspected source provides the
requested tracks in a supported source container.

Inspection publishes opaque standalone options with `requiresPremium=true`.
Provider URLs and format tokens remain internal. Admission resolves the
server-known option, applies the effective policy, and stores media kind,
source selection, container, capability flags, entitlement-time policy, and
the authorized anonymous identity in the existing JSON snapshot. Workers and
delivery use that immutable snapshot and do not query payment or entitlement
state. Historical snapshots without A3.2 fields retain normal-video semantics.

Normal video keeps its existing direct-or-stream-copy-mux behavior. Video-only
and audio-only each download one exact source token and never enter the mux
planner. No path strips tracks, converts codecs, changes quality, or encodes.
In particular, audio-only means the actual source format and does not promise
MP3. Artifact MIME and filename suffixes are derived from the semantic kind and
actual container.

Premium admissions bypass Free quota accounting and have no FetchNow
product-rate cap. Free normal video retains its rolling quota and configured
524288-byte/second delivery policy. Existing infrastructure safety ceilings,
leases, cleanup, ownership, Bearer authorization, and browser grants are
unchanged.

## Consequences

- Authorization denial (403) is distinct from source unavailability (422).
- A standalone job admitted while Premium is active survives later entitlement
  expiry for the same anonymous identity; new admissions use the current tier.
- The current web client parses the additive metadata but continues to render
  only Free normal-video options. Premium UI and checkout changes are deferred.
- No relational schema change or migration is required.
