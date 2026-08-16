# 07. Контейнеры FetchNow

## Фактический inventory

Источник — `compose.yaml`, `compose.override.yaml`, `compose.staging.yaml`, `deploy/compose/compose.prod.yaml` и Dockerfiles.

| Service | Image/build | Port/publish | User | Health, dependency, volume |
|---|---|---|---|---|
| `gateway` | `fetchnow-gateway:${FETCHNOW_RELEASE_REVISION}` (`local` or full SHA), `deploy/nginx/Dockerfile` | container `8080`; local host `8080` (override); staging `127.0.0.1:8091` | UID 10001 | live endpoint; ждёт healthy `api`, `web`; volume нет; OCI revision label |
| `web` | `fetchnow-web:${FETCHNOW_RELEASE_REVISION}`, `web/Dockerfile` | `8080`; host port нет | UID 10001 | GET `/`; build args `PUBLIC_SITE_URL`, `PUBLIC_MEDIA_FLOW_ENABLED` (default `false`), `PUBLIC_SEARCH_INDEXING_ENABLED` (default `false`, staging hard-coded `false`); dependency/volume нет; OCI revision label |
| `api` | `fetchnow-api:${FETCHNOW_RELEASE_REVISION}`, `backend/Dockerfile` | `8000`; local debug host `8000` (override); staging host port нет | UID 10001 | live endpoint; ждёт healthy `postgres`; **без** volume `tmp` (PR7); OCI revision label |
| `storage-init` | тот же `fetchnow-api` image, `fetchnow-storage-init` | HTTP-порта нет | UID 10001 | oneshot; `restart: no`; `network_mode: none`; rw `tmp` only; mkdir/validate download root; без DB/tool env; без sleep |
| `delivery` | тот же `fetchnow-api` image, `fetchnow-delivery` | `8000` internal only; host port нет | UID 10001 | read-only `tmp`; ждёт `storage-init` completed; `MEDIA_DELIVERY_*` only; не вызывает yt-dlp/ffmpeg |
| `worker` | тот же `fetchnow-api` image/Dockerfile | HTTP-порта нет | UID 10001 | healthcheck отключён; ждёт healthy `postgres` и `storage-init`; volume `tmp`; ffmpeg/ffprobe только здесь |
| `postgres` | `postgres:16.9-alpine`, registry image | `5432`; host port нет | не pinned в Compose; управляет official entrypoint | `pg_isready`; volume `pgdata` |

Все runtime services находятся в bridge network `fetchnow` (Docker name `{project}_fetchnow`). `storage-init` — исключение: `network_mode: none`, без Compose network и без `DATABASE_URL`. `gateway` ждёт healthy `api` и `web`; `api`/`worker` ждут healthy `postgres`. Это startup ordering, не гарантия дальнейшей доступности.


## Компоненты и restart

### gateway и web

Оба non-root (UID 10001), persistent state отсутствует. Restart запускает тот же container/image, recreate создаёт container заново. Gateway передаёт request ID и пишет access log в stdout. Upstream `api` и `web` резолвятся при загрузке конфига, поэтому их отсутствие не даст gateway стартовать; `delivery` резолвится per-request через Docker DNS, так что мёртвый delivery даёт 502 только на content route и не роняет фронтенд. Логи: `docker compose ... logs gateway web`.

### api

Image `fetchnow-api:<revision>` собирается из `backend/Dockerfile` (development `local`, staging full SHA); процесс non-root UID 10001. С PR7 api не монтирует volume `tmp`: приватные артефакты видят только `worker` (read-write) и `delivery` (read-only). Final stage несёт `org.opencontainers.image.revision`. Operator release builds (PRD1C2) materialize an exact `git archive` snapshot and build revision tags from that snapshot — not from a dirty worktree; see [глава 25](25-release-materialization-build.md). Внутри API process живут `URLValidator` и один pooled `SafeHTTPClient`: lifespan создаёт их вместе с DNS resolver, а при shutdown закрывает HTTP client и DB engine. API не запускает migrations при старте. Логи: service `api`; liveness не проверяет БД, readiness проверяет. См. [главу 24](24-release-preflight-health.md).

### worker

Тот же backend image и volume `tmp`, команда `fetchnow-worker`, non-root UID 10001. Worker выполняет durable media-inspection orchestration (PR5), download execution (PR6) и bounded muxing (PR9, default off). Runtime image installs Debian bookworm `ffmpeg` (provides `/usr/bin/ffmpeg` and `/usr/bin/ffprobe`). Paths are Compose-injected on worker only. Restart прерывает процесс через SIGTERM с bounded grace; cancel/lease loss не считаются успехом.

### storage-init

One-shot `fetchnow-storage-init` on the same image, `network_mode: none`, non-root UID 10001. Creates `/var/lib/fetchnow/tmp/downloads` with `ArtifactStore.validate_root()` if missing so delivery (read-only mount, no mkdir) does not crash-loop on an empty named volume. Never chmod/chown an existing operator root. No sleep. No `DATABASE_URL`, Postgres credentials, or tool paths. It does not load application Settings. Worker and delivery `depends_on` `service_completed_successfully` for local Compose up; release rollout uses `--no-deps` and therefore runs the oneshot explicitly before activating api/worker/delivery/web. Health does not require the oneshot to remain running.

### postgres

Официальный image хранит cluster в `pgdata:/var/lib/postgresql/data` (Docker name `{project}_pgdata`). Recreate контейнера сохраняет volume, удаление volume уничтожает БД. Логи: service `postgres`.

## Volumes

- `pgdata` → `{project}_pgdata` — persistent database state.
- `tmp` → `{project}_tmp` — общий временный storage: `storage-init` и `worker` монтируют read-write, `delivery` — read-only, `api` не монтирует вовсе (PR7). `storage-init` гарантирует `MEDIA_DOWNLOAD_TEMP_ROOT` до старта delivery.

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
