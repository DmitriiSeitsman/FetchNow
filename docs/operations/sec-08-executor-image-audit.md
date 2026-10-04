# SEC-08 — Media Executor Image Audit

Status: **FINDINGS / DECISION REQUIRED** (unchanged SEC-03C policy).  
Date: 2026-10-04. Worktree `/Users/dina/projects/FetchNow-sec08`, branch
`codex/security-sec-08-media-executor`, HEAD
`be254879fc4e4f520af680de3b3d8adfe1881b92`.

This is a vulnerability audit of a disposable linux/amd64 rebuild. It is **not**
production approval, SEC-09, residual acceptance, or a reuse of the native
behavioral PASS identity.

Native behavioral evidence (separate; not rescanned here):

- Run [37224290556](https://github.com/DmitriiSeitsman/FetchNow/actions/runs/37224290556)
  attempt 1 success.
- Native image ID:
  `sha256:90b1e70475f1bd6e664d1c61b6d7e413bf38b7259aa7a435fcf7230838b77951`
- `result.json` SHA-256:
  `feb4a7e4b6b42dc51507734bb86e98a892ae10de5d6cff8512cc9fad9734f7a5`

That native image was **not** present locally or in a reachable registry after
runner cleanup. No registry publish and no credentials were created.

## Artifact under audit — NEW AUDIT ARTIFACT

One disposable rebuild from the current HEAD, Dockerfile unchanged:

| Field | Value |
| --- | --- |
| Class | **NEW AUDIT ARTIFACT** (not the native PASS image) |
| Source SHA | `be254879fc4e4f520af680de3b3d8adfe1881b92` |
| Dockerfile | `backend/Dockerfile.media-executor` |
| Dockerfile SHA-256 | `6262357f7bf4aa12c59a250e39ce0787c180e05a3a9006fb59e91c9090df6b4a` |
| Platform | `linux/amd64` (buildx `--load` on darwin/arm64 via QEMU) |
| Build window | 2026-10-04T18:31:26Z → 18:33:03Z (~97 s) |
| Local tag | `fetchnow-media-executor:sec08-audit-local` |
| Docker `inspect .Id` (scan authority) | `sha256:f370208c2d4379a285ee887b093959e183b1985b54ff11eafeebc5c1b1eb06a6` |
| Buildx-exported config digest | `sha256:aeeb8270f4b3ec0be5769ee9566389015e8b18f077d0a8d6ecf322f5906d4da2` |
| Local `RepoDigests` | `fetchnow-media-executor@sha256:f370208c2d4379a285ee887b093959e183b1985b54ff11eafeebc5c1b1eb06a6` |
| Base resolved in build log | `python:3.14.7-slim-trixie@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d` |

Notes on digests: on this Docker Desktop host, `inspect .Id` equals the
buildx-exported **manifest** digest; the config digest is recorded separately
from the build export. The base digest above is the build-resolved reference
from the build log; a separate registry index vs amd64-only platform-manifest
split was not fetched. Do not treat this Image ID as the native
`90b1e704…` artifact.

Observed runtime versions inside the NEW artifact:

| Component | Version |
| --- | --- |
| Python | 3.14.7 |
| ffmpeg / ffprobe | Debian `7:7.1.5-0+deb13u1` |
| OpenSSL / libssl3t64 / openssl-provider-legacy | `3.5.7-1~deb13u3` |
| libpcre2-8-0 | `10.46-1~deb13u2` (fixable; see below) |
| OS | Debian 13.7 (trixie) |

## Scanner / DB provenance

Same pin and flags as SEC-03C (`scripts/image_audit.py`, Trivy **0.74.0**).
Canonical gate target list was **not** modified. Scan used the same Trivy
argv helpers and the same HIGH/CRITICAL policy functions
(`extract_findings`, `policy_verdict_for_findings`). Completeness for this
image used debian OS-package surface proof; a Python language Result is
**not** required because the image has no pip/venv packages (pip/ensurepip
removed). No VEX/exceptions applied. PostgreSQL/gosu VEX not used.

| Item | Value |
| --- | --- |
| Trivy | 0.74.0 |
| Archive | `trivy_0.74.0_macOS-ARM64.tar.gz` |
| Archive SHA-256 | `1caada5e0e2091909357c7525d3aa76f4b660b13821bc143b190c7483e31cc11` |
| DB Version | 2 |
| DB UpdatedAt | 2026-10-04T14:28:15.152965+00:00 |
| DB DownloadedAt | 2026-10-04T18:34:18.538570+00:00 |
| DB NextUpdate | 2026-10-05T14:28:15.152964+00:00 |
| Scan started | 2026-10-04T18:33:47Z |
| Offline flags | `--skip-db-update --skip-java-db-update --skip-check-update --offline-scan --image-src docker` |
| Scanners | `vuln` only |
| Telemetry | disabled |

Raw evidence (outside git):

| File | SHA-256 |
| --- | --- |
| `/tmp/fetchnow-sec08-executor-audit/reports/media-executor.trivy.json` | `49b5991f9138dfe846e5f970ea78d46fac684177d7b4508fb1ae20b630c9b6a3` |
| `/tmp/fetchnow-sec08-executor-audit/summary.json` | `410a9f36c79ab2c04a3a2da4bca65cce6eb6dceb73307da09b140fcf29a28ab1` |

## Inventory (safe)

Absent from the final runtime: `gcc`/`cc`/`g++`/`make`/`cmake`, `pip`/`pip3`,
`ensurepip`, `pytest`, Node/npm/yarn, Go/Rust toolchains. No `/opt/venv`.

Present by design: `python -m fetchnow.media_executor` entrypoint; USER `0`
(trusted supervisor contract; capabilities not proven by this scan — see native
run); `/usr/local/bin/sec08-launch`; `/usr/bin/ffmpeg` and `/usr/bin/ffprobe`;
`PYTHONPATH=/opt/fetchnow/src`.

Source copied into the image under `/opt/fetchnow/src/fetchnow/media_executor/`
(Python modules plus `sec08-launch.c`). `__pycache__` objects were also present
in this build context copy. No `.env`, private-key filenames, or
`credentials.json` paths were found in a name-only search. Secret contents were
not printed.

`network_mode: none` and Landlock/cgroup isolation are **not** used here as
proof that media parsers are safe. Untrusted media bytes still reach
ffmpeg/ffprobe when the executor is used.

## Policy verdict

| Metric | Value |
| --- | --- |
| Overall (SEC-03C policy) | **`fail`** |
| Reason | ≥1 HIGH/CRITICAL with non-empty `FixedVersion` |
| Total finding rows (all severities) | 637 |
| CRITICAL rows | 1 |
| HIGH rows | 213 |
| Gate fixable instances | **1** |
| Gate fixable unique IDs | **1** (`CVE-2026-103111`) |
| Gate unfixed instances | **213** |
| Gate unfixed unique IDs | **47** |

## Fixable HIGH/CRITICAL

### CVE-2026-103111 — AFFECTED (fixable)

| Field | Value |
| --- | --- |
| Aliases / vendor | DSA-6530-1; GHSA-r9hj-j2rw-4q3m |
| Package | `libpcre2-8-0` (Debian source `pcre2`) |
| Installed | `10.46-1~deb13u2` |
| Fixed (trixie security) | `10.46-1~deb13u3` |
| Severity | HIGH |
| Trivy status | `fixed` (fix available, not yet installed) |
| Source | distro OS package (ffmpeg transitive stack) |
| Primary tracker | https://security-tracker.debian.org/tracker/CVE-2026-103111 |

Debian security tracker (retrieved 2026-10-04): trixie vulnerable at
`10.46-1~deb13u2`; trixie (security) fixed at `10.46-1~deb13u3` via DSA-6530-1.
Upstream advisory: attacker-controlled regular expression / certain JIT API use
→ out-of-bounds write. `libpcre2-8-0` is installed in this final image.
Executor tools process attacker-influenced media bytes; presence in the runtime
ffmpeg dependency closure is enough to treat the instance as **AFFECTED** for
this audit (not NOT_AFFECTED via `network_mode:none`).

Minimal remediation scope (proposal only; not applied): pin/upgrade
`libpcre2-8-0` (and related `pcre2` binaries if pulled) to
**`10.46-1~deb13u3`** from Debian trixie-security in
`backend/Dockerfile.media-executor`, then rebuild and re-audit the new Image ID.

## Unfixed HIGH/CRITICAL (47 unique IDs)

Grouped by source. Instances inflate because ffmpeg source hits nine binary
packages each (`ffmpeg` + `libav*` + `libsw*` + `libpostproc`).

### FFmpeg stack — 16 unique IDs (144 instances)

Installed: `7:7.1.5-0+deb13u1`. Trivy status mostly `will_not_fix` /
`fix_deferred`.

| ID | Prior API residual class (Candidate A matrix) | Executor applicability |
| --- | --- | --- |
| CVE-2026-58049 | AFFECTED | **AFFECTED** — local media → ffmpeg/ffprobe |
| CVE-2026-64830 | AFFECTED | **AFFECTED** |
| CVE-2026-64835 | AFFECTED | **AFFECTED** |
| CVE-2026-66039 | AFFECTED | **AFFECTED** |
| CVE-2026-66041 | AFFECTED | **AFFECTED** |
| CVE-2026-70628 | AFFECTED | **AFFECTED** |
| CVE-2026-70632 | AFFECTED | **AFFECTED** |
| CVE-2026-64832 | NOT_AFFECTED (NVDEC) | **NOT_AFFECTED** — same product argv; Debian build disables NVDEC path cited in the API matrix |
| CVE-2026-64833 | NOT_AFFECTED (unused muxer) | **NOT_AFFECTED** — product muxes only `mp4`/`webm` |
| CVE-2026-64834 | NOT_AFFECTED (network/protocol) | **NOT_AFFECTED** — `-protocol_whitelist file`; executor `network_mode: none` is additional but not the sole basis |
| CVE-2026-66036 | NOT_AFFECTED (filter path) | **NOT_AFFECTED** — `-c copy` only |
| CVE-2026-66040 | NOT_AFFECTED (encoder path) | **NOT_AFFECTED** |
| CVE-2026-75142 | NOT_AFFECTED (unused muxer) | **NOT_AFFECTED** |
| CVE-2026-75143 | NOT_AFFECTED (network/protocol) | **NOT_AFFECTED** |
| CVE-2026-75144 | NOT_AFFECTED (network/protocol) | **NOT_AFFECTED** |
| CVE-2026-75146 | NOT_AFFECTED (network/protocol) | **NOT_AFFECTED** |

All seven previously listed **AFFECTED** media IDs are present. None of those
seven are missing. Isolation reduces blast radius; it does **not** clear these
IDs.

Closing remaining AFFECTED ffmpeg risk still depends on a vendor/distro bump
beyond `7.1.5` (API remediation text cites sid `7:9.0.2-1` as the forecast for
several deferred IDs). Exact package choice needs a fresh tracker check at
remediation time; versions are not proposed from memory here beyond that
existing Candidate A note.

### Other unfixed unique IDs (31)

| ID | Package(s) sample | Installed | Severity | Applicability |
| --- | --- | --- | --- | --- |
| CVE-2026-6653 | libxml2 | 2.12.7+dfsg+really2.9.14-2.1+deb13u3 | CRITICAL | **UNKNOWN** — linked via media stack; no proof XML attacker path is unreachable |
| CVE-2026-74860, CVE-2026-86138…86144 | libxml2 | same | HIGH | **UNKNOWN** |
| CVE-2026-66046, CVE-2026-76956, CVE-2026-76957, CVE-2026-93990 | libexpat1 | 2.8.3-1~deb13u1 | HIGH | **UNKNOWN** — also listed as new vs older API baseline in Candidate A notes |
| CVE-2026-16554, CVE-2026-29036, CVE-2026-67215, CVE-2026-67216, CVE-2026-87933 | libcjson1 | 1.7.18-3.1+deb13u1 | HIGH | **UNKNOWN** |
| CVE-2026-88806 | libx11-6 / libx11-data / libx11-xcb1 | 2:1.8.12-1 | HIGH | **UNKNOWN** — ffmpeg transitive UI libs; same residual posture as API matrix |
| CVE-2026-88807 | libxrender1 | 1:0.9.12-1 | HIGH | **UNKNOWN** |
| CVE-2026-36849, CVE-2026-52490 | libtiff6 | 4.7.0-3+deb13u3 | HIGH | **UNKNOWN** |
| CVE-2026-37555 | libsndfile1 | 1.2.2-2+deb13u1 | HIGH | **UNKNOWN** |
| CVE-2026-96889 | librsvg2-2 | 2.60.0+dfsg-1 | HIGH | **UNKNOWN** |
| CVE-2025-69720 | ncurses stack | 6.5+20250216-2 | HIGH | **UNKNOWN** |
| CVE-2026-16742 | libsystemd0 / libudev1 | 257.13-1~deb13u1 | HIGH | **UNKNOWN** |
| CVE-2026-54369 | libacl1 | 2.3.2-2+b1 | HIGH | **UNKNOWN** |
| CVE-2026-76642, CVE-2026-78408…78410 | util-linux / mount / login / libuuid… | 1:2.41.5-0+deb13u1 | HIGH | **UNKNOWN** |
| CVE-2026-9538 | perl-base | 5.40.1-6+deb13u1 | HIGH | **UNKNOWN** (`will_not_fix` / deferred) |

OpenSSL packages: **no** HIGH/CRITICAL findings on this artifact after the
`3.5.7-1~deb13u3` pin (contrast older API scans that still carried
`CVE-2026-84782` before that pin).

## Comparison with API / FFmpeg residual matrix

| Topic | API Candidate A (documented) | This executor NEW artifact |
| --- | --- | --- |
| Base | `python:3.14.7-slim-trixie` | same family |
| ffmpeg | `7:7.1.5-0+deb13u1` | same |
| OpenSSL pin | `3.5.7-1~deb13u3` | same; no HC OpenSSL rows here |
| Fixable HC unique | historically included PCRE2 / OpenSSL depending on scan date | **PCRE2 only** (`CVE-2026-103111`) |
| Unfixed HC unique | ~47 in Candidate A writeup | **47** |
| AFFECTED media IDs (7) | listed | all **present** |
| New vs that media set | — | no additional ffmpeg AFFECTED IDs beyond the prior seven; other unfixed IDs are shared transitive OS/media libs |

Instances ≠ unique IDs: 214 gate HC rows → 48 unique IDs (1 fixable + 47 unfixed).

Old API residual decisions and VEX documents were **not** rewritten and were
**not** applied to this image.

## Reuse of native evidence

Allowed: cite run 37224290556 for Landlock/cgroup/UDS/cancel behavior of the
**then-built** image `90b1e704…`.

Not allowed: treat this NEW audit Image ID as that native identity; treat this
scan as a behavioral re-test; treat isolation as CVE closure.

## Cleanup

Removed by this task: disposable tag
`fetchnow-media-executor:sec08-audit-local` (and matching Image ID) after the
report was written; inventory containers used `--rm`. Task-local Trivy cache
under `/tmp/fetchnow-sec08-executor-audit/cache` cleaned with
`trivy clean --all` against that directory only. Raw reports under
`/tmp/fetchnow-sec08-executor-audit/` may remain until the operator deletes the
directory; they are not committed.

No global docker prune. Main checkout, SEC-03C worktree, production, and
protected backups untouched. Staging left empty. Commit/push/PR/merge/deploy:
not performed.

## Verdict line

**SEC-08 — EXECUTOR IMAGE AUDIT FINDINGS / DECISION REQUIRED**

Policy `fail` because of fixable `CVE-2026-103111` on `libpcre2-8-0`. Unfixed
HIGH/CRITICAL remain, including the seven AFFECTED ffmpeg media IDs. This is
not rollout permission and not residual acceptance.
