# SEC-08/09 media storage/resource — native acceptance gate

Disposable Linux AMD64 acceptance for the storage/resource corrective pass.

## Scope

- Orchestrator: `scripts/native_media_storage_resource_acceptance.py` (S1–S9)
- Filesystem-only component: `scripts/native_media_storage_acceptance_harness.sh` (exit 78; not a green gate)
- Workflow: `.github/workflows/sec08-09-media-storage-resource-acceptance.yml` (`workflow_dispatch` only)

## Authorization

- Production storage/budget proposals remain `approved: false`
- Disposable contract/budget approvals exist only inside test evidence
- Does **not** authorize production migration, SSH, merge, deploy, VEX/residual activation, or owner budget approval

## Required cases

| ID | Meaning |
|----|---------|
| S1 | Linux AMD64, rootful Docker, cgroup v2/systemd, Landlock ABI ≥6, identities |
| S2 fault | Small dedicated FS ENOSPC + sentinel + missing-mount reject |
| S2 contract | Calculated ~37 GiB geometry; infrastructure shortfall → BLOCKED |
| S3 | Actual Docker mounts on contract FS (worker/delivery/executors) |
| S4 | Missing/wrong FS refused before storage-init/activation |
| S5 | Legacy root-backed volume → migration-required; sentinel preserved |
| S6 | All three sidecars under test slice; swap=0; negatives fail |
| S7 | Offline stream-copy/ffprobe + media-net local fixture (no external providers) |
| S8 | OFF / OFF→ON / injected ON failure restores prior authority; storage kept |
| S9 | Scoped cleanup of recorded identities; unmount failure → no recursive delete |

Overall PASS only when every required case is PASS (no NOT_RUN/BLOCKED/FAIL).

## Dispatch

```bash
gh workflow run sec08-09-media-storage-resource-acceptance.yml \
  --ref codex/security-sec-09-network-isolation \
  -f expected_sha=<exact-commit-sha>
```

Exact `expected_sha` must equal the checked-out HEAD. No push trigger. No second remote run after the result of one authorized dispatch.

## Local harness correction after run 39841546437

Run 39841546437 remains FAIL (`KeyError: 'id'` after prepare); S4–S8
remain NOT_RUN for that historical artifact. No new native run is implied.

- Prepared image identities now come from the canonical `load_manifest` and
  typed `ImageRecord.image_id`. The complete media manifest, revision and
  linux/amd64 platform are validated before any identities are recorded.
- S5 has a separate disposable OFF project, database, env and deployment root.
  Its legacy volume and sentinel are retained until scoped cleanup stops its
  containers. The positive fixture starts with new contract-backed bind
  volumes; this is test setup, NOT a production data migration mechanism.
- S4/S5 require the actual expected domain reason. An arbitrary exception,
  unrelated refusal, missing JSON field or unresolved journal is not PASS.
  The S4 evidence field no longer collides with `record(..., detail=...)`.
- S7 runs fixture generation as worker 10001, then MP4 **and** WebM mux/probe
  through the production UDS executor. There is no optional WebM success.
  The network image has no ffprobe: its separate check is explicitly a local
  tool-uid write/read on the contract filesystem, **not** yt-dlp/egress or
  Landlock acceptance. Network policy retains its separate acceptance gates.
- S8 prepares a distinct revision B and uses the existing one-shot gateway
  injection. PASS requires a reached injection, delegated real rollback and
  `rolled_back`; idempotency or an unrelated failed rollout cannot pass.
  All previous operational media-authority fields must be restored; its
  publication timestamp may change.
- Cleanup validates and handles both recorded disposable projects, stopping
  their containers before deleting their volumes. Failed inventory/cleanup
  cannot be recorded as successful S9.

New offline regressions exercise the real manifest serializer/parser and
orchestration with Docker/release transports stubbed. These tests do not
prove native mounts, UDS peer credentials, Landlock, cgroups or rollback on
live Docker. No commit/push/dispatch/SSH/production installation is authorized
by this local corrective pass. Production numeric approval and effective
residual activation are unchanged; READY FOR ROLLOUT remains NO.

Local verification for this correction: `tests/release` + `tests/pg_backup`
**767 passed** (fresh); Ruff on the five touched Python files and mypy on
the three harness/driver modules passed; `git diff --check` passed.
Backend broad/native/Trivy were not rerun; there are no new native image IDs.

## Local revision/tag correction after run 39846098215

Run 39846098215 at `ed4ddd81027726b2d904f1514c7f646980084e22`
remains FAIL: the positive prepare tried to rebuild revision tags still used
by the legacy project's running containers. Image identities were recorded;
S4–S8 were NOT_RUN and S9 passed. This was a fixture lifecycle defect, not
permission to bypass the image builder's occupied-image guard.

The fixture now creates separate synthetic legacy L and positive A revisions
inside its disposable clone. Their Git diff must contain exactly the
documentation marker, with L an ancestor of A. Each project uses its own
matching revision in env, prepare, rollout and release manifest. S8 still
creates a third target B distinct from active A. The legacy/positive
revision identities are recorded as tracked evidence and in S5 evidence.

The release image builder and overwrite protection are unchanged. Preparing
both roots under one revision before starting containers was deliberately
not used: a second build could retarget shared tags and stale the first
manifest even without running containers.

The orchestration regression now invokes the actual
`image_build.assert_tag_collision_safe` including image-ID/ancestor lookup;
only Docker metadata transport and the build/rollout execution are stubbed.
It verifies the old same-revision rebuild remains refused while legacy is
running, positive A proceeds, both env/revision bindings agree, and both
manifests still match their respective unmodified tags. A separate real
temporary-Git test verifies the marker-only delta. Duplicate revisions,
non-ancestor revisions, an empty delta or runtime-file changes fail closed.

These are local regressions, not a new native container/image proof. No
commit, push, CI dispatch, Trivy, SSH, host installation or production change
is part of this correction; a new native S1–S9 run needs separate authorization.
READY FOR ROLLOUT remains NO. Previous approvals/activation state are unchanged.

Fresh local verification for this tag correction: `tests/release` +
`tests/pg_backup` **772 passed**; Ruff on the two touched Python files, mypy
on the changed harness module, stdlib-only import (`python -S`) and
`git diff --check` passed. Native S1–S9 and image-security remain NOT_RUN
for this uncommitted correction; production installation is NOT_PERFORMED.

## Local env/evidence correction after run 39849296885

Run 39849296885 at `70eb1e343b3f564fa48187f3952fbb61e2acff77`
remains FAIL. Revision isolation crossed the previous occupied-image failure.
The new abort was the legacy ON attempt's empty `EGRESS_DENY_*`, before the
expected migration refusal. Its historical S3 FAIL attribution and S5 NOT_RUN
are not rewritten. Cleanup passed; no S5 migration, S7 or S8 proof was obtained.

- Both projects now use one `write_fixture_env` helper. It writes executor
  flags OFF and identical disposable deny coverage, then loads the actual env
  and calls the production `assert_deny_coverage`. The same required targets
  are passed to each ON rollout; no egress guard or production default changes.
- Offline orchestration regression calls that production coverage validator
  before ON. Intentionally empty legacy deny settings must NOT count as a
  successful legacy migration refusal. Missing/empty/incomplete subnet/host
  coverage is tested for both fixture roles.
- The release path reports the current S3–S8 case to the orchestrator before
  its work starts. Failures are attached to that case and to
  `primary_failure.json.case`; unreached cases stay NOT_RUN. Existing explicit
  failure evidence is kept, and an exception cannot hide behind an earlier PASS.
  Because execution order differs from the required-case list, aggregate FAIL
  now dominates BLOCKED/NOT_RUN regardless of position. It never becomes a
  NOT_RUN exit merely because an earlier-listed case was not completed.
- Every tracked prepared image now carries service, full Image ID, reference,
  platform and OCI/source revision from the validated canonical manifest.
  These bindings remain in both partial and final evidence for all prepared
  revisions; they are not inferred from the host architecture or a tag string.

Tests exercise the real orchestrator's error/summary/export path for each
release case, with native operations stubbed, and serialize two revisions'
image identities through partial/final evidence. These are offline proofs,
not native Docker/mount/cgroup/UDS acceptance. No commit/push/dispatch, Trivy,
SSH, host provisioning, production changes or approval changes are included.
READY FOR ROLLOUT remains NO.

Local verification of the env/evidence correction: targeted harness tests
**72 passed**; full `tests/release` + `tests/pg_backup` **792 passed** (fresh).
Ruff on the four touched Python files, mypy on both harness modules,
stdlib-only imports (`python -S`) and `git diff --check` passed. No native,
Trivy, broad backend or production checks were executed for this correction.
