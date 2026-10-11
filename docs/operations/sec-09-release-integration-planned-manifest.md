# SEC-08/09 Release Integration — Planned File Manifest

Worktree: `/Users/dina/projects/FetchNow-sec09`  
Branch: `codex/security-sec-09-network-isolation`  
Baseline HEAD: `e69b5d3a9ff4c779bacbf22f125d21eb90c362c6`  
Date: 2026-10-11

This document is the pre-implementation file list required before coding.
Preserved dirty docs (not part of this change set’s intent to rewrite from
scratch): `sec-09-media-net-executor.md`, `sec-09-pre-rollout-readiness.md`
(readiness will be updated after implementation).

## Design summary

| Concept | Choice |
| --- | --- |
| Source contract | **V4** (new prepares); V1–V3 remain readable |
| Image set | Explicit `release_mode`: `canonical` (default) or `media` |
| Activation | Explicit rollout/deploy-plan flag `media_activation` (default OFF) |
| current.json | Unchanged exact keys; media authority in `state/media-activation.json` + plan |
| Rollback | Restore **previous** media/canonical state (not always flags OFF) |
| Slice | Prerequisite check only; never install/remove production slice |
| Budget | Explicit validated document; conflicting 1 GiB defaults not auto-approved |
| Config rollout | Executor/deny keys classified; cannot enable via allowlisted config-only path |

## New files

| Path | Role |
| --- | --- |
| `scripts/fetchnow_release/release_mode.py` | Mode constants, expected services/refs |
| `scripts/fetchnow_release/media_activation.py` | Overlays/profiles, sidecar order, settings snapshot |
| `scripts/fetchnow_release/media_prerequisites.py` | Docker/cgroup/Landlock/slice/ControlGroup checks |
| `scripts/fetchnow_release/resource_budget.py` | Aggregate vs per-container budget validation |
| `scripts/fetchnow_release/egress_deny_coverage.py` | Required deny coverage (caller-supplied targets) |
| `scripts/fetchnow_release/media_health.py` | Socket protocol + proxy readiness (not port-open alone) |
| `scripts/fetchnow_release/media_state.py` | `state/media-activation.json` read/write |
| `tests/release/test_media_release_unit.py` | Offline unit/fixture regressions |
| `scripts/release_media_activation_integration_test.py` | Scoped disposable harness (Linux-only; Darwin NOT_RUN) |
| `docs/operations/sec-09-release-integration-planned-manifest.md` | This plan |
| `docs/operations/sec-09-release-integration-record.md` | Post-impl evidence record |

## Modified files

| Path | Role |
| --- | --- |
| `scripts/fetchnow_release/c2_constants.py` | V4 required/hash files; media build service names |
| `scripts/fetchnow_release/source_contract.py` | V4 dispatch |
| `scripts/fetchnow_release/manifest.py` | `release_mode`; mode-aware image validation |
| `scripts/fetchnow_release/image_build.py` | Mode-selected compose build + refs |
| `scripts/fetchnow_release/image_identity.py` | Inspect media images when mode=media |
| `scripts/fetchnow_release/override.py` | Pin media services when activating |
| `scripts/fetchnow_release/prepare.py` | `--release-mode` / PrepareInput |
| `scripts/fetchnow_release/verify_release.py` | Verify manifest image set by mode |
| `scripts/fetchnow_release/cli.py` | prepare `--release-mode`; rollout `--media-activation` + budget/deny args |
| `scripts/fetchnow_release/journal.py` | Additive optional `media` on PlanDocument |
| `scripts/fetchnow_release/c3_constants.py` | `STATUS_ACTIVATING_MEDIA` transition |
| `scripts/fetchnow_release/deploy_plan.py` | Record mode/activation prerequisites in plan JSON |
| `scripts/fetchnow_release/rollout.py` | Source-only vs activation; previous-state rollback |
| `scripts/fetchnow_release/recover.py` | Restore previous media authority |
| `scripts/fetchnow_release/health.py` | Delegate media checks when activation expected |
| `scripts/fetchnow_release/activate.py` | Profile-aware sidecar activation (no postgres) |
| `scripts/fetchnow_release/config_contract.py` | Classify executor/deny keys; forbid config-only ON |
| `scripts/fetchnow_release/environment.py` | Media overlay resolution helpers if needed |
| `backend/Dockerfile.media-executor` | OCI revision LABEL + ARG |
| `backend/Dockerfile.media-net-executor` | OCI revision LABEL + ARG |
| `backend/Dockerfile.media-egress-proxy` | OCI revision LABEL + ARG |
| `compose.media-executor.yaml` | build.args FETCHNOW_RELEASE_REVISION |
| `compose.media-net-executor.yaml` | build.args for media-net + egress-proxy |
| `tests/release/test_source_contract_unit.py` | V4 + CURRENT_PREPARE expectations |
| `docs/operations/sec-09-pre-rollout-readiness.md` | Post-impl readiness update |

## Explicitly out of scope (STOP if requested)

- commit / push / PR / remote CI
- SSH / production mutations / slice install / flag enable / deploy
- VEX / residual acceptance
- changing `MEDIA_MUXING_ENABLED` semantics or production deny hardcoding
- privileged / CAP_SYS_ADMIN / docker.sock / host cgroup bind
- moving changes into main FetchNow checkout

## Corrective review extension — 2026-10-11

The original list above is historical planning, not an assertion that every
candidate path changed. Current pending manifest is 41 files (27 modified,
14 new), including preserved SEC-09 documents. Additional corrective files:

- `scripts/fetchnow_release/bootstrap_cleanup.py`: label-scoped media service
  cleanup, including exited containers.
- `backend/src/fetchnow/media_executor/server.py` and `__main__.py`: apply the
  approved per-executor concurrency ceiling, range 1..2, unchanged default 2.
- `backend/tests/test_media_executor_protocol.py`: invalid/reduced ceiling cases.
- `tests/release/test_media_transaction_regressions.py`: 26 transactional
  regressions covering overrides/settings/budgets/health/recovery/publication.

Actual checks and evidence boundaries are in the implementation record's Codex
corrective section. No production budget, residual exception or native release
acceptance is implied by this manifest.
