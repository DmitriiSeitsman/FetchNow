# SEC-08/09 — remediated source: audit-only successor

**AUDIT CONTRACT ONLY. NOT image-security PASS. NOT READY FOR ROLLOUT.**

## Identities

| Role | Value |
|---|---|
| Product source (`PRODUCT_SOURCE_SHA`) | `3792c377ecde1218ad9da9f3d5b91d620ed63b46` |
| Product source parent | `5dfd0524758c7ae55e04cc7045f709c6cb22c66f` |
| Product source tree | `e9dbbc4588c2e968fd32e0201dac99b075924134` |
| Audit harness | the single audit-only commit on top of the product source |
| Previous final audit | run `40031834156`, source `9934ebe`, harness `5dfd052` |

The product commit contains exactly seven paths: `backend/uv.lock`,
`deploy/nginx/Dockerfile`, `web/Dockerfile`, the candidate-7 smoke helper, its
two unit test modules and `sec-08-09-final-candidate-remediation-20261024.md`.

The audit-only commit contains exactly:

- `.github/workflows/sec08-09-final-candidate-image-audit.yml`
- `docs/operations/sec-08-09-remediated-source-image-audit.md`
- `scripts/image_audit_final_candidate.py`
- `tests/image_audit/test_image_audit_final_candidate_unit.py`

No runtime, Dockerfile, lock, Compose, release tooling, smoke helper or
caller-attribution implementation is changed by the audit commit.

## Authorization

Push to `codex/security-sec-09-network-isolation`, run attempt 1 only. The
guard refuses unless all hold:

1. `HEAD == GITHUB_SHA` and HEAD has exactly one parent, the product source.
2. The product source has exactly one parent `5dfd052` and tree `e9dbbc45`.
3. `git diff 5dfd052 PRODUCT_SOURCE_SHA` is exactly the seven product paths.
4. `git diff PRODUCT_SOURCE_SHA HEAD` is exactly the four audit paths.

There is no commit-message match, no "latest HEAD" rule and no fallback to
`9934ebe` or the previous audit chain. Both checkouts use full history and
`persist-credentials: false`. Product source and audit harness are recorded
separately in `authorize.json`, `source-proof.json` and the summary.

## Clean source proof

The build context is `git archive PRODUCT_SOURCE_SHA` only. The proof requires:

- the extracted file set and every blob id equal `git ls-tree -r` of the source
  (no missing, extra or modified files);
- runtime Dockerfiles, locks, `pyproject.toml` and the pytest export equal the
  SHA-256 values pinned in `PRODUCT_RUNTIME_SHA256` (CI does not read `/tmp`);
- runtime inputs equal those of the audit checkout;
- the source identity is the pinned product commit and tree.

## Native run

Native Linux AMD64 only. Six FetchNow images are built from the clean source;
the candidate PostgreSQL is pulled by the Compose reference. PostgreSQL index,
linux/amd64 platform-manifest and config identities are recorded in
`postgres-identity.json` and must match the pulled image. The extracted gosu
SHA-256 is in the smoke report. Live retained PostgreSQL: not verified here.

Compatibility before scanning: product candidate-7 smoke (package inventory
with all five Alpine pins, API migrate/live/ready, worker start, MP4/WebM
stream-copy and ffprobe as the production UID, nginx config/HTTP, gosu
checksum) and isolated smoke (API `certifi==2026.10.19` from `/opt/venv`,
executor import/tools/fail-closed start, egress proxy start/listen/connect).
This is not the storage/resource/native isolation acceptance gate.

Trivy 0.74.0 (checksum-pinned, invoked by absolute path), one DB download,
seven offline scans on one snapshot, recorded scan exit codes, stable pre/post
identities. Findings keep the gate non-zero. No VEX, ignore-unfixed, threshold
change, approval transfer or automatic retry.

## Evidence and cleanup

Raw scanner reports are uploaded only as a CI artifact after a privacy check.
Cleanup removes only the run's own images and directories; smoke helpers remove
their own containers with `docker rm -f -v` and named volumes only by tracked
name. A failed inventory never counts as absence; new volumes or networks
against the baseline fail the job. A final manifest hashes the audit evidence
and the cleanup report (it does not hash itself).

## Results

Recorded after the single run. Historical runs `40025850562` (technical FAIL),
`40031834156` (security FAIL, cleanup FAIL) and `40004249842` (storage/resource
PASS on an earlier source) are unchanged and transfer nothing to this audit.
