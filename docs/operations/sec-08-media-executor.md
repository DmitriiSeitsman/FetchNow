# SEC-08 media executor — current contract

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
