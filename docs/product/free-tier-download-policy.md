# Free-tier download policy

## Implemented foundation (PRD1E-B1)

Free users receive ordinary combined video+audio files. When a provider exposes
compatible video-only and audio-only streams, the worker downloads both and
muxes them with ffmpeg stream copy. Muxing is not a Premium capability and no
codec, resolution, bitrate, audio, or video conversion is permitted.

Approved stream-copy outputs are deliberately narrow:

- H.264/AVC video plus AAC audio in MP4.
- VP8, VP9, or AV1 video plus Opus or Vorbis audio in WebM.

Incompatible or unknown pairs fail closed. Direct progressive files remain the
preferred execution path for a quality they already satisfy. Public format
responses identify finished combined choices and source-native standalone
choices without exposing provider tokens or URLs. Standalone choices are
Premium-only at admission and remain hidden by the current Free UI. Free has no separate 720p product
cap: compatible 1080p is eligible when it remains within configured inspection,
duration, file-size, disk, and execution bounds.

Each attempt reserves input video + input audio + output peak space. yt-dlp,
ffmpeg, and ffprobe share the lease watchdog and bounded process runner. The
attempt workspace is removed after success, failure, cancellation, lease loss,
or retry. A publication that cannot be committed under its fence is reclaimed
as an orphan after the configured grace period.

`MEDIA_MUXING_ENABLED` remains fail-closed by default. A later production
activation requires only the worker settings:

```env
MEDIA_MUXING_ENABLED=true
MEDIA_MUXING_FFMPEG_PATH=/usr/bin/ffmpeg
MEDIA_MUXING_FFPROBE_PATH=/usr/bin/ffprobe
```

`PUBLIC_SEARCH_INDEXING_ENABLED=false` remains unchanged. Enabling muxing does
not enable inspection, jobs, downloads, delivery, indexing, or any Premium
state.

## Free rolling quota (PRD1E-B2)

Free policy is three successful downloads in a rolling 24-hour window. Before
download admission, the browser calls `GET /api/v1/media/quota`; the server may
issue `__Host-fetchnow_client`, a Secure, HttpOnly, SameSite=Lax, Path=/ cookie.
Its 32 random bytes are encoded as unpadded base64url. PostgreSQL stores only a
domain-separated SHA-256 hash. The identity and cookie expire absolutely after
one year; activity does not extend them. Expired identities without accounting
entries are removed by bounded worker reconciliation.

The API locks the anonymous client and counts database-timestamped events in the
rolling window. A new idempotent download job and its reservation commit in one
transaction. The unique `(media_job_id, format_option_id)` download key and the
unique quota `download_job_id` ensure retries and concurrent duplicates reserve
only once. Parallel distinct admissions serialize on the anonymous-client row.

A reservation consumes capacity immediately but becomes a counted success only
in the same transaction that the fenced worker transition commits `ready`.
Terminal failure, cancellation, or pre-ready expiry releases it atomically.
Retries retain it. Worker lifecycle and reconciliation honor existing
reservations even when `FREE_DOWNLOAD_QUOTA_ENABLED` is later false; that flag
controls only admission of new quota-governed downloads in the API.

Status returns `tier`, `downloadLimit`, `downloadsUsed`, `downloadsReserved`,
`downloadsRemaining`, `windowSeconds`, `premiumExpiresAt`, and `resetAt`.
Reservations reduce remaining capacity but cannot promise a reset time. Thus
used=2/reserved=1 reports remaining=0 and `resetAt=null`; used=3/reserved=0
reports the oldest counted success plus 24 hours. PostgreSQL time is
authoritative for cutoff, reservation expiry, consumption, and reset
calculation.

This is a best-effort anonymous Free entitlement, not an anti-fraud identity.
Deleting the cookie, private browsing, or using another browser/device can
obtain another identity. IP addresses and invasive browser fingerprints are not
used as the entitlement key. B3 delivery throttling remains separate.

## Free delivery shaping (PRD1E-B3)

Free delivery shaping is implemented in the dedicated delivery process, after
the worker has acquired and published the artifact. Provider acquisition by
yt-dlp and stream-copy muxing remain unthrottled so the worker releases its
lease promptly. Source rollout is fail-closed:

```env
FREE_DELIVERY_RATE_LIMIT_ENABLED=false
FREE_DELIVERY_RATE_BYTES_PER_SECOND=524288
```

The selected production candidate is 512 KiB/s per HTTP response. Validated
rates are bounded to 256 KiB/s through 64 MiB/s. The central effective policy
uses `delivery_rate_bytes_per_second=None` while shaping is disabled; the same
representation also describes the unlimited delivery policy of an active
Premium admission.

The shared Bearer and browser-grant iterator paces every chunk, including the
first, with a monotonic virtual-finish deadline. Full and single-Range 200/206
responses preserve `Content-Length`, `Content-Range`, resume, disconnect
cleanup, file-descriptor closure, semaphore ownership, and grant authorization.
Repeated short Range requests therefore do not receive a free initial chunk.

The limit is deliberately per response. Parallel responses can achieve roughly
N times the configured rate, bounded only by process-local delivery concurrency
and infrastructure. Aggregate identity/IP shaping is not claimed or implemented.

## Premium policy integration (PRD2-A3.1)

The server resolves the A2 entitlement capability into one effective download
policy. Free remains three successful downloads per rolling 86,400 seconds and
512 KiB/s per response. While an entitlement is active, Premium has no download
count limit and no FetchNow product-level delivery rate cap. Infrastructure
safety limits and artifact authorization remain unchanged.

Only Free admissions create quota reservations, so Premium downloads never
increase the later Free rolling count. Existing Free successes remain intact
and resume their normal rolling-window effect when Premium expires. Entitlement
activity is evaluated with PostgreSQL time for each new admission; the resulting
server-generated policy is stored in the download job snapshot. That snapshot
governs subsequent delivery without polling entitlement state mid-stream.

This integration is independent of Robokassa mode. Combined Free-eligible
selection rules remain unchanged. It adds no Premium UI, account, second
cookie, or client-authoritative policy input.

## Premium media capabilities (PRD2-A3.2)

Free continues to admit only ordinary video with audio. An active Premium
entitlement may additionally admit a real source video-only stream or a real
source audio-only stream when inspection exposes one. Availability is derived
from track and codec metadata for the individual media item, not promised by
provider name. The API marks standalone options with `requiresPremium=true`,
but the server enforces the policy from the opaque inspected option at
admission; the browser cannot grant a capability by changing request fields.

Standalone downloads are source-format downloads. Audio-only may therefore be
M4A/AAC, WebM/Opus, Ogg, or another explicitly supported source container; it
is not guaranteed MP3. Video-only preserves the actual source video stream and
does not strip audio from a progressive file. Neither path transcodes. The
existing mux path remains only for producing normal video with audio and uses
ffmpeg stream copy.

The selected format and effective capability policy are frozen into the
server-generated admission snapshot. A Premium standalone job admitted before
entitlement expiry remains authorized for the same anonymous identity while it
is prepared and delivered. New admissions after expiry use the Free matrix.
Premium normal, video-only, and audio-only jobs create no Free quota entry and
have no product delivery-rate cap; Free normal video retains the configured
524288 bytes/second rate. Existing byte, duration, disk, timeout, lease,
ownership, cleanup, and delivery authorization boundaries remain unchanged.

No schema migration or Premium UI is part of A3.2.
