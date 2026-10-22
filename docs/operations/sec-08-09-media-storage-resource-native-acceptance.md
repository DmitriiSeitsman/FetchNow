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
