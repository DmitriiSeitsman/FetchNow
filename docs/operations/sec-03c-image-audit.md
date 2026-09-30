# SEC-03C — Minimal production-image vulnerability audit

Status: implemented in worktree (not committed). Production unchanged.
Scanner: **Trivy 0.74.0** (official GitHub release; SHA-256 pinned).

This gate scans the **four unique final runtime images** for OS and language-package
vulnerabilities. It does **not** replace SEC-03A/B2 lockfile audits, and it does
not cover secrets, misconfiguration, license, egress, host Nginx, or load.

## Scope

| Path | Role |
| --- | --- |
| `scripts/image_audit.py` | Bounded Trivy wrapper, completeness, policy |
| `tests/image_audit/` | Offline synthetic unit tests |
| `.github/workflows/ci.yml` job `image-audit` | linux/amd64 CI gate |
| this document | contract + rollout linkage |

Dockerfiles, Compose, locks, release transaction code, and integration fixtures
are **out of scope** for this stage.

## Targets (unique final images)

| Target | Image | Shared by |
| --- | --- | --- |
| `api` | `fetchnow-api` | api, worker, delivery, storage-init |
| `web` | `fetchnow-web` | web |
| `gateway` | `fetchnow-gateway` | gateway |
| `postgres` | `postgres:16.15-alpine3.24` | postgres |

Builder-only surfaces (backend `uv`/build-essential, web Node/Astro) are **not**
scanned as runtime.

## Scanner pin and provenance

| Item | Value |
| --- | --- |
| Version | `0.74.0` |
| Release | https://github.com/aquasecurity/trivy/releases/tag/v0.74.0 |
| Linux amd64 archive | `trivy_0.74.0_Linux-64bit.tar.gz` |
| Linux amd64 SHA-256 | `2ae6fe3ee734b7fdf11335663e18c75ea12dccc76062f09f164a3b0f8be4371a` |
| Linux arm64 SHA-256 | `b94ce1976bbf3c15b514b605ee88be7c6d94a29be2302847ff01cb794d47aad5` |
| macOS arm64 SHA-256 | `1caada5e0e2091909357c7525d3aa76f4b660b13821bc143b190c7483e31cc11` |

Checksums come from the release file `trivy_0.74.0_checksums.txt`. The release
also publishes Sigstore bundles (`*.sigstore.json`) for the archives and the
checksums file; CI verifies the archive SHA-256 before extract. Do **not** use
`latest`, `curl \| sh`, or an unpinned GitHub Action to install Trivy.

Confirm flags against `trivy image --help` for this version. The gate requires:

- `--scanners vuln` only (no secret / misconfig / license)
- `--disable-telemetry`
- task-local `--cache-dir`
- `--download-db-only` once, then scans with
  `--skip-db-update --skip-java-db-update --skip-check-update --offline-scan`
  and `--image-src docker` (no remote registry fallback when resolving the target)
- `--exit-code 0` so Trivy findings do not collide with technical failures;
  policy is applied by `scripts/image_audit.py`
- scan argument is the local Docker **Image ID** (`sha256:…`)

`--offline-scan` alone is **not** treated as a complete network ban; the
combination above is the documented offline scan contract for this gate.

## Identity vs digest

For each target the gate records **separate** fields:

| Field | Meaning | May be used as Image ID? |
| --- | --- | --- |
| `image_id` | Docker `inspect .Id` (config digest) | **Yes** — local scan authority |
| `config_digest` | Same as `image_id` for Docker | Yes (alias of Id) |
| `repo_digests` | Raw `RepoDigests` (`name@sha256:…`) | **No** |
| `repo_manifest_digests` | Digests extracted from `RepoDigests` | **No** |
| Registry index digest | Multi-arch index from the registry | **No** — record only from registry evidence |
| Platform manifest digest | linux/amd64 manifest from the registry | **No** |

A rebuild of the same git SHA may produce a different Image ID. Image ID is
**never** a registry index or platform manifest digest.

If `inspect .Id` equals a local `RepoDigest` digest, that can be normal Docker
behavior for locally tagged images (`name@<Id>`). A **different** registry
manifest digest must never be accepted as the Image ID when matching scanner
`Metadata.ImageID` (see completeness below).

### Prior identity recording error (do not rewrite old reports)

The AMD64 evidence under `/tmp/fetchnow-sec03c-amd64-evidence/` (2026-09-30)
incorrectly set PostgreSQL `image_id` equal to the **platform manifest digest**
`sha256:1a66d744…`. The registry config digest for that manifest is
`sha256:81bd698b…`. That collapse is documented here; the old report files are
left unchanged as provenance.

## Completeness

Empty `Vulnerabilities: []` is allowed. Missing proof of analysis is not:

- JSON `SchemaVersion` ≥ 2
- report `Metadata.ImageID` matches inspected **Image ID** (`inspect .Id`) after
  digest normalization — matching a RepoDigest/manifest alone is
  `image_mismatch` (`manifest_digest_not_image_id`), never a guessed PASS
- platform matches the declared expected platform when present in the report
- expected OS family (`debian` for api; `alpine` for web/gateway/postgres)
- at least one OS package Result
- api also requires a Python language Result (`python-pkg` / equivalent)

Current API runtime base (SEC-03C Candidate A): official
`python:3.14.7-slim-trixie` with Debian trixie `ffmpeg` and a pinned
trixie-security OpenSSL `3.5.7-1~deb13u3` set. The completeness check still
keys on OS **family** `debian`, not a specific Debian release codename.

Native Candidate A backend acceptance (CI job `backend-candidate-a`) builds the
canonical `backend/Dockerfile` default **runtime** image first, then a separate
test-only image from `.github/docker/backend-test.Dockerfile` that uses that
runtime Image ID as `FROM` and layers frozen `--extra dev` plus targeted
`backend/tests` copies. Production/`image-audit` builds keep using
`backend/Dockerfile` alone (no test stage; `backend/.dockerignore` still
excludes `tests` from the production context).

## Policy

| Condition | Verdict | Process exit |
| --- | --- | --- |
| All targets complete; no HIGH/CRITICAL gate hits | `pass` | `0` |
| HIGH/CRITICAL with non-empty `FixedVersion` | `fail` | `1` |
| HIGH/CRITICAL without fix (after excluding `not_affected`) | `needs_owner_decision` | `2` |
| Scanner/DB/timeout/malformed/incomplete/platform/image mismatch | `technical_failure` | `1` |

All severities remain in the sanitized summary. No `--ignore-unfixed`, no
blanket ignores, no exception file. Vendor `Status` / backports are considered
via Trivy fields (`Status`, `FixedVersion`); an old upstream version alone is
not treated as proof of an unfixed issue when the vendor marks `not_affected`.

Do not weaken the gate to make CI green. Remediate with a **separate** minimal
proposal (base/package bumps) — not inside this audit wrapper.

## Evidence (CI)

Publish only the sanitized summary JSON:

- image identity / platform / revision label / RepoDigests
- scanner version + offline flags
- DB metadata (`Version`, `UpdatedAt`, `DownloadedAt`, `NextUpdate`)
- finding rows: package, versions, fixed version, severity, status, advisory id
- counts and overall status

Do **not** publish layer contents, image history/env, or raw Trivy logs.

Task-local cache and disposable tags are removed in the job cleanup step only
(`trivy clean` against that cache dir; `docker image rm` of this job’s tags).
No global Docker prune and no deletion of the user/global Trivy cache.

## CI vs production pre-activation

### A. CI acceptance

The `image-audit` job builds disposable `fetchnow-{api,web,gateway}:sec03c-ci`
images on **linux/amd64**, pulls `postgres:16.15-alpine3.24` for that platform, and
scans those Image IDs. Passing CI proves those CI-built images only.

### B. Production pre-activation acceptance

After canonical release **prepare**, compare release-candidate Image IDs to the
IDs scanned in CI. If they differ, **re-scan the prepare candidates** before
activation. Do not assume the same git SHA rebuilds to the same digest.

For PostgreSQL, if the running container is retained across rollout: inspect and
scan the **actual running container’s image** in a separately approved window.
A fresh `docker pull` of the same tag does **not** prove the live Postgres image.
See `docs/operations/sec-03c-postgres-pin-rollout.md` — Compose pin updates do
not recreate a running postgres during ordinary app rollout.

This stage does **not** SSH, inspect production, or change rollout tooling.

## Local run (optional)

On a non-amd64 host, set `--expected-platform` to the inspected platform and
pass `--host-platform-note` stating that the run is **not** linux/amd64
evidence. CI remains the authoritative amd64 gate.

```bash
# After installing Trivy 0.74.0 with the pinned checksum:
PYTHONPATH=scripts python3 scripts/image_audit.py \
  --trivy-bin /path/to/trivy \
  --cache-dir /tmp/fetchnow-sec03c-trivy-cache \
  --targets-file /tmp/targets.json \
  --expected-platform linux/amd64 \
  --work-dir /tmp/fetchnow-sec03c-work \
  --summary-out /tmp/fetchnow-sec03c-summary.json
```

## Boundaries

| Surface | Gate |
| --- | --- |
| npm / Python lock metadata | SEC-03A / SEC-03B2 |
| Final image OS + runtime packages | **SEC-03C** |
| Secrets / config / egress / isolation | not this scan |
