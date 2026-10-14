# SEC-08 / SEC-09 — executor attempt disk limits

Status: **local safety rail only**. These numbers are **not** an approved
production resource budget and do not replace `resource_budget` / slice
ceilings used by media activation.

## Semantics

For every tool operation (`mux_copy`, `ffprobe_validate`, `inspect_metadata`,
and downloads) the supervisor measures:

| Quantity | Meaning |
| --- | --- |
| `baseline` | Attempt tree bytes at tool start (already-staged inputs) |
| `growth` | `tree - baseline` — new writes (output / cache / temp) |
| `tree` | Absolute attempt usage (inputs + output + cache) |
| `min_free` | Free space on the attempt volume |

Growth and tree are **independent**. Large legal mux inputs must not be treated
as “new output” against the growth ceiling.

Measurement / enumeration errors **fail closed** (treat as limit hit / refuse
launch). When a limit trips: cgroup kill, wait for reaping, no READY/publish
from the executor (worker remains the publication authority).

**Final post-reap check:** after the tool tree is reaped, the supervisor
re-measures growth/tree/min_free once more. A child that writes past a ceiling
and exits before the next periodic sample (default 200 ms) must still fail.
Pre-start refuses launch when the attempt already exceeds `max_tree` or
`min_free`. Primary `timed_out` / `cancelled` flags are preserved when a disk
limit is also detected; cleanup/reaping failures never return success.

## Server ceilings (disposable defaults)

| Op | `max_growth` | `max_tree` | `min_free` (when client omits) |
| --- | --- | --- | --- |
| `mux_copy` | `MAX_ARTIFACT_BYTES` | `3 × MAX_ARTIFACT_BYTES` | `TOOL_MIN_FREE_BYTES` (256 MiB) |
| `ffprobe_validate` | 16 MiB | `3 × MAX_ARTIFACT_BYTES + 16 MiB` | 256 MiB |
| `inspect_metadata` | 64 MiB | 64 MiB | 256 MiB |
| `download_*` | `min(client max_bytes, server max_filesize)` | same as growth | client `min_free` (protocol-capped) |

Client **cannot raise** growth/tree ceilings above the server constants.
Offline/inspect ops do not accept client `max_bytes` / `min_free` in the
protocol; the server supplies the table above.

`MAX_ARTIFACT_BYTES` matches the download artifact ceiling
(`3_221_225_472`). Mux tree peak follows video + audio + muxed output.

## Out of scope

- Production slice / cgroup budget approval (`resource_budget`)
- Cross-worker disk reservation
- Changing CVE / residual image policy
- Forced provider downloads for proofs
