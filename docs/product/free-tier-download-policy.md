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

## Separate follow-up: quota (PRD1E-B2)

The target Free policy is three successful downloads in a rolling 24-hour
window. It is not implemented by B1. B2 requires a new additive database
migration because the current per-flow bearer token in `sessionStorage` and
short-lived browser delivery grants are not stable anonymous identities.

B2 should issue an opaque random browser identity in an HttpOnly cookie where
practical and persist only its domain-separated hash and minimal accounting
data. IP may be an abuse signal but must not be the entitlement key.

Admission must atomically reserve capacity before enqueue so parallel requests
cannot exceed the limit. A reservation becomes consumed only when the fenced
download job reaches `ready`; terminal failure, cancellation, or expiry releases
it. This preserves the product rule that provider failures do not burn quota
while rejecting excess work before it reaches a worker. The API should expose
only safe policy state: tier, limit, used, remaining, and reset time.

## Separate follow-up: delivery rate (PRD1E-B3)

The target Free delivery rate limit is not implemented by B1 and no commercial
bytes-per-second value has been selected. B3 should add a validated configurable
rate to a central effective-download-policy object and pace the dedicated
delivery iterator using a monotonic clock. Acquisition by yt-dlp must remain
unthrottled so workers release leases promptly.

Delivery pacing must preserve `Content-Length`, full and single-Range 200/206
responses, disconnect cleanup, file-descriptor closure, semaphore ownership,
and browser-grant authorization. A future Premium policy can represent an
unlimited or higher delivery rate without changing the worker pipeline.

## Future Premium boundary

Premium and Robokassa are outside B1–B3. Future entitlement resolution should
produce one effective policy containing the download limit, delivery rate, and
allowed combined/audio-only/video-only products. Download, grant, and delivery
code should consume that policy instead of scattering tier branches.
