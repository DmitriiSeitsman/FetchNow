# 15. Backup и restore

Backup — отдельная восстанавливаемая копия данных; snapshot фиксирует состояние volume/VM на момент времени, но может зависеть от того же provider/account и без DB coordination быть неконсистентным. Backup, для которого никогда не проверяли restore, — разновидность оптимизма, а не стратегия.

## Scope (PRD1B + PRD1C3B2B1 foundation)

Репозиторий предоставляет **database-level logical backup** PostgreSQL (custom-format `pg_dump`) для staging Compose project:

- archive + SHA-256 + immutable `manifest.json`;
- atomic publication;
- structural validation (`pg_restore -l`);
- restore drill в изолированную temporary database;
- application/schema checks (Alembic);
- list / count-based prune (default dry-run).

**PRD1C3B2B1 (foundation only):** один непрерывный `BackupRootLock` на backup root для create→verify в одной сессии; **exact set equality** между explicit expected Alembic heads, static heads из явно указанного migrations/versions каталога schema release, и **всеми** строками `alembic_version` после restore; immutable attestation schema **v2** для успешных проверок; legacy v1 attestations остаются неизменными историческими артефактами. **Не выполняет** production Alembic upgrade/downgrade/stamp, migration transaction, deploy/rollout, изменения `state/current.json`, retention holds.

**Не входит в PRD1B/PRD1C3B2B1:** volume filesystem copy, backup `tmp`/media, restore поверх live DB, automatic rollback, cron/systemd, S3/off-host, encryption, `pg_basebackup`, WAL/PITR, host Nginx/TLS, deploy automation, unified migrate→rollout (PRD1C3B2C).

Локальный backup root (например `/srv/fetchnow-staging/backups`) **не** является off-host защитой. Scheduling и off-host copy — отдельная последующая работа. Recovery ≠ automatic application rollback.

## Tool

```bash
python3.12 scripts/fetchnow_pg_backup_cli.py --help
# или Make-обёртки ниже
```

Обязательные операторские входы для create/verify: `--project-name`, `--env-file`, один или несколько `--compose-file` (порядок важен), `--backup-root`. Project name **не** выводится из CWD. Staging не должен подхватывать `compose.override.yaml`.

Credentials не передаются в argv и не печатаются: утилиты выполняются внутри service `postgres`, используя его env (`POSTGRES_USER` / `POSTGRES_DB`).

### Staging create

```bash
umask 077
install -d -m 0700 /srv/fetchnow-staging/backups
make pg-backup-create BACKUP_ROOT=/srv/fetchnow-staging/backups
# эквивалент:
python3.12 scripts/fetchnow_pg_backup_cli.py create \
  --project-name fetchnow-staging \
  --env-file .env.staging \
  --compose-file compose.yaml \
  --compose-file compose.staging.yaml \
  --backup-root /srv/fetchnow-staging/backups
```

### Verify / list / prune

Operator CLI `verify` accepts explicit schema authority (repeatable for multi-head graphs):

```bash
python3.12 scripts/fetchnow_pg_backup_cli.py verify \
  --project-name fetchnow-staging \
  --env-file .env.staging \
  --compose-file compose.yaml \
  --compose-file compose.staging.yaml \
  --backup-root /srv/fetchnow-staging/backups \
  --backup-id 20260804T012345Z_d26d920 \
  --expected-alembic-head <revision> \
  --migrations-versions-dir /path/to/schema-release/migrations/versions
```

When `--expected-alembic-head` is omitted, heads are inferred only at the CLI boundary from `--migrations-versions-dir` (default: repository worktree `backend/migrations/versions`). **Lower authority** than the strict session API used by future PRD1C3B2B2 migration transaction callers, which must pass both explicit expected heads and explicit migrations directory.

```bash
make pg-backup-verify BACKUP_ROOT=/srv/fetchnow-staging/backups BACKUP_ID=20260804T012345Z_d26d920
make pg-backup-list BACKUP_ROOT=/srv/fetchnow-staging/backups
make pg-backup-prune-dry BACKUP_ROOT=/srv/fetchnow-staging/backups
# фактическое удаление только явно:
python3.12 scripts/fetchnow_pg_backup_cli.py prune \
  --backup-root /srv/fetchnow-staging/backups --keep 7 --apply
```

Default retention keep count = **7**. Prune никогда не удаляет newest valid backup и newest successfully restore-verified backup; incomplete/malformed/symlink entries не удаляются. Default prune — dry-run.

## Layout

```text
/srv/fetchnow-staging/backups/
  20260804T012345Z_<gitrev>/
    database.dump          # mode 0600, custom format
    manifest.json          # mode 0600, schema v1 (immutable after publish)
    verifications/
      verify_<ts>_passed.json
  .lock
```

Backup root и каталоги: `0700`. Root должен быть **вне** Git worktree (не `/`, не `$HOME`, не корень репозитория). Publication atomic: `.incomplete-<uuid>` → rename.

## Manifest и checksum

Manifest schema v1 фиксирует project, DB name, Postgres/`pg_dump` versions, compression, size, SHA-256, git revision (если доступен), structural validation result. Пароли, `DATABASE_URL` и содержимое таблиц не записываются.

Перед publish обязателен `pg_restore -l`. Файл ненулевого размера без TOC **не** считается валидным backup.

## Restore verification

`verify` пересчитывает SHA-256, снова вызывает `pg_restore -l`, создаёт DB только с префиксом `fetchnow_restore_verify_`, восстанавливает с `--exit-on-error --no-owner --no-privileges`, затем проверяет:

1. explicit expected Alembic heads (non-empty sorted unique revision IDs);
2. static head set from the explicit migrations/versions directory (fail closed on malformed/cyclic/duplicate-parent graphs; reject symlink leaf directory);
3. **exact set equality** `expected == static == all rows in restored alembic_version` (multi-head supported; never “last row” semantics);
4. semantic checks (connection, required tables for current baseline, invalid indexes);
5. cleanup of the temporary database in `finally` (cleanup failure ⇒ verification failure; no passed v2 attestation).

### Backup-root session (PRD1C3B2B1)

Programmatic callers use one continuous lock:

```python
from fetchnow_pg_backup.session import open_backup_root_session

with open_backup_root_session(backup_root=..., repo_root=...) as session:
    created = session.create_backup(target=...)
    verified = session.verify_backup(
        target=...,
        backup_dir=created.backup_dir,
        expected_alembic_heads=(...),
        migrations_versions_dir=Path(".../migrations/versions"),
    )
```

Public `create_backup()` / `verify_backup()` remain available and acquire their own session when called independently. No `skip_lock` / `assume_locked` bypass exists.

Source application database is never restored over, dropped, or renamed. Temporary DB name remains constrained to the verification prefix.

### Attestation v1 vs v2

| Schema | When written | Exact-head proof |
| --- | --- | --- |
| v1 | historical PRD1B passed/failed records only | no — legacy semantic-only evidence |
| v2 passed | successful verifications after PRD1C3B2B1 | yes — binds expected, static, restored head sets and `migrations_graph_sha256` |
| v2 failed | modern failed verifications (authority/archive/restore/semantic/cleanup failures) | no — records expected/static authority and optional restored heads without claiming equality |

v2 passed attestations require semantic success, cleanup success, and exact equality between `expected_alembic_heads`, `static_alembic_heads`, and `verified_restored_heads`. v2 failed attestations never claim `exact_head_proof`; they bind the selected migrations authority via content SHA-256 (`migrations_graph_sha256`) rather than host paths. New failures after B2B1 use schema v2 — v1 is not written for new verification attempts.

Typed `VerifyResult` exposes attestation schema version, path, SHA-256, expected/static/restored heads, `migrations_graph_sha256`, semantic/cleanup flags, and `exact_head_proof` for PRD1C3B2B2 orchestration (no stdout parsing required).

## Password URL caveat

Спецсимволы в `POSTGRES_PASSWORD` могут ломать raw URI interpolation `DATABASE_URL` (зафиксировано в PRD1A). PRD1B не меняет application config. Backup tool не парсит `DATABASE_URL` и не печатает пароль. **PRD1C1** staging preflight закрывает caveat: URL-safe password charset + length ≥ 32 (см. [главу 24](24-release-preflight-health.md)).

## Tests

```bash
make pg-backup-test            # unit
make pg-backup-integration     # unique project fetchnow-backup-test-<suffix>; builds API image
```

Integration always uses a per-run project name matching `fetchnow-backup-test-[a-z0-9]{8,32}` and refuses `fetchnow`, `fetchnow-staging`, `fetchnow-prod`, and the bare shared name `fetchnow-backup-test`. `down -v` is allowed only for that exact validated name. CI runs this in a dedicated **Postgres backup restore integration** job that explicitly builds the API image on a fresh runner (no shared Docker state with other jobs).

## Ошибки и rollback

Нулевой/truncated dump, checksum mismatch, failed catalog check → backup/verify недействителен. Не удалять предыдущую good copy. Restore drill никогда не пишет в source application DB. **DANGER:** не писать пароль в filename/command; не считать local backup off-host copy; не восстанавливать поверх production без отдельного recovery plan.
