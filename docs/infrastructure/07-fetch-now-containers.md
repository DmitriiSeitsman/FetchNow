# 07. Контейнеры FetchNow

## Фактический inventory

Источник — `compose.yaml`, `compose.override.yaml`, `compose.staging.yaml`, `deploy/compose/compose.prod.yaml` и Dockerfiles.

| Service | Image/build | Port/publish | User | Health, dependency, volume |
|---|---|---|---|---|
| `gateway` | `fetchnow-gateway:${FETCHNOW_RELEASE_REVISION}` (`local` or full SHA), `deploy/nginx/Dockerfile` | container `8080`; local host `8080` (override); staging `127.0.0.1:8091` | UID 10001 | live endpoint; ждёт healthy `api`, `web`; volume нет; OCI revision label |
| `web` | `fetchnow-web:${FETCHNOW_RELEASE_REVISION}`, `web/Dockerfile` | `8080`; host port нет | UID 10001 | GET `/`; dependency/volume нет; OCI revision label |
| `api` | `fetchnow-api:${FETCHNOW_RELEASE_REVISION}`, `backend/Dockerfile` | `8000`; local debug host `8000` (override); staging host port нет | UID 10001 | live endpoint; ждёт healthy `postgres`; volume `tmp`; OCI revision label |
| `worker` | тот же `fetchnow-api` image/Dockerfile | HTTP-порта нет | UID 10001 | healthcheck отключён; ждёт healthy `postgres`; volume `tmp`; inherits API image label |
| `postgres` | `postgres:16.9-alpine`, registry image | `5432`; host port нет | не pinned в Compose; управляет official entrypoint | `pg_isready`; volume `pgdata` |

Все services находятся в bridge network `fetchnow` (Docker name `{project}_fetchnow`). `gateway` ждёт healthy `api` и `web`; `api`/`worker` ждут healthy `postgres`. Это startup ordering, не гарантия дальнейшей доступности.


## Компоненты и restart

### gateway и web

Оба non-root (UID 10001), persistent state отсутствует. Restart запускает тот же container/image, recreate создаёт container заново. Gateway передаёт request ID и пишет access log в stdout. Логи: `docker compose ... logs gateway web`.

### api

Image `fetchnow-api:<revision>` собирается из `backend/Dockerfile` (development `local`, staging full SHA); процесс non-root UID 10001 и подключает logical volume `tmp` к `/var/lib/fetchnow/tmp` (Docker name `{project}_tmp`). Final stage несёт `org.opencontainers.image.revision`. Operator release builds (PRD1C2) materialize an exact `git archive` snapshot and build revision tags from that snapshot — not from a dirty worktree; see [глава 25](25-release-materialization-build.md). Внутри API process живут `URLValidator` и один pooled `SafeHTTPClient`: lifespan создаёт их вместе с DNS resolver, а при shutdown закрывает HTTP client и DB engine. API не запускает migrations при старте. Restart не теряет named volume. Логи: service `api`; liveness не проверяет БД, readiness проверяет. См. [главу 24](24-release-preflight-health.md).

### worker

Тот же backend image и volume `tmp`, команда `fetchnow-worker`, non-root UID 10001. Worker выполняет durable media-inspection orchestration (PR5) и download execution (PR6): reclaim/expire, `SKIP LOCKED` claim, lease/fencing. Inspection — когда `MEDIA_JOBS_ENABLED` и `MEDIA_INSPECTION_ENABLED` истинны; downloads — когда `MEDIA_DOWNLOADS_ENABLED` и `MEDIA_INSPECTION_ENABLED` (оба feature default false). Compose прокидывает `MEDIA_JOBS_*` и API-safe `MEDIA_DOWNLOAD_*` (TTL/max attempts) на api; worker получает полный `MEDIA_INSPECTION_*` / `MEDIA_DOWNLOAD_*` включая yt-dlp path и temp root. API create/status не запускает yt-dlp и не отдаёт файлы клиенту. Restart прерывает процесс через SIGTERM с bounded grace; cancel/lease loss не считаются успехом.

### postgres

Официальный image хранит cluster в `pgdata:/var/lib/postgresql/data` (Docker name `{project}_pgdata`). Recreate контейнера сохраняет volume, удаление volume уничтожает БД. Логи: service `postgres`.

## Volumes

- `pgdata` → `{project}_pgdata` — persistent database state.
- `tmp` → `{project}_tmp` — общий временный storage API/worker; worker download artifacts живут под `MEDIA_DOWNLOAD_TEMP_ROOT` внутри этого volume (private; не публичный delivery).

При `COMPOSE_PROJECT_NAME=fetchnow` имена совпадают с историческими `fetchnow_pgdata` / `fetchnow_tmp`. Staging (`fetchnow-staging`) получает отдельные volumes. Explicit global `name:` больше не используется (PRD1A).

Отдельного media volume нет. Не называйте `tmp` архивом или backup.

## Проверки

```bash
docker compose --env-file .env.staging --project-name fetchnow-staging \
  -f compose.yaml -f compose.staging.yaml ps
docker compose --env-file .env.staging --project-name fetchnow-staging \
  -f compose.yaml -f compose.staging.yaml logs --tail 100 gateway api worker postgres web
docker volume inspect fetchnow_pgdata fetchnow_tmp \
  fetchnow-staging_pgdata fetchnow-staging_tmp
docker network inspect fetchnow-staging_fetchnow
```

Ожидается `running/healthy` для всех кроме worker, который должен быть `running`; точное имя network подтверждайте `docker network ls`, так как Compose naming зависит от project/config.

**DANGER:** не входить в container для ручного изменения кода, не удалять volumes и не публиковать PostgreSQL. **ROLLBACK:** recreate из сохранённого commit/image; данные восстанавливать отдельно.
