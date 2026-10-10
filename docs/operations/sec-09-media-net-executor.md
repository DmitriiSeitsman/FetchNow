# SEC-09 — media-net executor (corrective pass №2)

Status: **CORRECTIVE PASS №2 COMPLETE / LOCAL ACCEPTANCE PASS / READY FOR CODEX REVIEW**
Date: 2026-10-10. Worktree `/Users/dina/projects/FetchNow-sec09`, branch
`codex/security-sec-09-network-isolation`, uncommitted on top of
`022d83f901d22ffa8a0132c35e7128ba1d0f9b31`.

## Corrective pass №2 (R1–R7)

| ID | Fix | Local regression |
|----|-----|------------------|
| R1 | `MAX_MIN_FREE_BYTES` (32 GiB) separated from artifact ceiling | worker-default min_free accepted; boundaries |
| R2 | `net_client` always release after cancel/wait; cleanup error distinct | public `run_inspect_metadata` / `run_download_to_dir` |
| R3 | `job_process` measures via identity helper process; fail closed | helper path + LocalRunner fail-closed; native DAC NOT_RUN on macOS |
| R4 | Native N1–N13 without constant `True`; disposable mock origin/DNS/yt-dlp | offline harness verdict tests; native Compose NOT_RUN locally |
| R5 | Audit keeps scanner exit codes; N13/residual not auto-PASS | three `scanner_timeout` → FAIL; missing Trivy → NOT_RUN |
| R6 | Killable subprocess DNS; no `setdefaulttimeout` | timeout/slots/no global mutation/no dial-after-timeout |
| R7 | Artifact stat in helper process; no handler `setresgid` | concurrent discovery; failure → `done` not stuck `running` |

Native Linux AMD64 Compose (N1–N13) and Trivy same-artifact audit were **prepared** in
`backend/tests/media_executor/network_acceptance.py` but **NOT RUN** on this macOS/arm64 host.


## Mode

Default-off: `MEDIA_NET_EXECUTOR_ENABLED=false`. When enabled, worker uses UDS to
`media-net-executor` through `egress-proxy` (CONNECT :443 only). No in-worker yt-dlp fallback.

## Local gates (this pass)

- `uv lock --check` (uv 0.12.19) + frozen sync
- Full backend pytest with disposable `postgres:16.15`: **1499 passed**, **2 skipped**
  (ffmpeg/ffprobe absent in test container)
- Ruff + mypy on executor/proxy paths; `actionlint` on sec09 workflow; `git diff --check` clean
- Image builds + import smokes (arm64 local); compose config render
- Staging: empty

## NOT RUN / residual

- Native Compose N1–N13 on linux/amd64 root host
- Trivy same-artifact audit (N13) — residual owner acceptance never automatic
- Production DAC byte-monitor proof requires native job_process path (LocalRunner ≠ production)

## 2026-10-10 — Codex native-harness corrections

The six review findings were corrected locally. This does **not** replace the
earlier backend acceptance or establish native/image-audit PASS.

- The proxy no longer installs packages at runtime. The root disposable host
  harness adds the public canary address only inside the proxy network namespace
  using host `nsenter`/`ip`; its audited filesystem stays read-only, with no new
  proxy capabilities. The address is recreated after proxy restart.
- Mock DNS has an explicit Python entrypoint. The mock yt-dlp executable uses
  `/opt/venv/bin/python`, independent of the sanitized PATH.
- All Compose operations include the active disposable overlay and explicit
  task image tags. Socket readiness requires a successful worker-uid RPC, not
  merely an existing socket file.
- N9 uses adversarial byte growth, stdout flooding, and a blocking timeout
  fixture. It requires the specific stop outcome and reaped parent + setsid
  descendant, not voluntary `--max-filesize` compliance or an unrelated error.
  The disposable download deadline is 20 seconds, not a production default.
- N10 starts two operations, waits for both live trees, cancels A, checks exact
  identities/reaping, and observes both B heartbeats advance before permitting
  B to finish. Cancelling a reservation alone cannot pass.
- N11 invokes the real `DownloadExecutor` watchdog against the native RPC
  executor with only the repository's lost-lease answer injected. It must raise
  `_LeaseLostError` and terminate/reap the operation. It also restarts the executor,
  requires the old operation to return `not_reserved`, and runs a fresh fence.
  This is **not** a PostgreSQL READY/publication transaction integration proof;
  database ownership remains a worker responsibility. No server high-water fence
  rule was invented.
- Confined tools do not read `/proc`. The outside observer captures each PID's
  starttime while alive, before growth/cancellation, and rejects PID reuse and
  zombies. Fixture processes deliberately leave their children to the trusted
  production cgroup supervisor.
- The manual native workflow prepares pinned uv 0.12.19 / Python 3.12 for N11.
  Its frozen dev environment is temporary, outside uploaded evidence, and removed
  after the worker proof. No app locks or audit policy were changed.

Local verification uses the existing diagnostic environment (Python 3.12.13,
pytest 8.4.1), **not** a newly synchronized locked pytest 9.0.3 environment.
New harness regressions: **36 passed**. Expanded local SEC-08/09 regression suite:
**172 passed**, no skips. Ruff, mypy on the three harness/helper modules,
actionlint, and `git diff --check` pass. An earlier expanded invocation had two
Unix-socket bind failures caused by sandbox EPERM; the same suite passed outside
the sandbox. Neither invocation was a native container run.

Native Compose, Trivy, full backend+PostgreSQL reacceptance, and production:
**NOT RUN in this corrective pass**. Existing 1499/2 evidence remains historical.
Residual acceptance/VEX/TEST/LIVE/SEO unchanged. Staging empty; no commit/push/PR.

Current status: **HARNESS CORRECTIONS / LOCAL REGRESSION PASS — NATIVE EVIDENCE PENDING**.

### Authorized native preparation (2026-10-10)

The disposable overlay overrides both executor `cgroup_parent` values with its
own unique `/run` slice, never the production slice. Preflight requires rootful
Docker >=28, systemd driver, cgroup v2 and native AMD64. The proof slice has a
2 GiB memory ceiling and TasksMax 256 (test budget, not production approval).
Its actual nested cgroup path is obtained from systemd ControlGroup and verified.
Exclusive creation refuses to adopt an existing unit. Cleanup stops/removes only
the unit recorded as owned by this proof, after the disposable Compose teardown.
No additional application privileges or security-policy exceptions are introduced.

Fresh pre-push regression: **175 passed**, no skips (same diagnostic environment
as above). Ruff, source-resolved mypy, actionlint and `git diff --check` pass.
The three added regressions cover nested ControlGroup resolution, exclusive unit
ownership and refusal to clean up a foreign slice. The existing three-scanner
timeout regression explicitly reaches all three mocked scans after preflight.

### Native run 38082649102 — FAIL, Trivy scan NOT RUN

One commit/push: `594a2897359f76a81b7b3c2a06a5fd3b95a200da`; event `push`,
attempt 1. Run: https://github.com/DmitriiSeitsman/FetchNow/actions/runs/38082649102
Job ran 2026-10-10T20:09:51Z–20:11:34Z. No rerun/second dispatch.

Preflight verified native x86_64, rootful Docker 28.0.4, systemd cgroup driver,
cgroup v2. The unique proof slice's resolved ControlGroup was
`/fetchnow.slice/fetchnow-sec09.slice/fetchnow-sec09-net.slice/fetchnow-sec09-net-7fec8347.slice`,
with memory.max 2147483648. All three image builds and Compose startup completed.

- Network executor: `sha256:44898245dd665f2bd766c54bc1959ae91d30c131a77b23d3a8e3fb160230ab13`
- Proxy: `sha256:efca94733372d9c00b3485c16d4121cb706016f141ec35f838e4b9dc16ea862c`
- Offline executor: `sha256:e6b7f69048c497827185221d430fb8c4e4301bf3cce904c9faf89b0bbe41f8ca`

At 20:11:09Z the matrix raised `OSError: fixture tree did not start`.
The harness did not preserve partial matrix checks or the failing operation's
response/child stderr; saved checks are `{}`. Therefore no individual N1–N12
PASS count is claimed, and the exact cause of the missing tree is UNKNOWN.
This is not evidence that isolation, cancellation or the executor passed.

Trivy 0.74.0 installation/checksum verification passed, but no image scan ran:
the exception exited the matrix before N13. No new CVE/residual verdict exists.
Cleanup reported `cleanup_errors: []`; the separate cleanup and evidence-upload
steps succeeded. Runtime images were disposable and were removed by cleanup.

Downloaded result.json SHA-256 (matches uploaded hashes.json):
`5c60bb9a935d23c1acc4b196e24e0656ea5a9c495d1cfd4c5f42530ab36b089f`.

Native acceptance **BLOCKED**; same-artifact audit **NOT RUN**. Production,
TEST/LIVE/SEO, VEX and residual acceptance remain unchanged. No PR/merge/deploy.
This run record is a local, unstaged documentation append, not another push.

### Fixture cwd diagnosis and bounded evidence correction

The trusted job process starts the launcher with cwd `/`; the launcher does not
change cwd. The synthetic controlled writer used relative parent/child marker
paths, unlike the server's absolute output path. Its child therefore attempted
to create `/fixture-child.json`, outside the writable attempt. Old local tests
had hidden this by starting in the attempt directory.

Local read-only Linux container reproduction as uid 10003 from `/` raised
`OSError: [Errno 30] Read-only file system: fixture-child.json`. This proves the
fixture defect; the original native run did not retain stderr and is not
retroactively assigned an independently observed exception.

The controlled writer now resolves its output path and enters that parent
directory before spawning the child. Production launcher, permissions, cgroup,
Landlock, network policy and application code are unchanged. A local ARM64
Landlock launcher smoke (three original capabilities, network none, read-only
root) completed with parent and child markers in the attempt and 4096 output
bytes. This is cwd proof only, not native AMD64 cancellation acceptance.
An initial smoke-driver import failed because the script has no `.py` extension;
using explicit SourceFileLoader corrected the diagnostic driver, not the runtime.

Native RPC diagnostics now retain allowlisted status fields, stream byte counts
and SHA-256, and bounded/redacted stderr (URLs/credential patterns removed).
No request URL or stdout contents are retained in that diagnostic stream.
Partial matrix verdicts survive exceptions; fixture-start failure saves the
last process snapshot. An interrupted matrix explicitly marks N13 NOT_RUN,
never clean. New regressions cover out-of-attempt cwd and evidence/privacy.

Fresh expanded regression: **178 passed**, no skips in the diagnostic Python
3.12.13/pytest 8.4.1 environment. Ruff, source-resolved mypy, actionlint and
`git diff --check` pass. Dependency lock hashes remain unchanged. The prior
run's formerly local record is included in this corrective commit, not rewritten.
One new native run is authorized for this correction; no automatic rerun.
