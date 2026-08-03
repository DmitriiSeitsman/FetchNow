# 07. Контейнеры FetchNow

## Фактический inventory

Источник — `compose.yaml`, `compose.override.yaml`, `deploy/compose/compose.prod.yaml` и Dockerfiles локального `main`.

| Service | Image/build | Port/publish | User | Health, dependency, volume |
|---|---|---|---|---|
| `gateway` | `fetchnow-gateway:local`, `deploy/nginx/Dockerfile` | `8080`; staging target `127.0.0.1:8091` | UID 10001 | live endpoint; ждёт healthy `api`, `web`; volume нет |
| `web` | `fetchnow-web:local`, `web/Dockerfile` | `8080`; host port нет | UID 10001 | GET `/`; dependency/volume нет |
| `api` | `fetchnow-api:local`, `backend/Dockerfile` | `8000`; staging host port нет | UID 10001 | live endpoint; ждёт healthy `postgres`; `fetchnow_tmp` |
| `worker` | тот же backend image/Dockerfile | HTTP-порта нет | UID 10001 | healthcheck отключён; ждёт healthy `postgres`; `fetchnow_tmp` |
| `postgres` | `postgres:16.9-alpine`, registry image | `5432`; host port нет | не pinned в Compose; управляет official entrypoint | `pg_isready`; `fetchnow_pgdata` |

Все services находятся в bridge network `fetchnow`. `gateway` ждёт healthy `api` и `web`; `api`/`worker` ждут healthy `postgres`. Это startup ordering, не гарантия дальнейшей доступности.

## Компоненты и restart

### gateway и web

Оба non-root (UID 10001), persistent state отсутствует. Restart запускает тот же container/image, recreate создаёт container заново. Gateway передаёт request ID и пишет access log в stdout. Логи: `docker compose ... logs gateway web`.

### api

Image `fetchnow-api:local` собирается из `backend/Dockerfile`; процесс non-root UID 10001 и подключает `fetchnow_tmp` к `/var/lib/fetchnow/tmp`. Внутри API process живут `URLValidator` и один pooled `SafeHTTPClient`: lifespan создаёт их вместе с DNS resolver, а при shutdown закрывает HTTP client и DB engine. API не запускает migrations при старте. Restart не теряет named volume. Логи: service `api`; liveness не проверяет БД, readiness проверяет.

### worker

Тот же backend image и `fetchnow_tmp`, команда `fetchnow-worker`, non-root UID 10001. Сейчас это idle heartbeat без media jobs и без SafeHTTPClient; отсутствие Docker healthcheck требует проверять state/logs. Restart прерывает процесс через SIGTERM. PostgreSQL queue и recovery jobs ещё не реализованы.

### postgres

Официальный image хранит cluster в `fetchnow_pgdata:/var/lib/postgresql/data`. Recreate контейнера сохраняет volume, удаление volume уничтожает БД. Логи: service `postgres`.

## Volumes

- `fetchnow_pgdata` — persistent database state.
- `fetchnow_tmp` — общий временный storage API/worker; в текущем коде media pipeline отсутствует.

Отдельного media volume нет. Не называйте `fetchnow_tmp` архивом или backup. Оба explicit volume name глобальны для Docker host; это нерешённый blocker совместного local/staging deployment, описанный в [главе 06](06-docker-compose.md).

## Проверки

```bash
docker compose -p fetchnow-staging -f compose.yaml \
  -f deploy/compose/compose.prod.yaml ps
docker compose -p fetchnow-staging -f compose.yaml \
  -f deploy/compose/compose.prod.yaml logs --tail 100 gateway api worker postgres web
docker volume inspect fetchnow_pgdata fetchnow_tmp
docker network inspect fetchnow-staging_fetchnow
```

Ожидается `running/healthy` для всех кроме worker, который должен быть `running`; точное имя network подтверждайте `docker network ls`, так как Compose naming зависит от project/config.

**DANGER:** не входить в container для ручного изменения кода, не удалять volumes и не публиковать PostgreSQL. **ROLLBACK:** recreate из сохранённого commit/image; данные восстанавливать отдельно.
