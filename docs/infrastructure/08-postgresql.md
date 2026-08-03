# 08. PostgreSQL

## Основы

Database server обслуживает несколько databases. Role/user идентифицирует клиента и задаёт права; password подтверждает доступ. Connection string объединяет driver, credentials, host, port и database. Schema группирует объекты. Transaction применяет набор операций атомарно. Migration изменяет schema; Alembic revision — версия такого изменения.

FetchNow использует container `postgres` внутри Compose network и `postgres:5432` в `DATABASE_URL`. Host PostgreSQL приложением не используется, port 5432 наружу не публикуется. Данные живут в `fetchnow_pgdata`, а не в writable layer.

## Migrations

API намеренно не выполняет migrations при старте: несколько replicas могли бы конкурировать, а неожиданный schema change усложнил бы rollback. Единственный migration script репозитория — `deploy/scripts/migrate.sh`; Compose-команда `make migrate` фактически запускает `alembic upgrade head` в one-shot `api` container и не вызывает этот shell script напрямую.

```bash
docker compose -p fetchnow-staging -f compose.yaml \
  -f deploy/compose/compose.prod.yaml exec postgres \
  sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
docker compose -p fetchnow-staging -f compose.yaml \
  -f deploy/compose/compose.prod.yaml run --rm api alembic current
docker compose -p fetchnow-staging -f compose.yaml \
  -f deploy/compose/compose.prod.yaml run --rm api alembic upgrade head
```

Первая команда проверяет readiness; переменные должны быть загружены в операторскую shell без печати. `alembic current` — **CHECK**, показывает текущую revision. `upgrade head` — **WARNING modifying**, применяет migrations до последней revision. В репозитории сейчас одна revision `0001_baseline`.

## Проверка и ошибки

После migration: снова `alembic current`, затем API readiness. `connection refused` означает сеть/процесс; authentication failed — credentials; database does not exist — неверное имя/инициализация. Не меняйте password в одном service: URL API/worker и PostgreSQL должны согласовываться.

## Rollback

Rollback приложения не означает Alembic downgrade: старая версия должна быть совместима с новой schema. Перед migration создают dump и читают migration code. Частично применённую/непонятную migration не «чинят» повторным downgrade на production; останавливают rollout и анализируют таблицу `alembic_version` и DDL.

**DANGER:** не выполнять `DROP`, `TRUNCATE`, `down -v` или ручное удаление data directory. Не вставлять credentials в команды, history и отчёты.
