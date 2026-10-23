# SEC-08/09 — final candidate image audit and UNKNOWN caller attribution

Audit-only procedure. It changes no product code, dependency, pin, base image,
approval, VEX document or canonical gate. Results are reported from the single
native run's artifact; this document fixes how they were produced.

## Identities

- `AUDITED_SOURCE_SHA` — `9934ebe63943e7c1007564402e50258865a3a380`, the
  product source. Images are built from `git archive` of this SHA into a
  separate directory, so no worktree or dirty file enters a build context.
- `AUDIT_HARNESS_SHA` — the audit-only commit whose single parent is the
  audited source SHA. It adds only the files listed in
  `scripts/image_audit_final_candidate.py` (`AUDIT_MANIFEST`).
- Every image is a `NEW_AUDIT_ARTIFACT`. Native acceptance run `40004249842`
  used synthetic L/A/B revisions; those images are not byte-identical to these
  and its PASS does not transfer. Prior approvals do not transfer either.

## Trigger and authorization

`.github/workflows/sec08-09-final-candidate-image-audit.yml` runs on push to
`codex/security-sec-09-network-isolation` only when its own file changes,
attempt 1 only. The authorize job requires: event `push`, the branch ref,
`HEAD == GITHUB_SHA`, exactly one parent equal to the audited source SHA, and
changed paths exactly equal to the manifest. No commit-message matching.

## Images (seven)

`api` (API, worker and delivery roles), `web`, `gateway`, `media-executor`,
`media-net-executor`, `egress-proxy`, and the candidate PostgreSQL pin from
the source `compose.yaml` (`postgres:16.15-alpine3.24`). Recorded per image:
full Image ID / config digest, platform, OCI revision label, and for
PostgreSQL the registry index and platform manifests. The candidate
PostgreSQL is not the retained production PostgreSQL; the candidate gosu
evidence and approval are not extended to any other PostgreSQL artifact.

## Order

1. Offline unit tests (this audit and the reused candidate-7 helpers).
2. Clean source proof: runtime input hashes of the archive equal those of the
   harness checkout (the audit commit touches no runtime input).
3. Builds from the clean context. A missing package pin fails the build; pins,
   bases and locks are not updated.
4. Compatibility smoke on the same images before any scan:
   - candidate-7 smoke: package inventory (dpkg/apk pins), nginx `-t` and
     HTTP, API live/ready with `alembic upgrade head` against a disposable
     PostgreSQL, worker startup, MP4 (h264) / WebM (vp9) stream copy plus
     ffprobe as uid 10001 (root only to prepare the disposable volume), gosu
     checksum;
   - executors: module import, launcher/tool presence (`ffmpeg`/`ffprobe` in
     the offline executor; `yt-dlp` and no `ffmpeg` in the network executor),
     and unprivileged start must fail closed at preflight;
   - egress proxy: module import, start as `65534:65534` on an isolated
     network, listen marker and loopback connect.
5. Trivy 0.74.0 (checksum-verified release asset), existing policy, one DB
   download, seven offline scans by local Image ID. The recorder proves one
   download, offline flags on every scan, and identical `db/trivy.db` and
   `db/metadata.json` hashes after download and after all scans. Image IDs are
   inspected before and after each scan.
6. UNKNOWN caller attribution on the same Image IDs (identity checked against
   the pre-scan record, before and after).
7. Evidence hashes, upload, scoped cleanup with verification.

Findings make the job non-zero without being a technical failure. Scanner,
source, schema, DB-freeze or timeout problems are classified `technical_failure`
and stop the audit; there is no retry. Raw Trivy reports are uploaded next to
the summary; analysis layers never rewrite raw findings.

## Caller attribution method

Families: libxml2 and libacl (priority), then FFmpeg libavformat, cJSON,
expat, libtiff, libsndfile. Per Debian image and role:

- package and version from `/var/lib/dpkg`, owned ELF paths;
- static ELF evidence: DT_NEEDED closure from the role's entry executables,
  direct consumers and imported symbols;
- runtime evidence from self-generated fixtures only (lavfi MP4/AAC and a
  benign static MPD), using the product's ffprobe and mux argv
  (`-protocol_whitelist file`, no forced input format). glibc `LD_DEBUG`
  records initialised libraries and symbol bindings; a binding counts as a
  call only when it is absent in the control run (MP4 instead of MPD,
  `import yt_dlp` instead of a parse). Python role modules are imported and
  their mapped objects listed;
- role context: privilege, sandbox placement, attacker input source, and the
  configuration condition under which the role executes the tool.

Verdicts: `confirmed` (differential call evidence; content-probed demuxer
selection for libavformat; for libxml2 a runtime MPD parse by the dash
demuxer, labelled as an inferred call because FFmpeg's libraries are linked
BIND_NOW and symbol bindings cannot be separated from load), `potentially reachable` (static chain or runtime
load without call evidence), `UNKNOWN` (no chain from known product entries;
other execs are not traced, so this is not unreachability). Advisory-specific
vulnerable-function reachability is not established by this audit and no
`NOT_AFFECTED` is derived from it.

Configuration note: `MEDIA_EXECUTOR_ENABLED` and `MEDIA_NET_EXECUTOR_ENABLED`
default to false. With muxing on and the executor off, ffmpeg/ffprobe run in
the secret-bearing worker outside any tool sandbox; with the network executor
off, yt-dlp runs in the worker. Transactional rollback from OFF→ON restores
the previous OFF authority, so the worker path is part of the candidate's
reachable surface.

## Reconciliation

`scripts/image_audit_final_reconcile.py` compares the new summary with run
`38689849543` (summary SHA-256
`163d272b35cf43b06efd6888e2083607b01e4d612a09160e4c270a1db015448f`): Image
IDs, vulnerable package versions, advisory IDs, instance rows (new / gone /
changed), fixable / unfixed, and advisory DB snapshots (different snapshots
are not called the same DB). The historical owner approval (47 IDs / 514
rows, valid until `2026-10-29T20:18:03Z`) is bound to the historical
artifacts; the tool only reports whether re-approval is required and never
extends or applies it.

## Out of scope

Product fixes, dependency bumps, approval/VEX changes, gate softening, SSH,
production installation, merge/deploy, TEST/LIVE/SEO changes. Not rollout
readiness.
