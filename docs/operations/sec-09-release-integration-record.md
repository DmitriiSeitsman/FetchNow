# SEC-08/09 Release Integration — Implementation Record

Status: **NATIVE ACCEPTANCE RETRY** (workflow bootstrap fix after run 38146059444)
Not READY FOR PRODUCTION / ROLLOUT. Residual acceptance / VEX unchanged.
Date: 2026-10-11. Worktree `/Users/dina/projects/FetchNow-sec09`.

### Prior native attempt (retained)

- Run: https://github.com/DmitriiSeitsman/FetchNow/actions/runs/38146059444
- Verdict: **workflow bootstrap FAIL; native matrix NOT_RUN**
- Cause: same-step `uv --version` after `$GITHUB_PATH` append (exit 127)
- SHA: `a9c67492eb34eee867172ad0f9d92d6e309f592b`
- New run does not rewrite this result.

### Bootstrap fix (this commit)

- Absolute `"$RUNNER_TEMP/${asset%.tar.gz}/uv"` in the install step; `$GITHUB_PATH` kept for later steps.
- Matrix step no longer pre-creates the evidence leaf; harness `exist_ok=False` retained.
- Missing `result.json` fallback creates the directory only then, writes FAIL/`native_result=absent`, preserves original exit code.
- Exact-subject marker: `[native-once-20261011-uvpath]`.
- Offline tests: `tests/release/test_sec09_release_workflow_bootstrap.py`.

## Contracts implemented

| Contract | Detail |
| --- | --- |
| Source contract **V4** | Required + hashed media overlays/Dockerfiles (+ slice unit required path) |
| `release_mode` | `canonical` (default) \| `media` — selects image set; never inferred from files |
| `media_activation` | Explicit rollout flag; default OFF = source-only |
| Previous-state rollback | Journal `media.previous_*` + `state/media-activation.json` |
| Config classification | Executor/deny keys in `INFRASTRUCTURE_ACTIVATION_WIRING` (not allowlisted) |
| Prerequisites | Rootful cgroup v2/systemd, Landlock ABI≥6, installed slice ControlGroup |
| Resource budget | Explicit JSON; `approved` required; disposable test budget separate |
| Deny coverage | Required CIDR/IP set must be covered (not mere non-empty) |

## Modes

1. **Source-only (flags OFF)** — canonical images; no media overlays/profiles; sidecars/slice not required; muxing untouched.  
2. **Explicit media activation** — media-mode release + `--media-activation` + budget + deny targets + denied-host proof; sidecars before worker; fail-closed.

## Initial implementation gates (historical, superseded below)

| Gate | Result |
| --- | --- |
| `tests/release` (full) | **479 passed** |
| `tests/release/test_media_release_unit.py` | **16 passed** |
| `tests/release/test_source_contract_unit.py` | included above |
| `tests/release/test_production_compose_contract_unit.py` | **4 passed** |
| `scripts/release_media_activation_integration_test.py` | **NOT_RUN** (Darwin; native Linux required) |
| Ruff (new media modules) | **All checks passed** |
| `git diff --check` | clean |
| mypy (sample: release_mode/resource_budget/egress_deny) | egress_deny typed after fix |
| pg_backup suite | **NOT_RUN** (no pg_backup schema/API change; journal media additive) |
| Remote native CI | **NOT_RUN** (forbidden in this pass) |
| Backend settings behavior change | **none** (classification only); backend pytest **NOT_RUN** |

## Initial implementation file hashes (historical, superseded below)

| Path | sha256 |
| --- | --- |
| `scripts/fetchnow_release/release_mode.py` | `9e10e3f9240913615a3c0dd17b0bf4f2b1b848978345fb5c354361d594526dd1` |
| `scripts/fetchnow_release/media_activation.py` | `fd8f63dfca71b66147fbfff024ee5dd307f364061e075c546e4e34e1d3e3a377` |
| `scripts/fetchnow_release/media_health.py` | `bdeb145fe38faa9ebfca71e9b712d8f2fe2dbb2314ff3b37a29ef1b04332d064` |
| `scripts/fetchnow_release/media_prerequisites.py` | `9bb87015cdb855d23b11839625f14bb749f7acee18f8f1e06386c4387070764e` |
| `scripts/fetchnow_release/media_rollout.py` | `b2098fc357668ea14dab6a4d5789dce21dd99a35f3230e70385ac2f7335f7624` |
| `scripts/fetchnow_release/media_state.py` | `0a465e29ed2222d00fc2e9faf01d6d954e4977f3c8a084d3511ec94240653b76` |
| `scripts/fetchnow_release/resource_budget.py` | `6f662d8a4f62fbb360ff1199bb90641acf979d0cf502209ecc1f304e7fc196d9` |
| `scripts/fetchnow_release/egress_deny_coverage.py` | `d53e6eceb989594770d8709a8cbc4744b53957340e5d122a4af30d7ee5baa6ad` |
| `scripts/release_media_activation_integration_test.py` | `d870fdfcfdab1c656683dfaf3e567b6e89520105949e2bc0385e85b7da8ed6eb` |
| `tests/release/test_media_release_unit.py` | `873629b59bbca42c0384b4a646dd166d9d5fb2290006ab7e2e0150386398f211` |
| `docs/operations/sec-09-release-integration-planned-manifest.md` | `afd59b0f2d265910231995ec3d4e4db4127841ddbdae9211a5da86fd7a5133eb` |

## Remaining external gates (not this pass)

1. Landlock ABI ≥6 disposable probe on production host  
2. Install/enable production `fetchnow-media-executor.slice` (not done here)  
3. Approve production resource budget (1 GiB template **not** auto-approved)  
4. Controlled production `EGRESS_DENY_*` covering inventory networks  
5. Media-mode prepare of the exact release candidate + native Trivy/residual gate  
6. Owner residual acceptance (50 / 8) — still **not** granted  
7. Native Linux disposable activation/health/rollback harness execution  

## Rollback semantics (implemented)

Rollback restores **previous** media authority from the journal:

1. OFF → ON failure → previous OFF (stop sidecars started by the attempt; slice retained)  
2. ON → ON failure → previous ON images/settings  
3. OFF → source-only failure → legacy application rollback  
4. Unresolved journal → recover uses the same previous-media restore path  

`MEDIA_MUXING_ENABLED` is never rewritten. No global prune. No host-slice uninstall.

## Codex corrective pass — 2026-10-11

The initial review found ten transactional/acceptance gaps. This pass fixes them
locally; it does not grant residual acceptance or authorize deployment.

1. Source overlays precede generated settings; immutable image overrides remain
   last. Recomposition deduplicates overlays and the settings file.
2. Worker executor flags/socket/work-root settings are rendered into a mode-0600
   transaction override rather than only recorded in a journal.
3. Approved per-container memory and slice placement are applied, then compared
   with live inspect and host cgroup paths. Aggregate memory/CPU/tasks are checked
   against the real ancestor files, not systemctl display ordering or defaults.
   `concurrency` is a limit **per executor**, validated in 1..2 and applied via
   infrastructure key `MEDIA_EXECUTOR_MAX_JOBS`; the old default remains 2.
   The slice ceilings cover the combined sidecars. No production budget approved.
4. Executor health runs as worker uid 10001 and requires `ok=true` plus exact
   reserve/cancel/release response codes. Unauthorized/error responses fail.
5. Same-SHA OFF-to-ON is a transaction, not an already-active shortcut. Active
   idempotency revalidates configuration and live media health.
6. Recovery accept-target reconstructs media settings/budget and verifies
   prerequisites, identities, scopes, protocol and worker flags before publishing.
7. OFF-to-ON rollback removes only owned target sidecars using the target source
   overlays. Cleanup errors remain errors. ON-to-ON restores previous settings,
   budget, IDs and health before publishing previous media authority.
8. Bootstrap cleanup includes exited/running owned media sidecars; PostgreSQL and
   foreign deployments are not included.
9. The journal retains the exact previous current-state snapshot. If media-state
   publication fails after current.json, verified rollback restores that snapshot;
   failed bootstrap retracts only matching owned publications. The two files are
   not claimed to be an atomic pair; unresolved transactions require recovery.
10. The placeholder native script is replaced with a disposable activation matrix:
    OFF bootstrap, same-SHA OFF-to-ON failure/success, idempotency, ON-to-ON
    failure/success, post-current publication failure and accept-target checks.
    Unsupported/dirty hosts return NOT_RUN with exit 2, never PASS. The recovery
    matrix calls the accept-target execution helper; the public recovery journal
    wrapper still needs its corresponding end-to-end gate.

### Corrective scope extension

In addition to the existing release-integration files: bootstrap cleanup;
executor `server.py` / `__main__.py` and protocol tests for a reduced concurrency
limit; new `tests/release/test_media_transaction_regressions.py` (26 tests).
The two executor changes only make an explicitly approved lower limit effective;
no default flag, CVE policy, application dependency or muxing semantics changed.
Total pending manifest: 41 paths, including preserved earlier dirty docs.

### Fresh local evidence (pre-native commit)

- Release + pg_backup: **572 passed** (505 release, 67 pg_backup), fresh, no skips/failures.
  The 26 transactional regressions are inside the release count.
- Executor protocol: **16 passed** (fresh). Transactional regressions alone: **26 passed**
  (fresh; also inside release 505). Prior “27” label is superseded by these counts.
- Ruff on corrective/harness/media modules: pass. Mypy (11 modules incl. harness
  path + executor server/main): pass. `git diff --check`: clean.
- Native matrix on Darwin: NOT_RUN / exit 2 (no Docker/slice artifacts).
- New workflow: `.github/workflows/sec09-release-media-activation.yml` (push-only,
  exact first-line subject, `run_attempt == 1`, does not replace network acceptance).
- Trivy / residual / production budget / slice install: **not** this stage.

### Evidence limits

- Accept-target matrix calls `_execute_accept_target` helper; that is **not** an
  end-to-end public `recover` CLI/journal wrapper proof.
- Native PASS does not replace audit of the exact candidate images or owner
  residual decision. No new Trivy in this stage.

### Current dependency hashes (unchanged)

- `backend/uv.lock`: `db530eda51c316655b3d6d9df5da192459f334a1963ce65cd60269cc45077a36`
- `backend/ci/pytest-requirements.txt`: `ef963b8d360983d177cdd0e61655400f52edb951738c18450b3d5607564f8391`
- `web/package-lock.json`: `e62923098830723100a168e46bd8e83e0265dce38b92603f25504fb4c7fc7f6c`

### Corrected artifact hashes (SHA-256)

- media_activation.py: `a0fdcd55d2acfca89c7f9a58771f220a7049687b0b4d00b1a9cce22af33c1e58`
- media_health.py: `09d2d96f26b1250e16e4c37a234bbf6bb139db97c928da118fe1e457a66734cb`
- media_prerequisites.py: `2a777201b348016e0cd65deb067ff459657a21542a1f5f2f771e488eeb0223ca`
- media_rollout.py: `c3b2e7c7f3298f1e6b9bdbb76fb4b4e90030943105fd26020d4c51935e483d53`
- media_state.py: `e09d5aa2060b38d7c8e43ec55e22fea4f298d5466666c0821de706b5a71cdbfd`
- resource_budget.py: `f8dfe0d6cdb837589fcfa986c1c9faf8ba93ba6b8949bd6c40814651f2f28649`
- release_media_activation_integration_test.py: `cb713c4ecf9a586f855e1988996478ff9d3798b727ccd05035079db967e5bfa6`
- sec09-release-media-activation.yml: `bc8b4597e1d288ca1c41ce86267491685313d729b82634351586ab7ff91ebbe8`
- test_sec09_release_workflow_bootstrap.py: `c70f791f065bc217717ccd17e336f36e6748b20929cc0fd3fe2884c8e980cc78`
- test_media_transaction_regressions.py: `dd3bef8a58350443d7167f34545427f9a79160fab083db1566b07120911be0ef`

**STOP:** local corrective evidence only. Native release acceptance, current
candidate image audit, production prerequisite/budget approval and residual-risk
decisions remain separate gates. Production/TEST/LIVE/SEO/VEX unchanged.
