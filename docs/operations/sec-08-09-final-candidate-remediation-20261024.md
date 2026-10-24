# SEC-08/09 — local remediation after audit 40031834156

**LOCAL REGRESSION PASS / IMAGE SECURITY NOT RE-SCANNED / NOT READY FOR ROLLOUT.**

Baseline: `5dfd0524758c7ae55e04cc7045f709c6cb22c66f`, branch
`codex/security-sec-09-network-isolation`. The pre-existing 21 dirty/new files
are preserved; staging remains empty. No commit/push/CI/SSH/deploy or owner
approval/VEX change is included in this pass.

## Exact scope (seven files)

- `backend/uv.lock`
- `web/Dockerfile`
- `deploy/nginx/Dockerfile`
- `scripts/image_audit_candidate7_smoke.py`
- `tests/image_audit/test_image_audit_candidate7_smoke_unit.py`
- `tests/image_audit/test_final_candidate_remediation_unit.py`
- `docs/operations/sec-08-09-final-candidate-remediation-20261024.md`

## Dependency correction

`uv 0.12.19 lock --upgrade-package certifi==2026.10.19` ran in an isolated
temporary project. Its generated lock is byte-identical to the repository
result. Of 42 lock records, only certifi changed: `2026.7.22` -> `2026.10.19`,
including its sdist/wheel URLs, hashes and upload metadata. The other 41
records, lock-level metadata, Python range and platform markers are unchanged.
No direct dependency, `pyproject.toml`, Python/base version or dev pin changed.

The [certifi advisory](https://github.com/advisories/GHSA-6q48-6c7w-9gw2)
and [PyPI release metadata](https://pypi.org/pypi/certifi/2026.10.19/json)
confirm the selected fixed version. Artifact SHA-256:

- wheel: `42d35340cc5b9ba78e818ddbff0c5b1c2b779de448a340fd1b989f5fa8670e1b`
- sdist: `035c1e85572b141bf1c3a3eff0e33a8eb74917522f2559075ff8d2fea780a1ae`

New `backend/uv.lock` SHA-256:
`be412a0b63f41f3fea408b6aa1a199834780e15e4888f634ff9eb6f43bda08a4`.

The pytest-only closure was freshly regenerated in memory using the documented
frozen export algorithm and compared byte-for-byte: unchanged, six packages.
`backend/ci/pytest-requirements.txt` remains
`ef963b8d360983d177cdd0e61655400f52edb951738c18450b3d5607564f8391`.
`web/package-lock.json` remains
`e62923098830723100a168e46bd8e83e0265dce38b92603f25504fb4c7fc7f6c`.

## Alpine correction

Both nginx Dockerfiles add only `brotli-libs=1.2.1-r0` and
`libx11=1.8.14-r0`. Base `nginx:1.31.6-alpine3.24` and existing
libexpat/pcre2/tiff pins, users and runtime layout are unchanged. No edge
repository or blanket upgrade is used. Smoke inventory now requests and
checks all five pinned packages; old brotli/libx11 versions are rejected.

Official v3.24/main APKINDEX was downloaded for x86_64 and aarch64; both
contain the selected versions, plus the three retained pins. Archives:

- x86_64: `a7f0c0024c4e2e810192016a1e9278d0c5ff6792390c87b13984af1872611780`
- aarch64: `2822e83c6dc363b1e0997a86a9c855484d2760e11d8c08da46d51e21f882472b`

Sources: `https://dl-cdn.alpinelinux.org/alpine/v3.24/main/<arch>/APKINDEX.tar.gz`;
[libx11 package](https://pkgs.alpinelinux.org/package/v3.24/main/x86_64/libx11),
[brotli-libs package](https://pkgs.alpinelinux.org/package/v3.24/main/aarch64/brotli-libs).
This verifies repository availability, not installation in new images.

## Scoped anonymous-volume cleanup

Smoke removes its tracked containers with `docker rm -f -v`: Docker deletes
their anonymous volumes, not named volumes. Named media volumes are removed
only through their explicit tracked identities. The gosu probe receives a
unique run-scoped name tracked before `docker create`, so a partial create
can also be cleaned. There is no prune or deletion of a baseline difference.

If removal fails, absence requires a successful exact resource inventory;
daemon errors do not count as absence. Cleanup is bounded by timeouts and
retains failures while continuing cleanup of other owned identities.

Fresh local Docker proof (Docker Desktop, **not native Linux AMD64**): two
disposable containers were created from a pre-existing Postgres image but
never started. Four owned anonymous volumes were removed, the named volume
survived container removal, and pre-existing volumes were preserved. The
owned named fixture volume was then removed separately; final volume
inventory equals baseline. No image was pulled/built/removed and no database
server was started. Evidence:
`/var/folders/bp/hgd0y79d5jj7yhk8pjr70ssw0000gn/T/fetchnow-rmv-proof-i_65ys95/result.json`.

## Fresh local verification

- `tests/image_audit`: **311 passed**, including lock/pin/cleanup regressions.
- `test_safe_http_client.py` + `test_safe_document_fetch.py`: **71 passed**
  using the isolated frozen app+dev environment with certifi `2026.10.19`.
- Isolated environment: **41 installed distributions** (40 external + app),
  CPython 3.12.13 on Darwin arm64; the worktree venv was not modified.
- Offline TLS smoke: HTTPX client creation, SSL `CERT_REQUIRED`, hostname
  verification enabled, 120 loaded CAs; compromised emSign ECC G3 absent.
  No network request or provider TLS compatibility claim is made.
- `uv lock --check --offline`: PASS; pytest export byte comparison: PASS.
- Ruff check/format, strict mypy on the smoke helper and diff-check: PASS.

Sandbox DNS restrictions initially blocked package metadata retrieval. The
subsequent authorized network access resolved the lock in `/tmp` and read
the official APKINDEX. No advisory scanner was run or retried.

## Identity, history and next boundary

Run `40031834156` remains completed scan evidence with security FAIL and
cleanup FAIL. The local correction does not rewrite it or claim findings
cleared by a fresh scan. Its 26 gosu HC advisories, applicability work and
506 unfixed instances are not accepted or modified here.

This changes runtime inputs (app lock and nginx packages). The existing audit
workflow is still intentionally frozen to product source `9934ebe`; it must
**not** be pushed/dispatched as validation of this local successor. After an
explicitly authorized runtime commit, freeze its new source SHA and prepare a
separate audit-only successor contract, fresh compatibility builds and scans.
Do not relax ancestry/manifests or silently reuse the old approved artifacts.

Broad backend/Postgres suites, new image builds, native cleanup, Trivy and
caller-attribution reruns: **NOT RUN**. Production, TEST/LIVE/SEO, gosu VEX,
residual approval, budget and residual activation: unchanged.
