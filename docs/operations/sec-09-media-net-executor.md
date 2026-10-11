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

### Corrective run 38083561995 — native checks PASS in log, evidence export failed

Commit `252d115014c3c773cfaba331e585c5ce9e5d7001`; one push, attempt 1.
Run: https://github.com/DmitriiSeitsman/FetchNow/actions/runs/38083561995
Job 114305256631 ran 2026-10-10T20:23:32Z–20:26:22Z, conclusion failure.

CI stdout explicitly recorded **N1–N12: 12 PASS**. N9's stdout/time branches
did not emit a later FAIL or raise; N10 cancellation/neighbor and N11 worker
watchdog/restart proof also recorded PASS. This is log-backed native functional
evidence at the exact commit, not a recovered result.json or full SECURITY PASS.
The former `fixture tree did not start` error did not recur after the cwd fix.

The log reached same-artifact Trivy at 20:25:25Z, then the main step exited 1.
Audit status/counts/error fields and image identities were saved only in the
result, not printed. Upload failed with EACCES while scanning
`audit/network/trivy-cache/fanal`; the GitHub artifact API confirms **0 artifacts**.
Therefore the exact Trivy verdict is **UNKNOWN / REPORT UNAVAILABLE**, not clean,
not attributed to existing residuals, and not described as scan NOT_RUN.
Cleanup step succeeded. No third native run, workflow rerun or dispatch occurred.

SHA-256 of the `gh run view --log-failed` output (not result.json bytes):
`b61634b3b124abb7c987b9f814e18a6b2db4138e882a54a36ec35be9d0dd4860`.
The first run 38082649102 remains FAIL with unrecovered partial matrix evidence.

### Local-only export correction after run 38083561995

The workflow now exports a bounded exact-file report set to `public/` and uploads
only that directory. Trivy caches/raw image reports/workspaces are not traversed
or chmodded. The exporter verifies result/hash consistency, refuses symlink and
non-regular reports, caps each read at 8 MB (three audit summaries), and copies
reports with directory mode 0755 and file mode 0644. It prints a bounded native/
audit verdict summary and result digest even when the gate verdict is failure.
Failed findings, scanner exits and the overall gate remain non-zero/unchanged.

Fresh expanded local regression after export correction: **182 passed**, no skips;
Ruff, source-resolved mypy and actionlint pass. Four new regressions cover private
cache exclusion, digest mismatch, symlink rejection and workflow upload scope.
This fix is **uncommitted and not remotely tested**. No additional dependency,
privilege, VEX, exception, merge, SSH, production, TEST/LIVE/SEO changes.

Current status: **NATIVE FUNCTIONAL CHECKS PASS (CI LOG) / TRIVY VERDICT UNKNOWN /
BOUNDED EXPORT FIX LOCAL PASS — NOT READY FOR SECURITY ACCEPTANCE OR ROLLOUT**.

### Export correction finalization — 2026-10-11

Owner authorized one corrective commit/push and one new native AMD64 run with
same-artifact Trivy. Fresh local checks: **182 passed** (diagnostic Python
3.12.13/pytest 8.4.1, not a frozen-pytest acceptance claim), Ruff, mypy,
actionlint and `git diff --check` PASS. Only the workflow, bounded exporter,
export regression tests and this document are included. Locks remain unchanged.
Previous run verdicts above stay separate. No automatic retry, merge, deploy,
VEX/exception change or residual-risk acceptance is authorized by this step.
The new remote result is pending; this record does not claim a security PASS.

### Run 38124451577 — native functional PASS / image findings / export PASS

One corrective commit `e7cb87e20861361ab8777aeb022c82c80b979b46`, one push,
attempt 1, event push. Run:
https://github.com/DmitriiSeitsman/FetchNow/actions/runs/38124451577
Job 114425786517: 2026-10-11T08:20:27Z–08:23:05Z, conclusion **failure**.
No rerun or dispatch. Prior run outcomes above are not superseded.

Native x86_64 / Docker 28.0.4 / systemd driver / cgroup v2. **N1–N12:
12 PASS**, confirmed from downloaded result bytes (not just CI stdout).
Import closure checks for all three images also PASS. N11 uses the real worker
watchdog with simulated repository lease loss; its database READY transaction
is explicitly NOT_RUN, not full DB publication evidence. This native run is
not a full backend suite or production acceptance.

Trivy **0.74.0** scanned all three exact functional-test image IDs on
linux/amd64. Report image ID/platform matched local identity in every scan;
schema/inventory completeness passed, no technical scanner errors. DB v2
UpdatedAt `2026-10-11T07:05:14.103977+00:00` (same across scans).

- Network: `sha256:927e0ec87ceb26f0621574a4e24747b6efb27b0844436538dd012ae37e057d5c`;
  scanned 08:22:16Z; **0 fixable HC**, 44 unfixed HC instances / 8 unique IDs;
  `needs_owner_decision`, adapter exit 2.
- Proxy: `sha256:4a9290152270ab808fd45e674ef3a5935f3fa3b4c53604c962335149b877060a`;
  scanned 08:22:23Z; **1 fixable HIGH**, 44 unfixed HC instances / 8 unique IDs;
  `fail`, adapter exit 1. Fixable `CVE-2026-103111`, `libpcre2-8-0`
  `10.46-1~deb13u2` → `10.46-1~deb13u3`, as recorded by the scan. The proxy
  Dockerfile lacks the existing executor pcre2 pin. Remediation is NOT applied
  in this one-run export correction; it requires a scoped follow-up build/scan.
- Offline: `sha256:92628ce72b2f7b5104e0d29a40bd49fa99e3422d41c8dce6409aa60fc148c4d5`;
  scanned 08:22:30Z; **0 fixable HC**, 216 unfixed HC instances / 50 unique IDs;
  `needs_owner_decision`, adapter exit 2.

N13 policy verdict remains **FAIL**, overall exit 1. Counts are this dated scan,
not proof that prior residuals were fixed or that reachability classification
is unchanged. No VEX/exception or residual acceptance was applied.

Cleanup, bounded evidence export and upload all **PASS**; `cleanup_errors=[]`.
Artifact `sec09-media-net-38124451577-1`, ID 11696925410, ZIP digest:
`sha256:d9c6612641dea0bdefec0b6a5338991abadb6a71bbe55587d30129de635db4c2`.
Downloaded `result.json` SHA-256 (matches `hashes.json` exactly):
`656c0a87570d70cb9de2f54257b0edfdcae745606ace4afd101f1fee83116b9d`.
Local evidence: `/tmp/fetchnow-sec09-native-38124451577.vLADq6/` (temporary;
GitHub run/artifact is the independent reference, no raw scanner cache in git).

This result record is a local unstaged append after the single authorized push.
Staging empty. No PR creation, merge, SSH, deploy, production/TEST/LIVE/SEO change.
**SEC-09 — NATIVE FUNCTIONAL ACCEPTANCE PASS / SAME-ARTIFACT AUDIT FINDINGS /
EVIDENCE EXPORT VERIFIED; NOT READY FOR SECURITY ACCEPTANCE OR ROLLOUT.**

### Scoped proxy pcre2 remediation — 2026-10-11

Owner requested continuation after run 38124451577. Apply only the existing
executor pin `libpcre2-8-0=10.46-1~deb13u3` to the proxy runtime apt install;
no blanket upgrade, app lock change, policy relaxation or residual acceptance.
A regression checks the identical security pin across the three Dockerfiles.
One new corrective commit/push and native same-artifact run are planned. The
prior finding remains the prior artifact's verdict until a new scan proves the
fix on a new image ID. Native and image-risk verdicts remain separate.

Fresh local regression: **183 passed**, no skips (diagnostic Python 3.12.13 /
pytest 8.4.1, not a full frozen backend suite). Ruff, actionlint and
`git diff --check` PASS. The three dependency artifact hashes remain unchanged.
