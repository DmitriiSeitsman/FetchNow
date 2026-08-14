# 08. PostgreSQL

## Основы

Database server обслуживает несколько databases. Role/user идентифицирует клиента и задаёт права; password подтверждает доступ. Connection string объединяет driver, credentials, host, port и database. Schema группирует объекты. Transaction применяет набор операций атомарно. Migration изменяет schema; Alembic revision — версия такого изменения.

FetchNow использует container `postgres` внутри Compose network и `postgres:5432` в `DATABASE_URL`. Host PostgreSQL приложением не используется, port 5432 наружу не публикуется. Данные живут в logical volume `pgdata` (Docker name `{project}_pgdata`, для local `fetchnow_pgdata`), а не в writable layer.

## Migrations

API намеренно не выполняет migrations при старте: несколько replicas могли бы конкурировать, а неожиданный schema change усложнил бы rollback. Единственный migration script репозитория — `deploy/scripts/migrate.sh`; Compose-команда `make migrate` фактически запускает `alembic upgrade head` в one-shot `api` container и не вызывает этот shell script напрямую.

```bash
docker compose --env-file .env.staging --project-name fetchnow-staging \
  -f compose.yaml -f compose.staging.yaml exec postgres \
  sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
docker compose --env-file .env.staging --project-name fetchnow-staging \
  -f compose.yaml -f compose.staging.yaml run --rm api alembic current
docker compose --env-file .env.staging --project-name fetchnow-staging \
  -f compose.yaml -f compose.staging.yaml run --rm api alembic upgrade head
```

Первая команда проверяет readiness; переменные должны быть загружены в операторскую shell без печати. `alembic current` — **CHECK**, показывает текущую revision. `upgrade head` — **WARNING modifying**, применяет migrations до последней revision. В репозитории head — `0006_browser_delivery_grants` (после `0005_download_file_details`) (после `0004_download_observability` / `0003_download_jobs` / `0002_media_jobs` / `0001_baseline`). PR6 добавляет таблицу `media_download_jobs`. PR10 добавляет `progress_stage` и `cancel_requested_at`. PR12 добавляет nullable `suggested_filename` и `progress_percent`; previous PR11 applications may INSERT/UPDATE without those columns. A BEFORE UPDATE trigger clears `progress_percent` when `progress_stage` changes and the percent value is unchanged (`NEW IS NOT DISTINCT FROM OLD`). PostgreSQL cannot distinguish an omitted column from an explicit assignment of the same value; both are normalized to NULL so a rolled-back PR11 application remains CHECK-compatible after a PR12 percent write. An explicit write of a different illegal percent still fails the CHECK. Public API `cancelled` кодируется как PR9 `public_state=expired` плюс `progress_stage=cancelled` и `cancel_requested_at`. Online expand сохраняет `progress_stage` server default **и** BEFORE trigger, который выставляет coherent `progress_stage` для PR9 UPDATE, не пишущих новую колонку, и не переписывает PR10 cancellation encoding. CHECK по-прежнему отклоняет explicit illegal PR10 state/stage pairs. Downgrade drops the PR12 trigger, function, constraints, and columns; a new `public_state` enum is not required.

## Проверка и ошибки

После migration: снова `alembic current`, затем API readiness. `connection refused` означает сеть/процесс; authentication failed — credentials; database does not exist — неверное имя/инициализация. Не меняйте password в одном service: URL API/worker и PostgreSQL должны согласовываться.

## Rollback

Rollback приложения не означает Alembic downgrade: старая версия должна быть совместима с новой schema. Перед migration создают dump и читают migration code. Частично применённую/непонятную migration не «чинят» повторным downgrade на production; останавливают rollout и анализируют таблицу `alembic_version` и DDL.

**DANGER:** не выполнять `DROP`, `TRUNCATE`, `down -v` или ручное удаление data directory. Не вставлять credentials в команды, history и отчёты.

## Backup / restore verification (PRD1B)

Logical dump + restore drill: см. [главу 15](15-backups-and-restore.md). Краткая staging-команда:

```bash
make pg-backup-create BACKUP_ROOT=/srv/fetchnow-staging/backups
make pg-backup-verify BACKUP_ROOT=/srv/fetchnow-staging/backups BACKUP_ID=<id>
```

Утилиты `pg_dump` / `pg_restore` / `psql` выполняются внутри container image `postgres:16.9-alpine`, чтобы клиент совпадал с сервером.
