# SEC-08 media executor — current contract

## Same-artifact native PASS / fixable remediated — 2026-10-05

One authorized follow-up commit/push: `022d83f901d22ffa8a0132c35e7128ba1d0f9b31`.
Four files only: native inventory harness, same-artifact regression tests,
workflow subject marker and its regression test. Runtime, Dockerfile, locks,
scanner/policy and the pre-existing local documentation additions were unchanged.
`dpkg-query` now explicitly emits Package, Architecture and Version; the parser
requires one record, correct package (bare or `:amd64`), installed `amd64`, and
exact version `10.46-1~deb13u3`. Wrong versions/architectures, duplicates and
incomplete rows remain failures. Fresh offline tests: 40 passed; Ruff, mypy,
actionlint 1.7.7 and diff check passed.

Single push-event run, attempt 1, no retry:
https://github.com/DmitriiSeitsman/FetchNow/actions/runs/37268708428
Native job: https://github.com/DmitriiSeitsman/FetchNow/actions/runs/37268708428/job/111630932475
Job ran 2026-10-05T05:38:57Z through 05:39:57Z. Overall workflow **failure**
is intentional under unchanged policy: wrapper exit 2, `unfixed_residual`.
No technical or native failure was reported.

- **Native PASS**, exit 0: actual MP4/WebM mux/probe, real peer credentials,
  six explicit EACCES denials after tool execution, overflow, cancel/reap of
  parent and setsid/reparented descendant, other-task heartbeats, cgroup cleanup,
  private-root/ancestor-limit checks and restart refusal all passed.
- Inventory PASS: `libpcre2-8-0`, installed `amd64`, `10.46-1~deb13u3`;
  no C sources, pyc or __pycache__ in the executor runtime source directory.
- **Same image** for native and Trivy, one build, no rebuild between stages:
  `sha256:3969a416cacb3db87f9f03f514a6343c3c6e625bb117609d2aedc465fdaece21`,
  linux/amd64. Dockerfile SHA-256 unchanged:
  `a59147b7c1a2ffd2e85bf613c33441ca52320436a33974cb2ee2218c6bfd615d`.
- Audit `needs_owner_decision`, exit 2: **0 fixable HC**, CVE-2026-103111 absent;
  **213 unfixed HC instances / 47 unique IDs**. The ID set matches run
  37226784669 (none added/removed). This is not residual acceptance.
- Trivy 0.74.0, DB v2 UpdatedAt `2026-10-05T01:10:31.191619+00:00`,
  downloaded `2026-10-05T05:39:49.445716+00:00`.
- Both cleanup error lists empty; independent cleanup, export and upload passed.

Artifact `sec08-media-executor-37268708428-1`, ID `11327094077`:
- archive digest: `7762fde154d9aaaac91109203e3fa2d0704df2c6b90a892477860b9dcb078a70`;
- result.json: `95903cd3459ee54b57862d61b79864a11eb4b3dd650ddedec910163a27ac6dc2`;
- native/result.json: `d7b11c795d980af48f869108aeb145ad9fb2877581cce6499a52a240465e083b`;
- audit/summary.json: `c3b71923aac26f41734970adcc0ed3a96ae564491cde9dfaa56b642539347452`;
- raw scan hash recorded by wrapper: `abebc6954b3ff9d0c142e6b711c385028876a66bcd5a43a87f4262b5a4626d86`.
Downloaded result bytes match hashes.json. Raw scan is not published to git.

**SEC-08 — SAME-ARTIFACT NATIVE PASS / FIXABLE REMEDIATED.**
**UNFIXED RESIDUAL DECISION STILL REQUIRED.** No SECURITY PASS/rollout approval.
No PR/merge/deploy/SSH/production/TEST/LIVE/SEO/VEX changes. SEC-09 not started.
This result entry remains local and unstaged, without a second commit/push.
Earlier skipped/failed runs below remain independent historical evidence.

## Latest bounded follow-up — 2026-10-04, run 37226784669

Commit `16a819a2b3a2a7acd1bcbea6a96030a31e61e418` fixes only the workflow
subject gate and adds `backend/tests/test_media_executor_workflow_gate.py`.
The two pre-existing local report additions were preserved, not committed.
One push, one run, attempt 1:
https://github.com/DmitriiSeitsman/FetchNow/actions/runs/37226784669

`authorize` succeeded; native/audit really executed. No full-message equality:
the first line is compared exactly; Co-authored-by trailers are tolerated,
unrelated subjects/suffixes rejected. Branch and attempt restrictions retained.
Offline gate + same-artifact + harness tests: 28 passed. Ruff, normal typed
mypy, actionlint 1.7.7 and diff check passed. An initial mypy invocation with
`--follow-imports=skip` discarded pytest decorator types and failed; normal
mypy passed without source changes or suppression.

Overall workflow **failure**, wrapper `native_fail` (exit 1). This is NOT a
same-artifact native PASS. Native preflight passed on x86_64/Docker 28.0.4,
systemd/cgroup v2. The inventory assertion then rejected the actual output
`libpcre2-8-0:amd64\t10.46-1~deb13u3` because it expected an unqualified
package name. Its preceding assertion verified no C/pyc/cache files in the
executor source directory. Media/isolation/cancellation/restart phases did not
run on this image; earlier native PASS remains separate. No launcher/runtime
regression is established by this package-name assertion.

Audit nevertheless completed on the same built image (no rebuild):
`sha256:98d506693dcd7d543c9c0a22122cf9ae588235d601ab71307caa50c465c6a683`,
linux/amd64. Dockerfile unchanged in this follow-up, SHA-256
`a59147b7c1a2ffd2e85bf613c33441ca52320436a33974cb2ee2218c6bfd615d`.
Trivy 0.74.0, DB v2 UpdatedAt `2026-10-04T14:28:15.152965+00:00`.
Audit `needs_owner_decision` (exit 2): **0 fixable HC**, CVE-2026-103111 absent;
213 unfixed HC instances / 47 unique IDs. No VEX/exceptions or residual approval.

Artifact `sec08-media-executor-37226784669-1`, ID `11312570063`:
- archive digest: `5dce596751b14c6b37af77785a45d3b3e21cfedc377c0d0bfb0ebfe14c96f6c6`;
- result.json: `9a3f7419fb71029a697a378f274d337eb06da0da7bed470ccb4e1abbc9d63f39`;
- native/result.json: `9f365034f3f43ca999d2342379f217910693ed17a2ba38b7427dda6d179c2bf7`;
- audit/summary.json: `808405d6843fed6f868424668344290262c4f82e111f3a4bd5e857e0195de92c`.
Downloaded result hash matches hashes.json. Both cleanup error lists empty;
independent cleanup, export and artifact upload succeeded.

No retry, second push, runtime/Dockerfile/audit-policy changes, PR/merge/deploy
or production access. This result entry remains local and unstaged. The exact
next harness fix is architecture-aware dpkg inventory parsing with regressions
that still reject wrong package/version/architecture; not applied in this
bounded trigger-only task. Another remote run requires authorization.

Historical feasibility notes stay in `sec-08-09-isolation-plan.md`. This file
is the implementation contract. It is not production approval, not SEC-09, and
not acceptance of residual CVEs.

## Baseline

- Source commit: `af2f53a9f9b01857478f9721df2c5e1baae303bc`
- Mechanism evidence: GitHub Actions run `36927034793` (25 PASS / 0 FAIL on
  that commit). That run proved the container mechanism. It is not this
  executor and not a production-host check.

## What is switched

With `MEDIA_EXECUTOR_ENABLED=false` (the default), mux and ffprobe stay
in-process in the worker. Artifact directories stay mode `0700`.

With the flag on, only these operations go to the executor:

- `mux_copy` — ffmpeg stream copy of local files, container `mp4` or `webm`
- `ffprobe_validate` — local ffprobe JSON

`inspect_metadata` and `download_progressive` are rejected by the protocol.
yt-dlp inspection and download stay on the worker until SEC-09. The executor
container is `network_mode: none`.

There is no silent fallback. If the flag is on and the socket, Landlock,
cgroup, or protocol fails, the download fails. Turning the flag off is an
operator action.

## Privileges

| Identity | UID | Role |
| --- | --- | --- |
| Worker | 10001 | DB, secrets, lease, fence, publication, quotas |
| Supervisor | 0, PID 1 | subreaper; caps only SETUID, SETGID, SETPCAP |
| Executor socket | 10002 | binds the control socket |
| Tool | 10003 | Landlock domain, empty groups, no capabilities |

The supervisor does not receive `DATABASE_URL`, payment credentials, a Docker
socket, or the host cgroup hierarchy. It cannot mark a job READY.

Landlock ABI floor is 6, with signal and abstract-unix scope. If that ruleset
cannot be created, the process does not start. Read-only roots must not cover
the work root.

## Files

The worker attempt stays mode `0700`. The executor work root is sticky
`1777`. Each job directory is `02770`, owner 10003, group 10001, created
without `chown`. Names are fixed: `input-video`, `input-audio`, `output-mux`.
The client cannot send a path, an argv, or an environment.

The worker copies inputs in and copies `output-mux` back only after the tool
has finished and the fence is still owned. Symlinks and `..` are rejected.
A stale result after reclaim, cancel, or restart is not published.

## Cgroups and UDS

The supervisor creates `fn-<16 hex>` under the private cgroup namespace,
places the launcher there, and writes `cgroup.kill` itself. The worker sends
job, attempt, and fence only. Startup cleanup removes only those `fn-*`
names in the visible namespace.

The socket is mode `0660`, owner 10002, group 10001. `SO_PEERCRED` must be
uid 10001. Messages are one JSON line, at most 4096 bytes in and 400000
bytes out. At most two tools run; a third gets `overflow`. Repeating a
finished operation returns the cached result. Cancel of an unknown job is
`not_running`.

## Compose

`compose.yaml` does not start this service. The opt-in file is
`compose.media-executor.yaml` with profile `media-executor`. The host slice
`deploy/systemd/fetchnow-media-executor.slice` is an uninstalled template.
Its 1024M / 50% / 256 tasks numbers are not a signed-off production budget.
The older mechanism proof's 64 MiB value is not that budget either. The real
executor native acceptance uses a disposable 256 MiB ancestor ceiling for two
Python job supervisors. This is a test allowance, not a production decision.

Native acceptance is `backend/tests/media_executor/native_acceptance.py`,
dispatched by `.github/workflows/sec08-media-executor.yml`. It has not been
run. macOS cannot replace it. `workflow_dispatch` requires the workflow to be
present on the default branch (GitHub documentation); merely pushing a new
dispatch-only file to this feature branch is not an approved/verified execution
path. Before remote acceptance, separately authorize either registration of
the workflow or a narrowly scoped branch trigger. Do not merge the executor
to obtain a test run; no trigger expansion is made in this local pass.

Reference: https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow

## Local correction pass — 2026-10-04

- UID/GID changes for directory creation/removal run in fresh helper processes,
  not in the threaded RPC server. Socket binding changes identity only before
  the server starts threads. No additional capabilities or `chown`.
- Each tool invocation has its own trusted subreaper process. It drains bounded
  stdout/stderr while the tool runs, kills all remaining cgroup descendants even
  after parent exit, and waits for its own children. Server threads never call
  `waitpid(-1)` and cannot reap another operation's child.
- Independent client connections allow cancel during a running RPC. RPC failure
  also requests remote cancellation. No automatic retry/in-process fallback.
- Stale directories are not adopted after restart. A failed reserve is not
  accepted by the client. Missing exit status is not success; container type is
  validated before lookup, and cached operations include the container choice.
- Worker output handoff opens files nonblocking before checking regular-file
  type, rejecting FIFO substitutions; partial writes are completed explicitly.
- Launcher compiles with warnings as errors, fails closed on FD-inventory or
  capability-drop failure, and rejects `/` as an allowed root over protected
  paths. Runtime base now matches Candidate A's Python 3.14.7/trixie and existing
  OpenSSL pins; no app lock changed. Compiler packages stay in a build stage;
  runtime pip/ensurepip are removed. This image is NOT covered by the API image
  scan and still needs a separate artifact audit before rollout.

Native acceptance now exercises the actual server/image in separate phases:
real MP4 + WebM mux/probe/replay/release, real SO_PEERCRED, and a test-only hostile
tool mounted under `/usr/local/bin` for lifecycle checks. The hostile fixture
does not enter the image or add any RPC operation/argv field. Denial requires a
successful instrumented tool execution plus explicit EACCES/EPERM, never merely
a nonzero launcher exit. Two overlapping jobs test overflow, cancel, descendant
reaping and continued heartbeat of the other job; restart must drop cached
results and refuse the old attempt. Actual private-cgroup root identity and the
ancestor ceiling are checked. The worker fence check remains a backend test,
not a claim of end-to-end DB publication in this container-only gate.

Cleanup failures fail the gate. CI has independent `always()` cleanup and
allowlisted bounded report export to a separate readable directory with byte
hash validation. Original workspace permissions are not changed for upload.
Only result/hash reports are uploaded, not workspaces, environments, or media.

Local validation on 2026-10-04:

- Full backend: **1420 passed, 0 failed, 0 skipped** in 95.66 seconds, after
  the final runtime edits. CPython 3.12.13, uv 0.12.19 frozen dev sync into an
  isolated `/tmp` environment; pytest 9.0.3 / pytest-asyncio 1.3.0. Disposable
  PostgreSQL 16.9 on loopback, all migrations through 0010; container and its
  anonymous volume removed afterwards. The initial 1397 report was not reused.
- Focused new regression files: **22 passed** (included in 1420, not additive).
- Ruff from `backend/`, strict mypy for touched runtime modules, actionlint
  1.7.7 for the dedicated workflow, and `git diff --check`: PASS.
- Linux ARM64 image build and C compiler `-Wall -Wextra -Werror`: PASS.
  Final local Docker inspect ID:
  `sha256:ec0013e4f17d9255ec0bd278b71babd99c9702a79bfc026ec7c287b2f5f28ebf`;
  config digest from build output (distinct field):
  `sha256:784d5d84060a9ffdd9bb492ae8e1548cbe56c5a4cda01e899e631bf364d9c258`.
  Final-image smoke confirmed no runtime pip or gcc. The disposable image tag
  is removed after validation; no global prune is used.
  Local narrow-capability container smoke: directory UID/GID/mode, socket bind,
  directory cleanup; real launcher UID/GID 10003 with empty groups and specific
  EACCES on a readable sibling canary: PASS. These are partial local smoke,
  **not** native AMD64 or real-cgroup cancellation acceptance.
- Entrypoint with Docker Desktop read-only cgroups: exit 1,
  `cgroup hierarchy is not writable` (expected fail-closed result).
- Actual native acceptance, remote CI, real cgroup cancellation by this
  executor, image vulnerability scan, and production-host inventory: NOT RUN.

Unchanged SHA-256:

- `backend/uv.lock`: `db530eda51c316655b3d6d9df5da192459f334a1963ce65cd60269cc45077a36`
- `backend/ci/pytest-requirements.txt`: `ef963b8d360983d177cdd0e61655400f52edb951738c18450b3d5607564f8391`
- `web/package-lock.json`: `e62923098830723100a168e46bd8e83e0265dce38b92603f25504fb4c7fc7f6c`

No staging, commit, push, PR, SSH, production/permission/TEST/LIVE/SEO changes.
SEC-03C/VEX/residual work in the other worktree was not modified. Image-risk
acceptance and SEC-09 remain pending. This is LOCAL REGRESSION PASS, not SEC-08
acceptance or rollout approval.

The working tree contains 29 changed/new paths (3 tracked modifications,
26 new files), versus 23 before this correction. The six added paths are
`job_process.py`, `tests/media_executor/{native_inside.py,probe_tool.py,export_evidence.py}`,
and `tests/{test_media_executor_regressions.py,test_media_executor_native_harness.py}`
(all under `backend/`, with `job_process.py` under `src/fetchnow/media_executor/`).

## Still on the worker until SEC-09

Provider inspection, progressive download, and any yt-dlp network I/O.
Proxy-only networking and SSRF acceptance are out of this stage.

## Authorized native run preparation — 2026-10-04

The owner authorized committing/pushing this isolated SEC-08 branch and one
native GitHub Actions run, without merge, PR creation, deployment, production
SSH, VEX changes or residual acceptance. To avoid merging a dispatch-only
workflow just to register it, the dedicated workflow has a branch-specific
push trigger filtered to its own file. The job additionally requires the exact
commit title `feat(security): validate SEC-08 executor [native-once-20261004]`
and `run_attempt == 1`. Ordinary later pushes and reruns cannot run this job.
No other workflow is changed by this authorization. A failure is evidence,
not permission for an automatic retry. The outcome will be recorded separately;
the local and historical results above are not native executor acceptance.

## Native executor acceptance — 2026-10-04: PASS

One authorized commit and push:
`be254879fc4e4f520af680de3b3d8adfe1881b92` (29 files), branch
`codex/security-sec-08-media-executor`. No PR was created. One push-event run,
attempt 1, no dispatch/retry/second push:
[37224290556](https://github.com/DmitriiSeitsman/FetchNow/actions/runs/37224290556),
[native job 111500628925](https://github.com/DmitriiSeitsman/FetchNow/actions/runs/37224290556/job/111500628925).
Job ran 2026-10-04T18:24:02Z through 18:24:52Z, conclusion `success`.
Acceptance, independent cleanup, safe export and upload all succeeded.

Evidence was downloaded and `result.json` bytes verified against `hashes.json`:

- Artifact: `sec08-media-executor-37224290556-1`, ID `11311109013`.
- Archive digest: `0a787d1a2d96c0afa384c8131b4a537ee5214300c4272037ffdaade49b737a38`.
- `result.json`: `feb4a7e4b6b42dc51507734bb86e98a892ae10de5d6cff8512cc9fad9734f7a5`.
- `hashes.json`: `8846764bdb7d726b6460a577150c0675b63e04017a493694dda9f0faab2abbb0`.
- Both test containers used image
  `sha256:90b1e70475f1bd6e664d1c61b6d7e413bf38b7259aa7a435fcf7230838b77951`.

Runner: Ubuntu 24.04.5, native x86_64, rootful Docker 28.0.4, systemd driver,
cgroup v2. Report `status=PASS`, `cleanup_errors=[]`. Proven on this artifact:

- Real UID peer authentication rejects UID 0; worker UID 10001 succeeds.
- Real ffmpeg MP4 and WebM stream-copy, ffprobe, cached replay and release.
- Tool real/effective/saved UID/GID all 10003, no supplementary groups;
  only PATH/LANG/LC_ALL in the observed tool environment.
- Actual executed tool gets EACCES for sibling/published/symlink/proc/cgroup
  reads and control socket access, rather than an unexecuted-launcher failure.
- Two concurrent tasks, third rejected; cancel kills and reaps the selected
  tree (parent and setsid/reparented descendant absent from `/proc`), while
  the other task's parent and descendant heartbeats continue. Job cgroups removed.
- Both private cgroup roots match their host scope by device/inode; the hidden
  256 MiB ancestor ceiling remains after attempted visible-root limit change.
- Restart loses cached results and refuses stale on-disk reservations.

This is actual executor native acceptance, not a reuse of the old mechanism
proof. It is not a full backend-suite run on AMD64, a production-host test, an
executor image vulnerability scan, SEC-09 networking or end-to-end DB publication
proof. Local 1420 backend tests remain separate evidence. Pre-push targeted tests
were freshly rerun: 42 passed; the sandbox-only invocation first had two socket
bind EPERM failures, then the authorized unsandboxed local run passed. No source
test weakening. Actionlint and diff check passed. Lock hashes above unchanged.

No merge/deploy/production/TEST/LIVE/SEO/VEX/residual decisions were performed.
The result record in this section is a local unstaged documentation addition
after the successful run; no second commit or push was made. Before rollout,
the remaining artifact audit, production prerequisite/budget decisions and
SEC-09 scope still require their own acceptance.

## Same-artifact acceptance attempt — 2026-10-04: BLOCKED (job skipped)

Commit `1f128acfc4498d5083d2838c9719686c4508acd9` pushed once to
`codex/security-sec-08-media-executor`. Push-event run
[37225778248](https://github.com/DmitriiSeitsman/FetchNow/actions/runs/37225778248)
attempt 1 concluded `skipped`: the `native` job `if` compared
`github.event.head_commit.message` for exact equality to
`feat(security): SEC-08 same-artifact pcre2 fix [native-once-20261004b]`, but
the published commit message included an automatic
`Co-authored-by: Cursor <cursoragent@cursor.com>` trailer, so the gate did not
start.

No native acceptance and no same-artifact Trivy audit ran for this image fix.
No second push, dispatch, or rerun was performed. Prior native PASS
`37224290556` on `be254879…` remains a separate successful result and is not
reused for the fixed image. Local offline same-artifact regressions passed
before the push; this section is a local documentation addition after the
skipped run.
