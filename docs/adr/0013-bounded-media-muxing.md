# ADR 0013: Bounded server-side media muxing

## Status

Accepted (PR9). The feature is **disabled by default**
(`MEDIA_MUXING_ENABLED=false`). Progressive direct download is unchanged.
ffmpeg/ffprobe paths are worker-only.

## Context

PR4–PR8 only execute a single exact progressive format token
(`yt-dlp -f <token>`). Real Yandex Preview → VK inspection often returns
H.264 video-only plus AAC audio-only streams and no combined file. The UI
then blocked every quality with “muxing is not offered yet.”

## Decision

1. Keep the public `MediaFormat` schema. Derived mux options look like the
   finished progressive artifact (`hasVideo`, `hasAudio`,
   `freeTierEligible`, final container/quality). Source tokens and URLs stay
   on the internal binding model only.
2. Derive mux options only when muxing is enabled **and** no free-tier
   progressive format exists. Pairing is deterministic, order-invariant, and
   limited to stream-copy combinations:
   - MP4: H.264/AVC + AAC → `mp4`
   - WebM: VP8/VP9/AV1 + Opus/Vorbis → `webm`
   Unknown/HEVC/MP3, DRM, non-HTTP protocols, and heights above 720p are
   rejected. No transcoding.
3. `muxingRequired` is false iff at least one executable free option exists
   (direct progressive **or** a derived mux option while the feature is on).
   Otherwise true.
4. Selection is a sealed union: `DirectDownloadSelection` (one exact token)
   or `MuxedDownloadSelection` (exact video token + exact audio token +
   approved container). Each token is validated by the existing atom policy.
   The server never accepts `best`, `+`, `/`, or other selector grammar.
5. Mux execution downloads video then audio into private subdirectories,
   runs a fixed ffmpeg stream-copy argv, validates with bounded ffprobe JSON,
   stages into ``output/`` by same-volume link+unlink (no extra copy), then uses
   the existing atomic publisher. The subprocess boundary connects stdin to
   `DEVNULL`; the ffprobe argv must not use ffmpeg-only `-nostdin`, which Debian
   bookworm ffprobe rejects before validating the local file.
6. Peak disk reservation is `video + audio + muxed output` plus the existing
   min-free headroom, and each mux stage is write-capped to that reservation.
   Mux concurrency is the existing single download worker
   (`MEDIA_DOWNLOAD_CONCURRENCY=1`). There is no cross-worker global disk
   reservation.
7. Empty shared volume startup uses a non-root `storage-init` oneshot that
   calls `ArtifactStore.validate_root()` (mkdir 0700 only when missing; never
   chmod an existing operator root; no symlink follow; no `sleep`). The
   oneshot has `network_mode: none`, no Compose network, and no
   `DATABASE_URL` / Postgres / tool-path env; it does not load application
   Settings. Worker and delivery `depends_on` `service_completed_successfully`
   for local Compose. Release rollout uses `--no-deps`, so the initializer is
   executed explicitly under the rollout lock before api/worker/delivery/web
   and is pinned to the immutable API image ID. Older releases without the
   oneshot remain valid. The image also precreates
   `/var/lib/fetchnow/tmp/downloads` so a first named-volume copy can include
   the directory.
8. No database migration. Previously saved PR8 inspection/download snapshots
   remain readable because the public format schema is unchanged.

## Consequences

- Operators must install ffmpeg/ffprobe in the worker image (Debian
  bookworm `ffmpeg` package) and set absolute paths only on the worker.
- Enabling muxing does not enable transcoding, cookies, plugins, or
  client-controlled argv.
- Video-only / audio-only rows are used internally for pairing only. They are
  never projected as public `MediaFormat` download choices.

## Residual risks

- yt-dlp provider HTTP remains outside `SafeHTTPClient`.
- Executable validate/exec TOCTOU between `stat` and spawn.
- Stream-copy compatibility depends on provider metadata (fail closed on
  probe mismatch; no transcoding fallback).
- No cross-worker global disk reservation.
- Shared image dependency surface now includes ffmpeg/ffprobe.
- No transcoding: incompatible pairs stay unavailable.

## Alternatives considered

- Additive API field `muxingMode` / new format schema — rejected; would
  break stored PR8 snapshots without a migration.
- yt-dlp `video+audio` selector string — rejected; selector grammar must
  never be user-influenced or logged as a combined expression.
- Delivery mkdir of the artifact root — rejected; delivery is read-only and
  must not chmod/chown operator paths.
- Sleep-based startup ordering — rejected.
