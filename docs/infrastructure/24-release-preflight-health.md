# 24. Release identity, preflight & health gate (PRD1C1)

**INFO:** This chapter documents repository tooling for immutable release identity, read-only staging preflight, and a Compose+HTTP health gate. It does **not** claim staging has been deployed. Exact-revision materialization and image build are in [глава 25](25-release-materialization-build.md) (PRD1C2). Container deploy/rollback (PRD1C3) remain out of scope.

## Scope (PRD1C1)

**In scope**

- Canonical `FETCHNOW_RELEASE_REVISION` (full lowercase 40-char Git SHA for staging; `local` for development)
- Revision-tagged application images (`fetchnow-api` / `fetchnow-web` / `fetchnow-gateway`)
- OCI label `org.opencontainers.image.revision` on final image stages
- Read-only staging preflight CLI
- Read-only Compose JSON + HTTP live/ready health CLI
- Unit tests, isolated health integration, Make/CI wiring, handbook updates

**Out of scope**

- `git fetch` / checkout / worktree release creation
- Image build orchestration or cleanup as an operator deploy step
- `docker compose up/down/restart` in the operator CLI
- Backup create during deploy, Alembic orchestration
- Deployment-state writes, automatic rollback, live DB restore
- SSH/server mutations, host Nginx/TLS, systemd/cron, S3/PITR

## Release identity

One variable: `FETCHNOW_RELEASE_REVISION`.

| Context | Value |
|---|---|
| Development | defaults to `local` (`${FETCHNOW_RELEASE_REVISION:-local}`) |
| Staging | required full SHA via `${FETCHNOW_RELEASE_REVISION:?…}` — fails closed if absent |

Rules:

1. Staging: exactly `[0-9a-f]{40}` — no abbreviated SHA, branch names, or `latest`.
2. API and worker share `fetchnow-api:<revision>` (same image identity).
3. Web/gateway use the same revision value on their images.
4. PostgreSQL stays `postgres:16.9-alpine` (unchanged).
5. No separate tag vs source-revision variables.

Build arg `FETCHNOW_RELEASE_REVISION` sets the OCI label on the **final** Dockerfile stage (declared late, after COPY/USER, so a revision change cannot reuse a stale LABEL from an earlier cached layer). Development builds may use `local`. Never pass secrets as build args.

## Password policy (closes DATABASE_URL caveat)

Staging `POSTGRES_PASSWORD` is interpolated raw into `DATABASE_URL`. Preflight requires:

- length ≥ 32
- charset only `A-Z a-z 0-9 . _ ~ -`
- reject `@ / : %` whitespace, quotes, shell metacharacters, empty/default/example placeholders

Never print the password. Application DB config is unchanged in this PR.

## Preflight (read-only)

```bash
python3.12 scripts/fetchnow_release_cli.py preflight \
  --project-name fetchnow-staging \
  --env-file .env.staging \
  --compose-file compose.yaml \
  --compose-file compose.staging.yaml \
  --expected-revision <40-char-sha>

# or:
make release-preflight EXPECTED_REVISION=<40-char-sha>
```

Validates (non-exhaustive): project name, env file existence/regular file/not symlink/mode ≤0600, required keys once, password policy, full SHA equals env, **revision already contained in local `refs/remotes/origin/main`** (`git merge-base --is-ancestor "$EXPECTED" refs/remotes/origin/main` — feature-branch descendants are **rejected**), clean worktree, Docker CLI + Compose v2 + daemon, rendered Compose staging contract (loopback gateway, no leaked ports, revision tags, shared API/worker image, pinned Postgres, no binds/reload), deploy/backup root safety (reuses PRD1B path policy), free-space margin ≥ 1 GiB.

**Trusted main:** preflight does **not** fetch. Operators must `git fetch` / synchronize `origin/main` separately before preflight. Missing or stale `refs/remotes/origin/main` fails closed. Staging Make target does not accept alternate trusted refs.

**Does not:** `compose build|pull|up|down|run|exec`, Alembic, backup create/verify, git checkout/reset/clean/fetch.

## CI note (PR commits)

A pull-request HEAD is intentionally **not** contained in `origin/main`, so operator preflight against PR HEAD must fail before merge. CI does **not** weaken that policy. Positive preflight coverage uses an isolated temporary Git repository whose `refs/remotes/origin/main` points at the tested commit (`make release-ancestry-integration`). Compose health integration stays separate and does not claim a skipped preflight as success.

## Health gate (read-only)

```bash
python3.12 scripts/fetchnow_release_cli.py health \
  --project-name fetchnow-staging \
  --env-file .env.staging \
  --compose-file compose.yaml \
  --compose-file compose.staging.yaml \
  --expected-revision <40-char-sha> \
  --gateway-base-url http://127.0.0.1:8091

# or:
make release-health EXPECTED_REVISION=<40-char-sha>
```

Uses `docker compose ps --format json` (array or NDJSON) plus `docker inspect`. Expected services: gateway, api, worker, postgres, web.

**Worker exception (temporary):** Compose disables the worker healthcheck today (`healthcheck.disable: true`) because the worker is still an idle heartbeat without a queue. The health gate therefore requires worker `running` only. This is temporary until queue health exists; do not treat it as a permanent policy.

**RestartCount** is logged for diagnostics only — a non-zero count does not by itself fail the gate (exited/dead/restarting states still fail).

HTTP (loopback staging gateway only; public HTTPS health is deferred to PRD1D):

- `GET /api/v1/health/live` and `/ready`
- HTTP 200, JSON object, `status` exactly `ok`
- per-request timeout, bounded retry/backoff, overall deadline
- no arbitrary redirects; no operator-provided public host for staging health

Safe diagnostics may include service name, state/health, image reference, retry count, HTTP status, error class. Never dump container env, `.env.staging`, `DATABASE_URL`, or unbounded logs.

Operator CLI prints `FAIL:` only for a final failed gate. Integration wait loops use `WAIT:` / `RETRY:` for expected transient unreadiness; deliberate negative checks may print `EXPECTED:`.

## Isolated integration

```bash
make release-health-integration
make release-ancestry-integration
```

Health integration uses unique `fetchnow-health-test-<suffix>` only. Ancestry integration uses a disposable temporary Git repository (never mutates real `origin/main`). Never touches `fetchnow` / `fetchnow-staging` / `fetchnow-prod`. CI cleanup uses `if: always()`.

## Boundaries

| Milestone | Status |
|---|---|
| PRD1A Compose isolation | done |
| PRD1B PostgreSQL backup tooling | done |
| **PRD1C1 release identity / preflight / health** | this chapter |
| PRD1C2 materialize + image build | [глава 25](25-release-materialization-build.md) |
| PRD1C3A application activation / rollback | [глава 26](26-deployment-transaction-rollback.md) |
| PRD1C3B migration transaction | not implemented |
| PRD1D public publishing | not implemented |
