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
responses contain only finished combined choices; provider tokens, URLs, and
standalone source streams remain internal. Free has no separate 720p product
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
`downloadsRemaining`, and `resetAt`. Reservations reduce remaining capacity but
cannot promise a reset time. Thus used=2/reserved=1 reports remaining=0 and
`resetAt=null`; used=3/reserved=0 reports the oldest counted success plus 24
hours. PostgreSQL time is authoritative for cutoff, reservation expiry,
consumption, and reset calculation.

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
representation can describe a future unlimited Premium policy without adding
Premium entitlement behavior now.

The shared Bearer and browser-grant iterator paces every chunk, including the
first, with a monotonic virtual-finish deadline. Full and single-Range 200/206
responses preserve `Content-Length`, `Content-Range`, resume, disconnect
cleanup, file-descriptor closure, semaphore ownership, and grant authorization.
Repeated short Range requests therefore do not receive a free initial chunk.

The limit is deliberately per response. Parallel responses can achieve roughly
N times the configured rate, bounded only by process-local delivery concurrency
and infrastructure. Aggregate identity/IP shaping is not claimed or implemented.

## Future Premium boundary

Premium and Robokassa are outside B1–B3. Future entitlement resolution should
produce one effective policy containing the download limit, delivery rate, and
allowed combined/audio-only/video-only products. Download, grant, and delivery
code should consume that policy instead of scattering tier branches.
