# 15. Backup и restore

Backup — отдельная восстанавливаемая копия данных; snapshot фиксирует состояние volume/VM на момент времени, но может зависеть от того же provider/account и без DB coordination быть неконсистентным. Backup, для которого никогда не проверяли restore, — разновидность оптимизма, а не стратегия.

## Scope (PRD1B)

Репозиторий предоставляет **database-level logical backup** PostgreSQL (custom-format `pg_dump`) для staging Compose project:

- archive + SHA-256 + immutable `manifest.json`;
- atomic publication;
- structural validation (`pg_restore -l`);
- restore drill в изолированную temporary database;
- application/schema checks (Alembic);
- list / count-based prune (default dry-run).

**Не входит в PRD1B:** volume filesystem copy, backup `tmp`/media, restore поверх live DB, automatic rollback, cron/systemd, S3/off-host, encryption, `pg_basebackup`, WAL/PITR, host Nginx/TLS, deploy automation.

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

`verify` пересчитывает SHA-256, снова вызывает `pg_restore -l`, создаёт DB только с префиксом `fetchnow_restore_verify_`, восстанавливает с `--exit-on-error --no-owner --no-privileges`, проверяет:

- соединение;
- таблицу `alembic_version` (создаётся Alembic, не изобретена инструментом);
- revision ∈ discovered Alembic heads репозитория (`version_num`);
- required tables for the **current empty baseline** (`alembic_version` only — no invented application tables yet);
- отсутствие invalid indexes.

When real application tables appear in future migrations, extend semantic checks without changing the backup archive format.

Temporary DB всегда удаляется в `finally`. Attestation пишется отдельно; published `manifest.json` не мутируется.

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
