# 27. Migration Compatibility and Deployment Planning (PRD1C3B1)

PRD1C3B1 adds a **read-only deployment planner** and a **strict migration
compatibility contract**. It does **not** create backups, run Alembic
upgrade/downgrade/stamp, mutate PostgreSQL, recreate containers, write
deployment journals, or deploy to staging.

PRD1C3B2A adds dual application/database current state and a compatibility
envelope so application rollback can remain honest when the database is ahead
(see [chapter 28](28-dual-application-database-state.md)). PRD1C3B2B will
repeat every check under the shared deployment lock and perform verified
backup + migration orchestration. PRD1C3B2C unifies migrate→rollout. PRD1D
remains the first real staging deployment.

## Compatibility contract

Every deployable release ships an exact-source policy file:

`deploy/migrations/compatibility.json`

Initial repository state (baseline only):

```json
{
  "schema_version": 1,
  "transitions": []
}
```

### Schema v1

| Field | Requirement |
|-------|-------------|
| `schema_version` | Must be `1` |
| `transitions` | JSON array (may be empty) |

Each transition object must contain **exactly** these fields:

| Field | Requirement |
|-------|-------------|
| `from_heads` | Non-empty, sorted, unique revision IDs |
| `to_heads` | Non-empty, sorted, unique revision IDs |
| `included_revisions` | Non-empty, sorted, unique revision IDs |
| `execution_mode` | Exactly `"online_expand"` |
| `online_with_previous_application` | Exactly `true` |
| `previous_application_rollback_compatible` | Exactly `true` |
| `data_loss` | Exactly `"none"` |
| `database_downgrade` | Exactly `"forbidden"` |
| `rationale` | Non-empty human-readable text (bounded; no placeholders) |

Unknown fields, missing fields, duplicate transitions, unsorted IDs, unsafe
booleans, non-`none` data loss, or any downgrade allowance are rejected.
**No environment variable or CLI flag overrides these values.**

### Online expand meaning

`online_expand` means the migration may be applied while the previous
application release remains running and compatible (for example adding a
nullable column or other backward-compatible expand). Human code review must
confirm each transition before it is merged; the contract is an executable
policy gate, not a substitute for review.

## Static Alembic graph

PRD1B static AST graph discovery (`fetchnow_pg_backup.alembic_graph`) is
extended for planning:

- heads, ancestors, forward/downgrade/divergent classification
- `included_revisions = ancestors(target_heads) − ancestors(current_heads) − current_heads`

`depends_on` is validated statically but does **not** change discovered heads;
Alembic applies dependency ordering at runtime separately from lineage parent
edges used for head discovery.

No migration Python is imported or executed.

## Read-only deploy planner

Production command:

```bash
fetchnow_release deploy-plan \
  --project-name fetchnow-staging \
  --env-file .env.staging \
  --compose-file compose.yaml \
  --compose-file compose.staging.yaml \
  --expected-revision <40-char-sha> \
  --deploy-root /srv/fetchnow-staging
```

Make target:

```bash
make release-deploy-plan EXPECTED_REVISION=<sha> DEPLOY_ROOT=/srv/fetchnow-staging
```

### Planner sequence

1. Validate inputs and env permissions
2. Verify target prepared release (source contract v2+)
3. Resolve `state/current.json` (v1→in-memory dual state or v2)
4. Verify current application release and database schema release
5. Read live DB heads (read-only `SELECT` via postgres container env)
6. Assert live heads equal **saved** `current.database.heads`
7. Discover target heads from exact target release source
8. Load and validate `compatibility.json` from target release source
9. Validate graph transition and policy match
10. Set `verified_backup_required` / `application_rollout_required`
11. Emit canonical redacted JSON plan to **stdout** (diagnostics to stderr)

The planner does **not** acquire the rollout mutation lock.

### Plan output fields

- `schema_version`, `project`, `current_revision`, `target_revision`
- `current_db_heads`, `target_db_heads`
- `migration_required`, `included_revisions`
- `compatibility_contract_sha256`
- `verified_backup_required`
- `previous_application_rollback_allowed`
- `database_downgrade_allowed` (always `false`)
- `application_rollout_required` (target application revision differs)
Stdout is deterministic canonical JSON (stable key order, sorted arrays, no
timestamps, no secrets, no absolute paths).

### No-migration case

When current DB heads equal target release heads:

- `migration_required = false`
- `included_revisions = []`
- `verified_backup_required = false`
- compatibility file must still exist and validate

### Initial deployment boundary (PRD1D)

`deploy-plan` requires an existing `state/current.json` and a verified current
prepared release. When `current.json` is absent, planning fails with an
explicit **PRD1D** diagnostic: initial database bootstrap and first staging
deployment are out of scope for PRD1C3B1.

The planner also rejects:

- a missing `alembic_version` table (uninitialized database)
- an empty `alembic_version` result without treating it as valid empty heads
- live database heads that drift from saved `current.database.heads`

There is no `--bootstrap`, adoption bypass, or implicit empty-head baseline.

### Planner output is advisory

Stdout JSON is canonical and deterministic for unchanged inputs, but the plan
is **time-sensitive**: database heads, container state, and prepared releases
must still match at B2 execution time. B2 repeats every check under the
shared deployment lock.

### Versioned release source contract

Prepared releases record an explicit `source_contract_version` in
`release.json` (manifest schema remains v1). Contract version is **never**
inferred from source file presence.

| Version | Required source paths | Used by |
|---------|----------------------|---------|
| **v1** (legacy) | PRD1C2/PRD1C3A pinned list (no `compatibility.json`) | Existing finalized releases; valid **current** release for rollout/recover/deploy-plan |
| **v2** (current prepare) | v1 paths + `deploy/migrations/compatibility.json` | Every new `release prepare` after PRD1C3B1; **required** for deploy-plan **target** releases |

Rules:

- Immutable finalized releases keep their historical verification contract forever.
- Idempotent re-prepare never rewrites an existing release manifest.
- Unknown `source_contract_version` values fail closed.
- A manifest declaring v1 verifies with the v1 path list even if extra files exist in `source/` (not promoted to v2).
- A manifest declaring v2 without `compatibility.json` fails verification.
- New prepare without `compatibility.json` in the trusted commit fails before publication.
- There is no operator flag to treat an old release as new or upgrade a manifest in place.

Deploy-plan:

- **Current release** may be legacy v1 (no policy file required).
- **Target release** must declare v2+ and pass strict policy validation.

### Migration-required case

When heads differ on a forward-reachable path:

- Exactly one contract transition must match `from_heads`, `to_heads`, and
  `included_revisions` exactly
- `verified_backup_required = true`
- downgrade, divergence, and unrelated histories are rejected

## Read-only guarantee

The planner must not invoke backup, Alembic mutation, SQL writes, container
recreate, image build/pull, `release prepare`, or rollout/recover. Allowed
operations are filesystem reads, release verification reads, read-only Compose
ps/config/inspect, read-only `psql SELECT`, image inspect, and free-space
checks.

When an unresolved migration journal exists, `deploy-plan` fails closed with a
clear diagnostic (mutation blocked until `recover-migration` resolves it).

## Scope boundary

| Phase | Responsibility |
|-------|----------------|
| PRD1C3B1 | Contract + read-only `deploy-plan` |
| PRD1C3B2A | Dual state + compatibility envelope (no DB mutation) |
| PRD1C3B2B2 | Verified migration transaction (`release-migrate`); see [chapter 29](29-verified-migration-transaction.md) |
| PRD1C3B2C | Unified deploy orchestration |
| PRD1C3A | Application rollout when compatibility envelope permits |
| PRD1D | First real staging deployment |

PR12 adds the `0004_download_observability` → `0005_download_file_details`
online-expand transition: nullable `suggested_filename` and `progress_percent`.
A previous PR11 application may INSERT/UPDATE without those columns. A BEFORE
UPDATE trigger clears `progress_percent` when `progress_stage` changes and
`NEW.progress_percent` is not distinct from `OLD.progress_percent`. PostgreSQL
cannot distinguish an omitted column from an explicit assignment of the same
value; both are normalized to NULL so PR11 lifecycle DML remains valid after a
PR12 percent write. An explicit write of a different illegal percent still
fails the CHECK. Downgrade drops the trigger, function, constraints, and
columns and does not rewrite `public_state`.

PR14 adds the `0005_download_file_details` → `0006_browser_delivery_grants`
online-expand transition: new `media_delivery_grants` table only. Previous
applications ignore the table; existing download/job CHECKs and enums are
unchanged. Downgrade drops only the new grant objects.
