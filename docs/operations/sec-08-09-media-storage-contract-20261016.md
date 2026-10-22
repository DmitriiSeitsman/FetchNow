# SEC-08/09 — media storage contract (host-managed dedicated FS)

**Status: LOCAL CONTRACT PREPARED / NOT INSTALLED / `approved:false`.**

Backend: **one** host-managed, fixed-capacity filesystem bind-mounted into all
media data roots. Marker files, free-space readings alone, Docker named volumes
on the root FS, `max_bytes`, periodic sampling, and post-reap reject are **not**
hard boundaries.

## Writable path map (peak contributors)

| Path / root | Writer | Service | Lifetime | Peak contribution |
| --- | --- | --- | --- | --- |
| `{tmp}/downloads/{job}/{attempt}/` video/audio/mux/output | worker | worker | attempt | ≤3×`MAX_ARTIFACT_BYTES` |
| `{tmp}/downloads/published/{id}/` | worker | worker; delivery RO | retained TTL | ≤`MAX` each; plan ×2 headroom |
| `{tmp}/media_inspection/inspect_*` | worker (OFF path) | worker | attempt | metadata only |
| `{executor}/{job}/` input-video/audio, output-mux | media-executor (+ worker stages) | media-executor | attempt | ≤3×`MAX` |
| `{media-net}/{job}/` cache + output-artifact | media-net-executor | media-net-executor | attempt | ≤1×`MAX` (+cache) |
| Handoff copies | worker↔executors via `copy_regular` | both sides | transient | +source while dest grows |
| egress-proxy `/tmp` tmpfs | proxy | egress-proxy | ephemeral | not on media FS |

**Critical overlap:** download may already exist on worker while executor still
holds inputs/output and a published copy is being written — peak is not “one
artifact”.

## Calculated capacity

`MAX_ARTIFACT_BYTES = 3221225472` (3 GiB).

| Component | Bytes |
| --- | --- |
| media-net active | 1×MAX |
| worker V+A+out | 3×MAX |
| offline executor tree | 3×MAX |
| handoff amplification | 2×MAX |
| retained published ×2 | 2×MAX |
| FS reserve (inodes/journal) | 4 GiB |
| **Total `capacity_bytes`** | **39728447488** |

Concurrency note: `MEDIA_EXECUTOR_MAX_JOBS` is **per executor**. Offline and
network can run simultaneously; the peak model includes both.

## Activation contract fields (approved activation only)

See `docs/operations/sec-08-09-media-storage-contract.json` proposal wrapper.
Activation requires `approved:true` plus real:

- `host_bind_root` (absolute, non-symlink)
- `fs_uuid`, `source_device`, `fs_type` ∈ {ext4,xfs}
- layout dirs `tmp`, `executor`, `media-net` already present on that FS
- kernel mount identity via `findmnt` (not a marker file)
- usable capacity in `[capacity_bytes - 4 GiB, capacity_bytes]`; free ≥ 3×MAX
  for one operation. The approved 37 GiB is a **maximum**, not a minimum.
  The 4 GiB reserve accounts for filesystem metadata/format overhead; usable
  blocks must still fit the full 33 GiB working/retained peak. Provisioning must
  verify measured geometry rather than assume device bytes equal usable bytes.

**Host facts still unknown (do not invent):** free partition/device/UUID on the
production host. Operational install must supply them after inventory.

## Release wiring

- `check_media_prerequisites(..., require_storage=True)` before activation
- generated `media-settings.json` redefines `tmp` / `executor-work` /
  `media-net-work` as bind `driver_opts` to layout paths (missing device → fail)
- canonical OFF (no media overlays) unchanged
- rollback restores previous media authority only; **never** formats/unmounts

### Existing data and actual mounts (corrective pass, 2026-10-22)

`rollout --media-storage-json <approved-contract.json>` passes the explicit
contract to the release transaction. Proposals are still rejected. Compose
validation checks each service's media volume mappings, then inventories existing
Docker volumes **before storage-init or sidecar changes**. A legacy/root-backed
volume with different driver options causes an explicit migration-required
refusal. Nothing is removed, recreated or copied automatically.

This is not an implemented data-migration procedure: the existing production
`tmp` volume and retained media need a separately authorized preservation,
consistent-copy and rollback plan. Do not remove the old volume to bypass the
gate. Until that plan is executed and verified, activation remains blocked.

After sidecar startup and in full managed health, actual container Mounts,
volume options/Mountpoint and kernel filesystem UUID/device are inspected.
Worker, delivery and both executors are covered. The API has no media-data
mount; the short-lived storage-init mapping is checked in rendered Compose.
Empty UUID, missing root
identity, arbitrary device prefixes and root-FS fallback fail closed. Raw device
symlink aliases are canonicalized; a findmnt bind suffix is not a prefix match.

Swap policy survives budget serialization (`memory_swap_max_bytes=0`), is checked
at ancestor `memory.swap.max`, and container MemorySwap must equal its RAM limit.
Generated settings set the matching `memswap_limit`. No budget was approved here.

The prepared native shell harness now owns a fresh private temporary directory,
rejects caller WORKDIR and retains evidence on unmount/detach failure. Its
filesystem-only completion returns 78, not full native PASS; Compose/cgroup
acceptance still requires a separate authorized run. No host operations occurred
in this corrective pass.

## ENOSPC / EDQUOT

`copy_regular` maps ENOSPC/EDQUOT → `HandoffError("storage_exhausted")` →
`DOWNLOAD_STORAGE_UNAVAILABLE`. Attempt fails; no READY publish; cleanup stays
attempt-scoped; no fallback write onto root FS.
