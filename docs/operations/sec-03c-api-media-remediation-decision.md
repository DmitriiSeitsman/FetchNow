# SEC-03C — Stage 2: API / FFmpeg remediation decision

Status: **CANDIDATE A IMPLEMENTED / OPENSSL REMEDIATED / RELEASE RISK DECISION REQUIRED**

Historical Stage-2 decision evidence below is preserved. Owner-approved local
implementation (Candidate A + OpenSSL pin) is recorded in
§8 without rewriting prior scan counts.

Worktree: `/Users/dina/projects/FetchNow-sec03c`  
Branch: `codex/security-sec-03c-image-audit`  
HEAD: `f8ddd426b208500c17bbde2831dfeb508ea43574`  
Staging: empty for this implementation step (commit/push/PR not performed).

Baseline evidence: `/tmp/fetchnow-sec03c-remediation/summary.json`  
SHA-256: `22f612f8cfbe5999f50a4ea3afd24f28c43f0b6751bb70c3ef85792713f0e49f`  
Baseline API image ID: `sha256:81f52f8899529383521396aedb3530002426f0ec6ecfda4d351289ba4de00d49`  
Baseline API unfixed HIGH/CRITICAL: **260 rows / 90 unique advisory IDs**  
(OS family Debian 12.15 / bookworm; ffmpeg `7:5.1.9-0+deb12u1`).

Stage-2 evidence root: `/tmp/fetchnow-sec03c-api-media/` (outside the worktree).  
Implementation evidence root: `/tmp/fetchnow-sec03c-candidate-a-impl/` (outside the worktree).

Gosu VEX remains **not applied**. Residual risk acceptance is **not granted**.
PostgreSQL / web / gateway images were not redesigned here.

---

## 1. Real media surface (from code)

Shared image: `fetchnow-api` (api + worker + delivery + storage-init).  
**ffmpeg/ffprobe paths are Compose-injected on worker only** (`MEDIA_MUXING_*`).
API/delivery/storage-init must not receive tool paths (compose contract).

| Use | Tool | Operation | Network |
| --- | --- | --- | --- |
| Bounded mux (feature flag, default off) | `/usr/bin/ffmpeg` | **Stream-copy only** (`-c copy`) of local video+audio into `mp4` or `webm` | **Excluded** (`-protocol_whitelist file`) |
| Mux output validation | `/usr/bin/ffprobe` | Structural JSON: `codec_type,codec_name` + `format_name,duration,size` | **Excluded** (`-protocol_whitelist file`) |
| Progressive download / inspection | `yt-dlp` | Exact `-f <token>`; **no** `--ffmpeg-location` / merge flags | Provider HTTP via yt-dlp (outside this argv) |

Server-controlled ffmpeg argv (abbreviated):

```text
ffmpeg -hide_banner -nostdin -nostats -loglevel error -n
  -protocol_whitelist file
  -i <abs_video> -i <abs_audio>
  -map 0:v:0 -map 1:a:0 -c copy -sn -dn
  -map_metadata -1 -map_metadata:s -1 -map_chapters -1
  -f <mp4|webm> <abs_output>
```

Server-controlled ffprobe argv (abbreviated):

```text
ffprobe -hide_banner -loglevel error
  -protocol_whitelist file
  -print_format json
  -show_entries stream=codec_type,codec_name:format=format_name,duration,size
  -i <abs_muxed_file>
```

**Trusted inputs:** absolute local paths under the private artifact workspace;
executable paths validated as regular non-symlink files named `ffmpeg`/`ffprobe`.

**Untrusted / attacker-influenced inputs:** media bytes downloaded by yt-dlp for a
user-submitted provider URL (after extractor allow-lists). Clients never supply
ffmpeg argv, URLs to ffmpeg, or codec flags.

**`-c copy` does not prove absence of dangerous parsing.** Demux/mux and ffprobe
still open containers and may initialize codec paths while identifying streams.
Timeouts and memory limits are **not** process isolation.

Runtime already installs Debian `ffmpeg` with `--no-install-recommends`. Transitive
shared libraries remain linked into the ffmpeg stack; they were **not** removed
without linkage evidence.

Sources: `backend/src/fetchnow/downloads/ffmpeg_argv.py`,
`ffprobe_argv.py`, `executor.py`, ADR 0013, `compose.yaml` worker env.

---

## 2. Candidates (maximum two)

### A — Official Python / Debian distro FFmpeg (supported)

| Item | Value |
| --- | --- |
| Base | Official `python:3.14.7-slim-trixie` (linux/amd64) |
| Index digest observed at build | `python@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d` |
| FFmpeg | Debian trixie/security package `ffmpeg` **`7:7.1.5-0+deb13u1`** (`libavcodec61` / `libavformat61` / …) |
| Authenticity | Docker Hub official `python` image; Debian apt from `deb.debian.org` / `debian-security` |
| Security updates | Distro `apt` + official Python image rebuilds |
| License | Unchanged distro FFmpeg (GPL-enabled Debian build) |
| linux/amd64 | Yes (built/scanned under `--platform linux/amd64`) |

**Vendor forecast (Debian security tracker, ffmpeg source + related packages):** ~
45 of the baseline 90 unique IDs resolve on trixie vs open on bookworm; **15/16**
baseline ffmpeg-core IDs remain **open** on trixie (postponed for 7.1.x), fixed
in sid `7:9.0.2-1`. One ffmpeg ID (`CVE-2026-8461`) is resolved on trixie.

**Not promised:** a clean Trivy scan.

### B — Alternative FFmpeg delivery (third-party static; not Debian-supported)

Defined only because A leaves substantial **applicable** media CVEs per tracker.

| Item | Value |
| --- | --- |
| Base (planned) | Same `python:3.14.7-slim-trixie` app layers |
| FFmpeg | Pinned BtbN static **n9.0.2-14-gebafaee10a** linux64 GPL |
| Release | `https://github.com/BtbN/FFmpeg-Builds/releases/tag/autobuild-2026-09-29-13-10` |
| Archive SHA-256 | `1ea5558621c3fdb4b0b99a1f02f881eefd7670cd70d88a8bee88edc899687552` |
| Binary SHA-256 | ffmpeg `2fb90a5fe663bfe383f921a377dafff1c3aeb9892144ac35af3e44130b2861cc`; ffprobe `da1e2267a1571c4765ccfffc9f7ac615fa680453b57d8a0ced10e208c38182a4` |
| Authenticity | GitHub release + `checksums.sha256` verification (community builder, **not** Debian) |
| Updates | Manual pin bumps; no distro DSA channel |
| License | GPL static build (distribution implications vs current apt ffmpeg must be owner-reviewed) |
| Vendor forecast | Aligns with Debian **sid** fixed_version `7:9.0.2-1` for remaining ffmpeg-core IDs |

**Experimental build:** **FAILED** (technical).  
Error: `ghcr.io` anonymous token EOF while resolving `astral-sh/uv:0.12.19`.  
Per Stage-2 rules: **no automatic retry**. B has **no** measured scan/tests.

No sid/testing base image is proposed as a production default. No FetchNow-owned
FFmpeg compile/maintenance path is recommended while A exists as a supported OS
move.

---

## 3. Measured experiment (Candidate A only)

Disposable Dockerfile (outside tree):  
`/tmp/fetchnow-sec03c-api-media/candidate-a/Dockerfile`  
Delta vs canonical: **only** `python:3.14.7-slim-bookworm` → `python:3.14.7-slim-trixie`
(builder + runtime). Canonical tree untouched.

| Field | Value |
| --- | --- |
| Tag | `fetchnow-api:sec03c-media-a` |
| Local inspect `.Id` / scan authority | `sha256:bb21629acb5f8e4b4550eccfc8f0bed061efd1a422da81650b34e60d166e6ca9` |
| Buildx config digest (export log) | `sha256:286cd769cfc3eb9f2afee616ce5770b535df2323f1e3af128f2bc061fb43b6ed` |
| Platform | `linux/amd64` |
| Revision label | `f8ddd426b208500c17bbde2831dfeb508ea43574` |
| ffmpeg/ffprobe | `7.1.5-0+deb13u1` |

**Tests (A):**

- `fetchnow-api` / `fetchnow-worker` console scripts present; app import OK.
- Frozen venv: `yt-dlp 2026.7.4`, `fastapi 0.141.1` (no unexpected unlock).
- Local fixture: encode tiny H.264+AAC + AAC, then **production-shaped**
  stream-copy mux + file-only ffprobe → `FFMPEG_FIXTURE_OK`
  (`streams [('video','h264'),('audio','aac')]`, mp4 family format).

**Scan (A-only — not a four-image gate):**

- Scanner: Trivy **0.74.0** (`/tmp/fetchnow-sec03c-trivy-bin/trivy`)
- Flags: `--scanners vuln --disable-telemetry --skip-db-update
  --skip-java-db-update --skip-check-update --offline-scan --image-src docker`
- DB: `UpdatedAt=2026-09-30T13:11:13.714761Z` — **same advisory DB generation as
  baseline** (`DownloadedAt` differs only). Comparison is not confounded by DB drift.
- Report: `/tmp/fetchnow-sec03c-api-media/scans/candidate-a.trivy.json`

| Metric | Baseline API | Candidate A |
| --- | --- | --- |
| Unique unfixed HIGH/CRITICAL IDs | **90** | **47** |
| Cleared vs baseline | — | **46** |
| Still present | — | **44** |
| New unfixed vs baseline | — | **3** (`CVE-2026-66041`, `CVE-2026-76956`, `CVE-2026-76957`) |
| Unique fixable HIGH/CRITICAL (non-empty FixedVersion) | 0 (post prior remediation) | **2** (`CVE-2026-75804`, `CVE-2026-84782`) |

This is an **API-image experiment**, not a full `sec-03c-image-audit` pass/fail.

---

## 4. Recommendation

**Recommend Candidate A:** move the API runtime/builder base from
`python:3.14.7-slim-bookworm` to **`python:3.14.7-slim-trixie`**, keep
`apt-get install --no-install-recommends ffmpeg` (distro).

Measurable effect: **~48% reduction** in unique unfixed API HIGH/CRITICAL
advisories (90 → 47) on the same Trivy DB generation, with mux/probe behavior
verified and frozen app deps unchanged.

**Do not recommend Candidate B** as the default remediation: it is not a
Debian-supported update channel, GPL static redistribution needs an explicit
owner license/supply-chain decision, and the disposable build hit a technical
failure without retry. Sid/`testing` is not proposed for production.

**Do not** claim readiness for rollout until the owner accepts A’s residual risk
posture (below) and any follow-on VEX/isolation work.

---

## 5. Exact file scope to implement A (owner-approved later)

| Path | Change |
| --- | --- |
| `backend/Dockerfile` | Replace both `FROM python:3.14.7-slim-bookworm` lines with `python:3.14.7-slim-trixie` |
| Docs referencing “Debian bookworm ffmpeg” | Update to trixie / `7.1.x` (e.g. `docs/adr/0013-bounded-media-muxing.md`, `docs/infrastructure/07-fetch-now-containers.md`) |
| Locks / Compose tool paths / CI gate policy | **No change required** for the minimal A path (`/usr/bin/ffmpeg`, `/usr/bin/ffprobe` unchanged) |

Out of scope for A: custom FFmpeg builds, package deletions without linkage
proof, VEX application, gosu exceptions, web/gateway/postgres.

---

## 6. Residual risks after A (47 unique unfixed HC)

Static classification of the **Candidate A** unfixed set (not a blanket accept):

| Verdict | Count | Meaning |
| --- | --- | --- |
| **AFFECTED** | **7** | Attacker-influenced local media can reach ffmpeg demux/decode paths used while muxing/probing |
| **NOT_AFFECTED** | **16** | Concrete mismatch with fixed argv / unused OS tools / disabled NVDEC path |
| **UNKNOWN** | **24** | Linked transitive libraries without proven call reachability |

### AFFECTED (media bytes → worker ffmpeg/ffprobe)

`CVE-2026-58049`, `CVE-2026-64830`, `CVE-2026-64835`, `CVE-2026-66039`,
`CVE-2026-66041`, `CVE-2026-70628`, `CVE-2026-70632`

- **Attacker input:** crafted provider media downloaded into the worker workspace,
  then opened by ffmpeg stream-copy and/or ffprobe.
- **Possible consequence:** memory corruption / DoS / code execution in the
  worker tool process (CVE-dependent).
- **Existing limits:** non-root UID 10001; file-only protocol whitelist; fixed
  argv; timeouts; stdout/stderr caps; mux feature default off; no client argv.
- **These limits do not prevent:** parser bugs on local crafted files; they are
  **not** equivalent to SEC-08/09 isolation.

### NOT_AFFECTED (examples with mechanism)

| IDs | Why |
| --- | --- |
| `CVE-2026-75143`, `CVE-2026-64834`, `CVE-2026-75144`, `CVE-2026-75146` | Network/protocol or live-DASH paths; argv forces `-protocol_whitelist file` |
| `CVE-2026-64833`, `CVE-2026-75142` | Unused muxers (S/PDIF, MPEG-PS); product muxes only `mp4`/`webm` |
| `CVE-2026-66036`, `CVE-2026-66040` | Filter / PNG-encoder paths; product uses `-c copy` only |
| `CVE-2026-64832` | NVDEC hardware decoder; Debian ffmpeg build config disables libmfx/omx and does not enable NVDEC/CUDA |
| util-linux / ncurses / acl / perl Archive-Tar IDs listed in evidence JSON | App/worker does not invoke those tool entrypoints for muxing |

Full per-ID table: `/tmp/fetchnow-sec03c-api-media/scans/candidate-a-residual-class.json`.

### UNKNOWN (high-signal examples)

`libxml2` / `libexpat1` / `libcjson1` / `librsvg2-2` / `libtiff6` / `libsndfile1`
/ `libX11` / `libXrender` — present as ffmpeg transitive dependencies; no proof
they are exercised by the fixed stream-copy/probe argv, and no proof they are
unreachable. Treat as residual until linkage/call evidence or vendor fixes.

### Proposed future owner-approved VEX scope (NOT APPLIED)

Only for IDs with documented NOT_AFFECTED mechanisms above, scoped to:

- component: Debian `ffmpeg` / matching `libav*` **`7:7.1.5-0+deb13u1`** on the
  **implemented** API image identity after A;
- justification: fixed server argv + protocol whitelist / unused code path;
- review triggers: argv change, ffmpeg package/digest change, enabling filters
  or network protocols, or new Symbol/call evidence.

**Do not** VEX the seven AFFECTED decoder/demuxer IDs or the UNKNOWN transitive
set as a blanket exception.

### Isolation dependency

Closing residual **AFFECTED** media risk to an acceptable production bar still
depends on worker isolation work tracked under **SEC-08/09** (not started here).
Timeouts ≠ isolation. That dependency affects rollout ordering: mux enablement
and public media flow remain coupled to isolation maturity even after A.

---

## 7. Impact on nearest rollout

| Item | Effect |
| --- | --- |
| App rollout with A implemented | New API/worker/delivery image on Debian 13; **Postgres pin/rollout policy unchanged** |
| Image gate | API likely remains `needs_owner_decision` (~47 unfixed HC) until per-ID owner actions; not a clean pass |
| Fixable leftovers on A | Two HIGH/CRITICAL with FixedVersion (`CVE-2026-75804`, `CVE-2026-84782`) — address in the implementation PR or gate stays `fail` for fixables |
| Mux feature | Still default off; enabling remains an operator decision after isolation/risk acceptance |
| Gosu / Postgres | Unchanged; Stage-1 applicability review complete; VEX not applied |
| Candidate B | Not in rollout path |

**Not READY FOR ROLLOUT** on the strength of this decision alone.

---

## Owner decisions required (pre-implementation; historical)

1. Approve **Candidate A** base move (`slim-trixie` + distro ffmpeg) for a later
   implementation PR.
2. Decide residual handling for the **7 AFFECTED** + **24 UNKNOWN** IDs
   (track vendor DSA, accept with SEC-08/09 dependency, or further harden) —
   **no blanket exception**.
3. Optionally authorize a **narrow** VEX only for documented NOT_AFFECTED IDs
   after A is implemented and re-scanned (proposal only here).
4. Explicitly reject or defer **B** (static FFmpeg) unless supply-chain/license
   ownership is accepted and a clean rebuild is separately ordered.

---

## 8. Implementation record (owner-approved local; not a rollout)

Canonical tree now applies Candidate A plus the OpenSSL pin:

| File | Change |
| --- | --- |
| `backend/Dockerfile` | `python:3.14.7-slim-trixie` (builder+runtime); `apt` installs `ffmpeg` and pinned `libssl3t64`/`openssl`/`openssl-provider-legacy` **`3.5.7-1~deb13u3`** from trixie-security |
| `docs/adr/0013-bounded-media-muxing.md` | Current distro assertion → trixie |
| `docs/infrastructure/07-fetch-now-containers.md` | Worker ffmpeg assertion → trixie |
| `docs/operations/sec-03c-image-audit.md` | Notes current API base/OpenSSL pin; family check remains `debian` |
| this document | Status + §8 |

OpenSSL source verified before pin: `apt-cache policy` on `debian:trixie-slim`
linux/amd64 showed candidate **`3.5.7-1~deb13u3`** on
`http://deb.debian.org/debian-security trixie-security/main` for all three
packages (same OpenSSL 3.5.7 branch as the vulnerable `…u2` install).

### Implemented artifact (fresh)

| Item | Value |
| --- | --- |
| Evidence root | `/tmp/fetchnow-sec03c-candidate-a-impl/` |
| API Image ID | `sha256:95688cc894b354aa2509f609f90d42e30adc806c115b9b3af0ba66c94828835c` |
| Platform | `linux/amd64` |
| Python | `3.14.7` |
| FFmpeg | `7:7.1.5-0+deb13u1` (`/usr/bin/ffmpeg`, `/usr/bin/ffprobe`) |
| OpenSSL packages | `libssl3t64` / `openssl` / `openssl-provider-legacy` = **`3.5.7-1~deb13u3`** |
| Gate (four targets) | overall **`fail`** (postgres gosu fixable 22); API **`needs_owner_decision`** (0 fixable HC; 47 unique unfixed HC); web/gateway **`pass`** |
| OpenSSL CVEs on API | **absent** (`CVE-2026-75804`, `CVE-2026-84782`) |
| msgpack/setuptools | **absent** |
| Residual class (API unfixed HC) | **7 AFFECTED / 16 NOT_AFFECTED / 24 UNKNOWN** (re-checked vs current packages + unchanged media argv) |
| Trivy DB | `UpdatedAt=2026-09-30T13:11:13.714761Z` (same generation as Stage-2 Candidate A experiment) |

Historical Stage-2 disposable Candidate A image (`sec03c-media-a`, pre-OpenSSL pin)
is **not** the implemented artifact.

Residual media AFFECTED/UNKNOWN classifications remain owner decisions;
Gosu VEX still not applied; SEC-08/09 not started; production mux state not
inspected here.

## Status line

**SEC-03C — CANDIDATE A IMPLEMENTED / OPENSSL REMEDIATED / RELEASE RISK DECISION REQUIRED**
