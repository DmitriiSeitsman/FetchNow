# SEC-08/09 — acceptance / pre-rollout pass, 2026-10-15

**LOCAL REVIEW COMPLETE / PRODUCTION READ-ONLY PREFLIGHT COMPLETE / ACTIVATION NOT READY**

Owner residual decision: **APPROVED**, subject to technical prerequisites.
Effective residual policy: **NOT ACTIVATED**. READY FOR ROLLOUT: **NO**.
Approval is not a deployment authorization or proof of containment.

Latest status: the production authentication blocker was resolved by the owner;
read-only inventory completed and the six specifically authorized loopback tests
passed. Both follow-ups are recorded below. Original failures remain historical;
no complete 833-test rerun or production activation PASS is claimed.

## Baseline and manifest

- Worktree `/Users/dina/projects/FetchNow-sec09`.
- Branch `codex/security-sec-09-network-isolation`.
- HEAD `de54d9265af3febb8826f4ca127cb76aa91e4e88` unchanged.
- Starting scope: 10 untracked files (the 9 pre-existing gosu files plus residual
  disposition). Staging empty; tracked diff empty.
- This session adds exactly **2 files**:
  - `docs/operations/sec-03c-candidate7-residual-approval-47.json`;
  - `docs/operations/sec-08-09-acceptance-pre-rollout-20261015.md` (this report).
- Existing files were not edited. No app/lock/Dockerfile/Compose/workflow edits.
- Final scope: **12 untracked files**, staging **none**. Main checkout, protected
  files and backups untouched. No evidence of secret/raw-report/build-artifact
  additions to the intended scope.

## Owner decision recorded, not silently applied

The owner's messages were:

> риски приняты, дальше что делаем?

> давай првоедем acceptance и pre-rollout pass

Separate local record:
`sec-03c-candidate7-residual-approval-47.json`, SHA-256
`4f2e6600dc1e6917e42cd9c9d7b6fdee5f64aab13ac04cc3a595a3b5f8f03a4f`.

Record UTC: `2026-10-15T20:18:03Z`; expires
`2026-10-29T20:18:03Z`. These are local recording/expiry times, not an asserted
exact timestamp of the user message. Completing prerequisites does not extend
the window. The record is not a cryptographic signature or executable gate.

Fresh consistency verification **PASS**:

- exact 47 advisory IDs, no duplicates/missing/extra IDs;
- exact **514 distinct package/image tuples**, not merely matching counts;
- four exact linux/amd64 image identities, per-target counts 213/213/44/44;
- zero accepted fixable rows; no new `not_affected` classifications;
- source summary and disposition hashes match; 14-day window matches;
- effective status remains `PENDING_TECHNICAL_PREREQUISITES`;
- no authorization for production numeric budget, host changes, unbounded
  shared-disk impact, secret-bearing-role compromise, commit/push/merge/deploy.

The earlier disposition and gosu approval retain their historical statuses.
This new record documents the later conditional residual decision; it does not
rewrite the original failed audit, the source findings or the gosu-only approval.

## Independent Security Review

Exactly one requested Security Review subagent reviewed the **10 starting
untracked files**. Confirmed actionable security findings: **0**.
Fresh independent offline application/dry-run tests: **83 passed** (28 +55).

Coverage: gosu identity/advisory/expiry/evidence-hash/completeness boundaries,
failure behavior, raw-finding preservation, approval vs CI/production authority,
residual disposition conditions. No production or full release architecture
review is claimed. The two new decision/report files were created after this
review and have **main-agent validation only**, not a second independent review.

Important limit retained: operational use must take real UTC. The CLI `--now`
test clock is not an approved way to extend a mandatory gate's validity.

## Fresh local verification — one combined test run, not PASS overall

Command scope: offline `tests/image_audit`, `tests/release`, `tests/pg_backup`;
Python 3.12.13 on darwin/arm64, bytecode/cache writing disabled for pytest.

Result: **827 passed / 1 failed / 5 errors**, exit **1**, **34.04 s**.
The six non-passing tests all failed to bind a disposable loopback HTTPS
listener with `PermissionError: [Errno 1] Operation not permitted` in the
restricted execution sandbox. No application assertion was reached for these
listener-dependent checks. This is an **execution-environment failure**, not
evidence of a source fix and not a suite PASS. No automatic rerun was made.

- FAIL: `test_missing_content_length_cannot_bypass_bounded_read`.
- ERROR: `test_homepage_body_between_4kib_and_64kib_passes`.
- ERROR: `test_homepage_body_over_64kib_fails`.
- ERROR: `test_homepage_declared_content_length_over_64kib_fails_early`.
- ERROR: `test_json_live_ready_still_capped_at_4kib`.
- ERROR: `test_public_https_shared_deadline_remains_bounded`.

All six are in `tests/release/test_routing_health_unit.py`. No assertions,
timeouts or security policy were weakened. Successful tests are not a substitute
for the incomplete acceptance of the suite. A controlled follow-up run needs
permission to bind the local test port; do not call non-reproduction a code fix.

Fresh Ruff on the four untracked Python implementation/test files: **PASS**.
Fresh strict mypy on the two gosu helper modules: **PASS**.
Fresh actionlint on candidate-7 audit workflow: **PASS** (workflow unchanged).
Fresh `git diff --check`: **PASS**.

No backend/PostgreSQL app suite, standalone integration, new native workflow,
build or advisory scan was run. Historical native/audit evidence remains reused
only within the artifact/source limits of the disposition document.

## Production preflight — authentication not ready, no SSH connection attempted

Read-only local checks outside sandbox confirmed:

- SSH-agent: **The agent has no identities**.
- `ssh -G fetchnow-prod`: alias does **not** resolve to the documented production
  target; effective hostname stays `fetchnow-prod`, user is local user `dina`.
  No attempt was made using that unintended identity.
- The previously used `/Users/dina/.ssh/fetchnow-prod` key file exists and is
  readable. **Contents not read**, key not loaded by the agent.
- No user/key guessing, config edits, password prompts, host-key changes or SSH
  connection attempts. The user was given local `ssh-add` instructions.

Documented intended target remains `ubuntuuser@195.209.215.87`, pending the
owner's confirmation/unlocked agent. The 2026-10-11 inventory is history, not
a fresh verification of host, flags, topology, images or release authority.

Still unproven: current live revision/images/retained PostgreSQL, Landlock ABI,
private cgroup mount behavior, installed slice and approved aggregate/per-service
budget, production deny coverage and routing, shared-disk consequence boundary,
and UNKNOWN-library caller attribution outside media sandbox.

Unlocking a key will authorize authentication for the requested read-only pass,
not installing a slice, running containers/probes that mutate the host, changing
flags/firewall, merging or deploying. Such actions are not done here.

## Integrity hashes

- Source audit summary:
  `163d272b35cf43b06efd6888e2083607b01e4d612a09160e4c270a1db015448f`.
- Residual tuple projection (canonical encoding defined in approval record):
  `4cd01e8f4d04ee6e080d55e86d71e5c8c443da0e91a6d9b4558693dffc4a93fa`.
- Disposition:
  `bac319aad1d6e7cc00b18c6630d87fddccf881bd7a7d4de7fbb66c47527e62e2`.
- Existing local gosu application result:
  `9218f07ae7383098757df6b677a1cc36a3ef476365c41babd39cbbd3379047f9`.
- `backend/uv.lock`:
  `db530eda51c316655b3d6d9df5da192459f334a1963ce65cd60269cc45077a36`.
- `web/package-lock.json`:
  `e62923098830723100a168e46bd8e83e0265dce38b92603f25504fb4c7fc7f6c`.

## Verdict / next bounded step

- Gosu local security review: **PASS**, scoped saved-audit application only.
- Owner residual decision: **APPROVED**, exact snapshot / conditional / expires.
- Effective residual policy and CI integration: **NOT ACTIVATED / UNCHANGED**.
- Combined local regression: **BLOCKED BY SANDBOX LISTENER PERMISSION**.
- Production preflight: **PENDING AUTHENTICATION**; no new host evidence.
- Production readiness: **NO**. No merge/deploy authorized or performed.

Next: owner unlocks the existing agent key and confirms the documented target;
then perform one bounded read-only production inventory. Keep the six failed
local checks as separate evidence pending a controlled port-enabled run. Do not
infer green CI/security/rollout from the acceptance JSON or from historical PASS.

Staging: **none**. Commit/push/PR/remote CI/SSH/build/scan/merge/deploy: **NO**.
Production/permissions/TEST/LIVE/SEO: **unchanged**.

**STOP — ACCEPTANCE BLOCKED; NO AUTOMATIC RETRY.**

## Follow-up — production read-only preflight completed after key unlock

Owner message: **«готово, ключ добавил»**. The existing agent then held the
`fetchnow-production` ED25519 identity. No key contents were read or copied.
Target: `ubuntuuser@195.209.215.87`, not staging and not the unresolved local
`fetchnow-prod` alias. Agent-only public-key authentication, BatchMode,
StrictHostKeyChecking=yes, one connection attempt per bounded read-only command;
no password prompt, agent forwarding, sudo or automatic retry.

Two successful bounded read-only queries, both exit **0**, no diagnostic errors:

- Inventory UTC: `2026-10-15T20:22:07.009883+00:00`–
  `2026-10-15T20:22:10.287564+00:00`.
- Targeted nested release-authority/backing-filesystem read UTC:
  `2026-10-15T20:22:57.749758+00:00`–
  `2026-10-15T20:22:57.758042+00:00`.

The second query completed the schema-v2 application-state selection; it was
not an authentication retry or a repeat of a failed command. The first summary
did not select nested application fields. No authority values were guessed.

### Fresh confirmed facts

- Host Linux x86_64, kernel `7.0.0-31-generic`; unprivileged SSH uid 1001.
- Docker **29.7.2**, rootful (no rootless security option), cgroup **v2/systemd**;
  builtin seccomp and AppArmor listed. No policy was disabled.
- **Landlock host ABI 8** from `landlock_create_ruleset(NULL,0,VERSION)`.
  The version query creates no ruleset and changes no restrictions. This closes
  the host ABI-number UNKNOWN, **not** container ruleset/privilege/FS acceptance.
- `current.json` schema v2 application revision
  `2d24140434ccd53388e73744dfd276f313e76113`, deployment
  `eff53f36-ae69-42f0-99fb-8fe03b739ff0`; updated
  `2026-09-22T21:01:43Z`. Current image authority matches live OCI labels/image
  IDs for api/worker/web/gateway. Delivery shares the live API image.
- Recorded database head: `0010_successful_delivery_quota`; no database query
  or migration ran, so this is authority metadata, not a fresh DB-head proof.
- Live api/worker/delivery:
  `sha256:6d149ce0b0fa1246156ded2d1cca96eb162650ad62fb4c191d85eb8a920403ed`.
- Live web:
  `sha256:253f3746df71b20d16e72fa0dfe95997e2351e26456afdea23a1c240b3fb4cd2`.
- Live gateway:
  `sha256:dc236a481d25492f2e5614e32dd17d5b687b34ae302e09c0793538ed73ad698a`.
- Retained live PostgreSQL **16.9-alpine**, image
  `sha256:7c688148e5e156d0e86df7ba8ae5a05a2386aaec1e2ad8e6d11bdf10504b1fb7`.
  It is **not** candidate `16.15-alpine3.24` config `81bd698b…`; candidate gosu
  VEX does not cover this retained image. No live scan/extraction was performed.
- Five app-facing containers and PostgreSQL are running; services with Docker
  healthchecks are healthy, restart_count 0. Worker has no Docker health state;
  do not describe this as an executor/job functional test or public HTTP probe.
- Worker `MEDIA_MUXING_ENABLED=true`; `/usr/bin/ffmpeg`, `/usr/bin/ffprobe`,
  `/opt/venv/bin/yt-dlp` configured. Executor flags **absent**; no activation
  authority file, media sidecars, executor socket/work roots or production slice.
  Product mux flag remained unchanged.
- Slice name resolves to an inactive implicit unit, with empty FragmentPath and
  ControlGroup; MemoryMax/CPUQuota/TasksMax infinity. This is **not** an installed
  slice or approved resource ceiling. Host cgroup2 mount rw does not establish a
  future container's writable private mount.
- All five queried audited candidate identities (four residual images plus
  candidate PostgreSQL) absent from host image inventory. Exact source revision
  `de54d9265af3febb8826f4ca127cb76aa91e4e88` not prepared under production releases.
- About **15.61 GiB total RAM / 13.93 GiB available**, no swap at this snapshot.
  Existing FetchNow declared container memory limits total **2.375 GiB**.
  An idle snapshot is not a peak-load budget approval.
- Docker data on the shared root **ext4** filesystem, options `rw,relatime`;
  about **88.69 GiB available / 149.52 GiB total**. No hard quota/isolation of
  media disk consumption was proven. Free capacity does not bound malicious
  overshoot or protect co-located services.
- Current network inventory still has the FetchNow bridge, default bridge and
  the two previously documented neighbor bridges; host public address matches
  the approved target. No media internal network/proxy exists. Current runtime
  `EGRESS_DENY_*` absent. The exact topology is retained only in local evidence,
  not used to silently install firewall/config changes.

### Evidence / privacy

Sanitized generated evidence outside git:
`/private/tmp/fetchnow-pre-rollout-readonly-20261015/`.

- `production-inventory.json` SHA-256:
  `5ecdec041b00839075049913aeee16bb0c801b5e8577ac0cb90c30018d977dea`.
- `release-authority.json` SHA-256:
  `64bd23817b879c1b9bc62c6697015dc13e3b327c86e910ce1d58fd0f6151f301`.
- Live `current.json` SHA-256 (computed remotely, contents allowlisted):
  `09f85150b704f6e783f02659da4ac0d5e3f66d2a0becbd9010bcca86eace5ac7`.

Raw docker inspect, full env, secrets, private key material and app logs were
not saved. Env output was restricted to media flags/tool paths and deny entry
counts. Neighbor application configs were not inspected; only topology and
resource-usage impact data were read. Read-only SSH may create normal audit logs
on the host; no application, configuration or infrastructure mutation was made.

### Remaining bounded work — not a new open-ended security project

1. Prepare/identify the actual intended release images, keeping identity and
   residual acceptance boundaries. Current accepted images are absent on host;
   a new build must not inherit this approval without reconciliation/authorization.
2. Close host setup/activation prerequisites under separate operational approval:
   real slice, approved memory/CPU/tasks/concurrency budget, infrastructure denies,
   then container-level runtime acceptance before worker routing is switched.
   Repository overlay limits sum to **2.75 GiB**, versus the **1 GiB** slice
   template; that is not an automatically approved shared ceiling. No numeric
   budget has been chosen for the owner in this pass.
3. Confirm shared-disk consequences and UNKNOWN caller attribution outside
   sandbox; host ABI 8 and ample idle RAM do not settle either question.
4. Keep the six local listener tests non-PASS until one explicitly approved
   loopback-enabled run. No rerun has yet occurred in this follow-up.
5. Canonical CI/security adapter still does not consume the approved local gosu
   or conditional residual records. It remains a scoped implementation gate,
   not a reason to bypass checks or suppress findings.

Latest verdict: **read-only host inventory PASS / production activation NOT READY**.
Owner residual decision remains **APPROVED / NOT EFFECTIVE UNTIL CONDITIONS**,
expires `2026-10-29T20:18:03Z`. No new vulnerability scan or runtime isolation
PASS is claimed. No commit/push/CI/build/container exec/install/restart/merge/deploy.
Production/permissions/TEST/LIVE/SEO unchanged. Staging remains empty.

## Follow-up — one authorized loopback-only test run

Owner explicitly selected **«Да, один локальный прогон»** for the six tests
listed in the initial sandbox failure. Exactly one new attempt ran those six
nodes with permission to bind loopback, no production/external service access.

- UTC start `2026-10-15T20:24:52.451453+00:00`, finish
  `2026-10-15T20:24:56.499447+00:00`.
- Pytest: **6 passed in 3.56 s**, process exit **0**.
- The JUnit report independently contains exactly the six intended test names,
  no extra testcases, failures, errors or skips. Full captured pytest stdout
  also lists all six PASSED node IDs.
- No code/input/assertion change, install, automatic retry or second run.
- Metadata caveat: `loopback-six-run.json`'s convenience `test_ids` array has
  five entries because the local recording code sliced the argv one index late.
  Execution used all six supplied nodes. The original record is not rewritten;
  exact executed selection is proven by full stdout and JUnit, not that array.

Evidence, same local directory as the SSH inventory:

- `loopback-six-run.json` SHA-256:
  `2ba4507bea06b0bca8bcd99df454baec8b7cfebc0d8335e01153037777bd818c`.
- `loopback-six-junit.xml` SHA-256:
  `cef65c85b4e698b3fed5ba570c9000b6c53fc481b7ae72b2555c5c58b2591b59`.

Local acceptance coverage now has **827 passing results from the initial run +
6 passing results from this authorized follow-up**, unchanged source inputs.
That covers the 833 selected tests across two executions, **not** a fresh single
833-PASS run. The initial 1 FAIL/5 ERROR result remains intact and classified as
the sandbox listener restriction; no product source fix is claimed.

### Latest final status

- Local regression coverage: **PASS across the two bounded executions**.
- Independent Security Review: **PASS**, reused from the prior acceptance
  session, limited to its stated ten-file scope; not a host audit.
- Read-only production inventory: **PASS**; host Landlock ABI **8** confirmed.
- Container isolation on production / approved budget / real slice / egress
  configuration / disk boundary / UNKNOWN caller attribution: **NOT PROVEN**.
- Exact accepted candidate on production: **NOT PREPARED / images absent**.
- Approved local decisions consumed by canonical CI: **NO**.
- Owner residual approval: **APPROVED / conditional**, no new CVE waiver.
- Effective activation readiness: **NOT READY**; no overall SECURITY PASS.

This turn changed only this existing untracked report. All twelve untracked
files are preserved; HEAD unchanged; staging empty; fresh diff check clean.
No source/lock/Compose/Dockerfile/workflow or approval-record edits. No
commit/push/PR/remote CI/build/scan/container exec/slice install/merge/deploy.
Production/permissions/TEST/LIVE/SEO unchanged.

**STOP — READ-ONLY PRE-ROLLOUT PASS COMPLETE; ACTIVATION REQUIRES SEPARATE SCOPE.**

## Follow-up — local pre-rollout preparation package (no activation)

Later local implementation pass on the same HEAD prepared offline policy
adapters, an unapproved resource-budget proposal, activation package docs,
prerequisites checklist, mux argv dual-input whitelist fix, and limited
libxml2/libacl attribution notes. See
`docs/operations/sec-08-09-activation-package-20261015.md`.

Fresh verification this follow-up (offline only; no remote CI/Trivy/SSH):

| Suite | Result | Notes |
| --- | --- | --- |
| `tests/image_audit/` | **162 passed** | includes residual/gosu negative tests |
| release media/budget unit set | **64 passed** | 5 release modules |
| ffmpeg/mux/protocol | **30 passed** | dual `-protocol_whitelist` before each `-i` |
| Ruff (touched Python) | **PASS** | |
| mypy (argv modules) | **PASS** | |
| actionlint (candidate-7 workflow) | **PASS** | |
| Compose config w/ media profiles | **PASS** | mem_limit 1024m/1536m/256m; cgroup_parent set |
| `git diff --check` | **PASS** | |
| policy_layers on saved audit 38689849543 | exit 1 | `PENDING_TECHNICAL_PREREQUISITES`; gosu 25; residual 514; not “0 vulns” |

Prior gosu application/dry-run **83 PASS** and the 827+6 backend coverage are **reused** only where inputs were unchanged. Mux argv runtime input changed → prior native image PASS is **not** acceptance for a new candidate identity.

Worktree after this follow-up: **4 modified + 18 untracked paths** (22 porcelain entries; fixture dir expanded), staging **empty**, HEAD unchanged. Starting “12 untracked” baseline from the prior acceptance record is superseded by this preparation package's additional files.

This follow-up does **not** install a slice, approve a numeric budget, activate
residual policy, run remote CI/Trivy, or claim READY FOR ROLLOUT. Staging remains
empty unless a later explicit commit is requested.

## Follow-up — five findings corrective pass (local, no activation)

Codex/Bugbot findings F1–F5 remediated on the same HEAD without editing owner
approvals, gosu immutable evidence, locks, or CI wiring:

- F1: output/input path alias refusal + measured `source_findings_preserved`
- F2: activation-package rollback aligned with `restore_previous_media` (ON→ON keeps prior ON)
- F3: exact eight prerequisite IDs + PROVEN evidence binding; pending never exit 0
- F4: invalid/REJECTED/REVOKED/expired approval → `accepted_risk_count=0`
- F5: synthetic portable fixtures; no workstation `/tmp` audit dependency

Effective residual activation remains **not** granted. READY FOR ROLLOUT: **NO**.
