# 26. Deployment transaction & rollback (PRD1C3A)

**INFO:** This chapter describes repository tooling for an application-only rollout transaction. It does **not** claim that staging is deployed.

## Scope

PRD1C3A activates only `api`, `worker`, `web`, and then `gateway`, using immutable image IDs recorded by PRD1C2. PostgreSQL is neither recreated nor migrated by `rollout`. The read-only database gate requires live Alembic heads to match saved `current.database.heads`, then allows the target application only when its source heads equal those DB heads **or** its revision is listed in the explicit compatibility envelope (see [chapter 28](28-dual-application-database-state.md)). Bootstrap still requires live heads to equal the target release source heads.

`current.json` is written atomically only after the target passes stabilization.
As of PRD1C3B2A it is schema v2: nested application identity plus database
heads/compatibility envelope (see [chapter 28](28-dual-application-database-state.md)).
Application-only rollback never rolls back database state. Each attempt has a
deployment journal with a plan, events, image override, and terminal result.
Journals redact password and `DATABASE_URL` values.

## Normal activation

First prepare the exact trusted revision, then roll it out:

```bash
make release-prepare \
  EXPECTED_REVISION=<40-char-sha> \
  ENV_FILE=.env.staging \
  DEPLOY_ROOT=/srv/fetchnow-staging

make release-rollout \
  EXPECTED_REVISION=<40-char-sha> \
  ENV_FILE=.env.staging \
  DEPLOY_ROOT=/srv/fetchnow-staging
```

The Make target fixes `--project-name fetchnow-staging`; it cannot be redirected to another project. Image tags are not the activation authority: the generated Compose override pins the inspected immutable image IDs from `release.json`.

After a committed rollout, standalone staging health uses the same identity model:
`make release-health ENV_FILE=/srv/fetchnow-staging/env/.env.staging
DEPLOY_ROOT=/srv/fetchnow-staging EXPECTED_REVISION=<sha>` reads schema-v2
`state/current.json`, verifies its prepared manifest binding, and checks running
containers by exact image ID. The external env file is consumed in place; do not
copy or symlink it into the repository.

## Bootstrap

The first application rollout requires an explicit acknowledgement:

```bash
make release-rollout \
  EXPECTED_REVISION=<40-char-sha> \
  ENV_FILE=.env.staging \
  DEPLOY_ROOT=/srv/fetchnow-staging \
  BOOTSTRAP=1
```

Bootstrap requires a healthy existing PostgreSQL container and refuses to run when application containers or `current.json` already exist. Database initialization/migrations remain a separately approved operation; this command does not perform them.

## Recovery history

When automatic rollback itself fails, the original deployment `result.json` (including `rollback_failed`) remains immutable forever. Operators run explicit recovery; each attempt creates a separate directory under `recoveries/<recovery-id>/` with its own plan, events, and terminal result. A successful recovery atomically publishes `resolution.json` referencing the SHA-256 hashes of the original deployment result and the successful recovery result. Failed recovery attempts also remain immutable; a later attempt uses a new recovery ID. Rollout remains blocked until `rollback_failed` is paired with a validated `resolution.json`.

Recovery never overwrites deployment `result.json`. Bootstrap activation labels every recreated application container with `com.fetchnow.deployment-id` and `com.fetchnow.release-revision`; bootstrap failure cleanup removes only containers bearing the exact labels for that transaction.

## Alembic head discovery

Target Alembic heads are discovered via static AST parsing of migration files (no import/execution). Heads are revisions never referenced as a `down_revision` parent; `depends_on` is validated when present but does not change head calculation.

## Automatic rollback and recovery

After application activation begins, an unhealthy target causes an attempt to
restore the previous application's immutable image IDs. Before that restore
mutation, live database heads must still match saved `current.database.heads`
and the previous application must pass the compatibility envelope gate.
`current.json` remains on the previous release unless the new target stabilizes
successfully (application section is not rewritten on automatic rollback).
A successful repeat of the current revision is idempotent: it verifies health
without recreating services.

If automatic rollback itself cannot complete, inspect the deployment journal and choose an explicit action:

```bash
make release-recover \
  DEPLOYMENT_ID=<journal-uuid> \
  ACTION=rollback \
  ENV_FILE=.env.staging \
  DEPLOY_ROOT=/srv/fetchnow-staging
```

`ACTION=accept-target` is only for an operator who has verified that the target is healthy and should become current. Recovery does not introduce a database migration path.

## Boundaries

| Milestone | Responsibility |
|---|---|
| PRD1C3A (this chapter) | application image-ID activation, health stabilization, journaled rollback/recover; no DB mutation |
| PRD1C3B2A | Dual application/database `current.json` + compatibility envelope (no DB mutation) |
| PRD1C3B2B2 | verified forward migration transaction (database state only; no application activation) — см. [главу 29](29-verified-migration-transaction.md) |
| PRD1C3B2C | unified migrate→rollout orchestration |
| PRD1D | host Nginx/TLS/public publish and production operationalization |

Isolated CI integration uses only unique `fetchnow-rollout-test-*` projects and removes that exact project with `docker compose down -v`. It never targets `fetchnow`, `fetchnow-staging`, or `fetchnow-prod`.
