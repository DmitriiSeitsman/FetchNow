# 28. Dual application and database release state (PRD1C3B2A)

## Why application and database state are separate

After an online-expand database migration, PostgreSQL may already be on
schema tip **B** while the running application remains on release **A**
(and may roll back to **A** if application activation of **B** fails).

`state/current.json` therefore records two identities:

- **application** — active immutable image set and release revision
- **database** — committed Alembic heads, the schema-defining release,
  and an explicit **compatibility envelope** of application revisions
  proven safe against that schema

Automatic database downgrade, restore, or `alembic stamp` remain forbidden.

## Schema versions

| Version | Shape | Writers |
| --- | --- | --- |
| v1 | Flat application-only fields (`revision`, `image_ids`, …) | Historical only |
| v2 | Nested `application` + `database` | All successful new writes |

Legacy v1 files are **resolved in memory** against the verified prepared
release (derived heads + singleton envelope). Resolution never rewrites
the on-disk v1 file.

## Authority

1. **Live PostgreSQL heads** — runtime schema tip (`alembic_version`).
2. **`current.json.database.heads`** — last committed claim of the release system.
3. **Prepared release source** — expected heads for a specific revision.

Steady-state gates require `live == saved database.heads` before any
application mutation or already-active verification. Application release
source is **not** DB authority when it differs from
`database.schema_release_revision`.

### Migrations graph authority (schema v2)

`current.json` schema v2 does **not** store `migrations_graph_sha256`.
The sole persisted migrations-graph authority is
`database.schema_release_revision`: the immutable prepared release whose
`backend/migrations/versions` directory defines the Alembic graph.

Migration journals, backup v2 attestations, and retention holds bind a
graph SHA-256 for B2B1/B2B2 cross-checks. At every commit/recovery gate
the release tooling recomputes that SHA from the verified prepared release
at `schema_release_revision` and requires equality with the journaled
binding. There is no separate graph field in `current.json`.

## Compatibility envelope

An application target may run against the current database when **either**:

1. target release source heads equal `current.database.heads`, or
2. target revision is listed in `compatible_application_revisions`.

There is **no transitive inference**. The envelope is an explicit finite set.

## Application rollback with DB-ahead state

Application-only rollback restores previous images and may rewrite the
application section of `current.json`. Database heads, schema release,
migration linkage, and the envelope are preserved. The previous application
must pass the compatibility gate before container mutation.

## Deploy-plan

`deploy-plan` plans transitions from **saved database heads**, not from
application source heads. It emits:

```text
application_rollout_required = (target revision != current application revision)
```

Example: `application=A`, `database=B`, `target=B` →
`migration_required=false`, `application_rollout_required=true`.

Downgrade/divergent target DB heads still fail closed even if the target
revision is in the envelope. Explicit application rollback remains a
C3A recover/rollout concern, not forward deploy planning.

## What this PR does **not** do

- No Alembic upgrade/downgrade/stamp
- No backup create/verify
- No migration journal or `release-migrate`
- No unified `release-deploy`
- No staging database migration or deployment

## Follow-on work

| PR | Scope |
| --- | --- |
| PRD1C3B2B1 | Backup verification foundation: one-lock session, exact Alembic head proof, attestation v2 (implemented; no migration execution) |
| PRD1C3B2B2 | Verified backup + migration execution transaction (consumes typed verify evidence) |
| PRD1C3B2C | Unified deploy orchestration (migrate → application rollout) |

PRD1C3B2B1 does **not** change `state/current.json`, rollout/recover behavior, retention holds, or staging deployment. Future migration callers must use the strict session API with explicit expected heads and explicit schema-release migrations directory — not silent worktree inference.

## Operator notes

Existing commands (`deploy-plan`, `rollout`, `recover`, `prepare`,
`verify-release`, `health`, `preflight`) understand dual state. Do not
document non-existent migrate/deploy commands.
