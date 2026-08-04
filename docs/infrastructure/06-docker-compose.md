# 06. Docker Compose

## Понятия

Compose описывает project из services, networks и volumes. Service — шаблон одного или нескольких containers. Profile включает опциональную группу (в FetchNow profiles нет). Override объединяется с основным YAML. `environment` задаёт значения прямо, `env_file` загружает файл в контейнер (в текущем Compose `env_file` не используется). `depends_on` задаёт порядок и может ждать healthy dependency, но не заменяет runtime retry. Resource limits ограничивают CPU/RAM.

Compose interpolation (файл `.env` рядом с Compose / `--env-file`) подставляет `${VAR}` **при рендере** YAML. Это не то же самое, что переменные внутри контейнера: application env нужно явно перечислить в `services.*.environment`.

## Слои Compose (PRD1A)

| Файл | Назначение |
|---|---|
| `compose.yaml` | нейтральная топология: gateway/api/worker/postgres/web, network, logical volumes `pgdata`/`tmp`, healthchecks; **без** host ports и **без** top-level `name:` |
| `compose.override.yaml` | local-only: gateway `8080`, api debug `8000`, `APP_ENV=development` |
| `compose.staging.yaml` | staging-only: loopback gateway, fail-closed Postgres secrets, без override |
| `deploy/compose/compose.prod.yaml` | optional production-shaped fragment (host gateway port) |

Имена project задаются только через `COMPOSE_PROJECT_NAME` / `--project-name`. Top-level Compose `name:` и `container_name` запрещены.

## Изоляция volumes

Явные `volumes.*.name: fetchnow_pgdata` удалены: они обходили project scoping и позволяли разным Compose projects на одном Docker host подключить один и тот же volume.

Логические ключи:

```yaml
volumes:
  pgdata:
  tmp:
```

Docker volume names становятся `{project}_pgdata` и `{project}_tmp`, например:

- local `COMPOSE_PROJECT_NAME=fetchnow` → `fetchnow_pgdata`, `fetchnow_tmp` (совпадает с прежними именами при project=`fetchnow`)
- staging `--project-name fetchnow-staging` → `fetchnow-staging_pgdata`, `fetchnow-staging_tmp`

Networks: `{project}_fetchnow`.

**CHECK:**

```bash
docker volume ls | grep fetchnow
COMPOSE_PROJECT_NAME=fetchnow docker compose -f compose.yaml config --format json | python3 -c "import json,sys; c=json.load(sys.stdin); print({k:v.get('name') for k,v in c['volumes'].items()})"
docker compose --project-name fetchnow-staging -f compose.yaml -f compose.staging.yaml --env-file .env.staging.example config --format json | python3 -c "import json,sys; c=json.load(sys.stdin); print(c['name'], {k:v.get('name') for k,v in c['volumes'].items()})"
```

Автоматическая миграция volumes в PRD1A **не выполняется**. Копирование PostgreSQL data directory при работающем Postgres запрещено. См. раздел «Existing volumes» ниже и [главу 08](08-postgresql.md).

## Local development

Стандартные команды (подхватывают `compose.override.yaml`):

```bash
# Рекомендуется gitignored `.env` с COMPOSE_PROJECT_NAME=fetchnow (см. .env.example)
docker compose build
docker compose up -d
docker compose ps
docker compose logs -f --tail=200
docker compose down   # без -v
```

Makefile: `make up`, `make down`, `make logs`, `make compose-check` (`compose-check` / `make check` need Docker Compose v2 CLI; they run `docker compose config` only — no stack start, no `down -v`).

## Staging

Всегда явный project name и file set, чтобы **не** загружать `compose.override.yaml`:

```bash
cp .env.staging.example .env.staging   # один раз; заполнить секреты; chmod 600
docker compose \
  --env-file .env.staging \
  --project-name fetchnow-staging \
  -f compose.yaml \
  -f compose.staging.yaml \
  config
docker compose \
  --env-file .env.staging \
  --project-name fetchnow-staging \
  -f compose.yaml \
  -f compose.staging.yaml \
  build
docker compose \
  --env-file .env.staging \
  --project-name fetchnow-staging \
  -f compose.yaml \
  -f compose.staging.yaml \
  up -d
docker compose \
  --env-file .env.staging \
  --project-name fetchnow-staging \
  -f compose.yaml \
  -f compose.staging.yaml \
  ps
docker compose \
  --env-file .env.staging \
  --project-name fetchnow-staging \
  -f compose.yaml \
  -f compose.staging.yaml \
  logs --tail 200
docker compose \
  --env-file .env.staging \
  --project-name fetchnow-staging \
  -f compose.yaml \
  -f compose.staging.yaml \
  down   # без -v
```

**CHECK** rendered staging: пять services; единственная host-публикация gateway `127.0.0.1:8091→8080`; нет host ports у postgres/api/worker/web; volume names `fetchnow-staging_*`; нет bind mounts и reload/watch.

## Release identity (PRD1C1)

Application images use one variable `FETCHNOW_RELEASE_REVISION`:

- development: defaults to `local`
- staging: fail-closed full 40-char lowercase SHA on image tags and build args

API and worker share `fetchnow-api:<revision>`. Web/gateway use the same revision. Postgres remains pinned. See [глава 24](24-release-preflight-health.md).

Host Nginx/TLS, off-host backup copy, container deploy/rollback automation — PRD1C3–PRD1D (logical backup tooling: PRD1B / [глава 15](15-backups-and-restore.md); preflight/health: PRD1C1 / [глава 24](24-release-preflight-health.md); exact-revision materialize + image build: PRD1C2 / [глава 25](25-release-materialization-build.md)).

**CHECK:** rendered config может содержать пароль БД; не прикладывайте полный вывод к публичному тикету.

## Existing volumes (safety)

1. Inspect old/current names: `docker volume ls`, `docker volume inspect fetchnow_pgdata fetchnow_tmp`.
2. Render new names with `docker compose ... config` as above.
3. If local project remains `fetchnow` and keys are `pgdata`/`tmp`, Compose reuse existing `fetchnow_pgdata` / `fetchnow_tmp` automatically.
4. If the project name changes (e.g. directory-derived name ≠ `fetchnow`), Compose creates **new** empty volumes; old data remains on the previous volume names until a separate controlled migration.
5. Never `docker compose down -v` against development or staging as a routine step.
6. Never copy live PostgreSQL data directories while Postgres is running.

## Ошибки и откат

- Запуск staging без `-f compose.staging.yaml` или без `--project-name` может подхватить local override / неверный project.
- `restart` не применяет новый image/env; нужен recreate через `up -d`.
- **DANGER:** никогда не добавлять `-v` к `down` в штатной операции.

**ROLLBACK:** вернуться к сохранённому Git commit/image и выполнить `up -d` после compatibility check.
