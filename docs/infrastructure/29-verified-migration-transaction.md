# Verified migration transaction (PRD1C3B2B2)

PRD1C3B2B2 adds a **standalone, journaled, verified PostgreSQL migration transaction**. It does **not** activate target application containers. Unified migration→application rollout belongs to PRD1C3B2C.

## Boundary

| In scope (B2B2) | Out of scope |
|-----------------|--------------|
| Revalidate migration plan under rollout lock | Target application activation |
| Create + restore-verify source-schema backup (B2B1) | Combined migrate+rollout command |
| Retention hold on verified backup | Automatic PostgreSQL restore / downgrade |
| Forward Alembic from target release API image | Host/worktree Alembic authority |
| Exact post-migration head verification | Staging/production migration in this PR |
| Current-application health with unchanged containers | Off-host backup upload / encryption |

## Lock order

```text
RolloutLock
  → BackupRootLock (via open_backup_root_session())
```

Application rollout and database migration cannot run concurrently. Backup create→verify→hold runs under one backup-root session while the rollout lock is held.

## Authority split

- **Source schema release** (from `current.json` database side): used to restore-verify the pre-migration backup.
- **Target schema release** (requested revision): supplies migrations directory, compatibility contract, manifest, and immutable API image for Alembic.

`current.json` schema v2 persists `database.schema_release_revision` as the
sole migrations-graph authority. Graph SHA-256 appears in migration
journals/backup attestations/holds for cross-check only; it is recomputed
from the verified prepared release at commit/recovery gates and is **not**
stored in `current.json`.

## Transaction flow

```text
revalidate plan under lock
→ verified backup (v2 attestation, exact source heads)
→ retention hold
→ re-read live source heads
→ Alembic upgrade (target release image, --pull never, --no-deps)
→ verify target heads
→ verify current application health (containers unchanged)
→ CAS current.json (database side + compatibility envelope only)
→ migration result.json
→ release retention hold
```

## State machine

Phases include `planned` → `backup_started` → … → `migration_started` → … → `committed`.

After `migration_started`, failures enter **`migration_requires_recovery`** (not a silent terminal `failed`). Pre-mutation failures may terminal `failed` and release the hold.

## Commands

```bash
# Production (staging project fixed in Makefile)
make release-migrate \
  EXPECTED_REVISION=<40-char-sha> \
  ENV_FILE=.env.staging \
  DEPLOY_ROOT=/srv/fetchnow-staging \
  BACKUP_ROOT=/srv/fetchnow-backups

make release-migration-recover \
  MIGRATION_ID=<uuid> \
  ACTION=accept_source|accept_target \
  ENV_FILE=.env.staging \
  DEPLOY_ROOT=/srv/fetchnow-staging \
  BACKUP_ROOT=/srv/fetchnow-backups
```

CLI equivalents:

```bash
python3.12 scripts/fetchnow_release_cli.py migrate …
python3.12 scripts/fetchnow_release_cli.py recover-migration …
```

## Recovery

Explicit `recover-migration` actions:

- **`accept_source`**: live heads equal planned source; `current.json` unchanged; terminally resolve as failed; release hold.
- **`accept_target`**: live heads equal planned target; commit database side of `current.json` if needed; resolve; release hold.

Partial/divergent heads reject both accept actions; hold remains; operator escalation required. No automatic restore or downgrade.

- **`release_hold`**: migration result already `committed` but retention hold is still active (for example hold release failed after commit); releases the hold without mutating database state.

### `release_hold` guards and evidence

| Guard | Behavior |
| --- | --- |
| Journal ownership | `--migration-id` must match an existing migration directory |
| Prior resolution | If `resolution.json` exists, returns success idempotently with prior action |
| Terminal result | Requires `result.json` with status `committed` |
| Active hold | Requires an active hold for the migration whose expected heads and graph SHA match the journaled plan |
| Live DB / `current.json` | No database or application mutation; health/container checks are not re-run |
| Rollout lock | Held for the duration of the action |

**Idempotency:** repeating `release_hold` after a successful resolution returns
`OK: migration already resolved action='release_hold'` without error.

**Success evidence:** stdout includes
`OK: retention hold released (<hold_id>)` and
`OK: committed migration hold release completed`.

**Hold release fails again:** the action returns `FAIL:` (migration remains
`committed`; hold stays active and backup remains prune-protected). Operator
must fix backup-root permissions/lock contention and retry `release_hold`.

If migration commits but hold release fails, the transaction returns success with a visible WARN and the hold remains active until `release_hold` or accept-path recovery completes it.

## Idempotency

When database state, live heads, and compatibility envelope already match the target: read-only verify, **`already migrated`**, no backup/journal/Alembic/state rewrite.

## Integration testing

Isolated Docker integration uses only `fetchnow-migration-test-*` projects:

```bash
make release-migration-test
python3.12 scripts/release_migration_integration_test.py
```

## Related chapters

- [Backups and restore](15-backups-and-restore.md) — B2B1 backup verification and retention holds
- [Command reference](21-command-reference.md)
- [Deployment transaction rollback](26-deployment-transaction-rollback.md)
- [Migration compatibility & deploy-plan](27-migration-compatibility-deployment-planning.md)
- [Dual application/database state](28-dual-application-database-state.md)
