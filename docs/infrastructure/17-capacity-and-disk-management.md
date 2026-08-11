# 17. Ёмкость, диск и runtime configuration

Disk capacity измеряет bytes, inode — количество filesystem objects; миллионы мелких файлов исчерпывают inodes раньше bytes. Docker layers/build cache, logs, PostgreSQL volume и `fetchnow_tmp` конкурируют за host disk. Swap временно вытесняет memory pages на disk, но постоянный swap pressure ухудшает latency.

## Фактические environment variables

Статус **runtime** означает, что значение читает текущий код/Compose. **Settings only** — Pydantic принимает значение, но feature ещё не применяет limit. **Planned** — есть только в example/policy. Не путайте example default с выбранным production значением.

| Группа | Variable | Default/example | Фактическое использование |
|---|---|---|---|
| Application | `APP_NAME` | `FetchNow` | runtime Settings/API |
| Application | `APP_ENV` | `production` Settings; example `development` | runtime, OpenAPI docs visibility |
| Application | `LOG_LEVEL` | `INFO` | runtime logging |
| Application | `API_HOST` | `0.0.0.0` | runtime внутри container |
| Application | `API_PORT` | `8000` | runtime API listener |
| Database | `DATABASE_URL` | local example на `localhost:5432` | runtime API/worker; secret-bearing |
| Database | `POSTGRES_DB` | `fetchnow` | Compose PostgreSQL/URL interpolation |
| Database | `POSTGRES_USER` | `fetchnow` | Compose PostgreSQL/URL interpolation |
| Database | `POSTGRES_PASSWORD` | development placeholder | Compose secret; production обязано заменить |
| Providers | `PROVIDER_VK_ENABLED` | `true` | runtime exact registry flag |
| Providers | `PROVIDER_RUTUBE_ENABLED` | `true` | runtime exact registry flag |
| URL | `URL_ALLOWED_SCHEMES` | `http,https` | runtime validation; другие schemes invalid config |
| URL | `URL_ALLOWED_PORTS` | `80,443` | runtime validation |
| URL | `URL_MAX_LENGTH` | `4096` | runtime validation/redirect limit; API payload max отдельно 8192 |
| URL | `URL_MAX_REDIRECTS` | `5` | runtime, максимум config 20 |
| DNS | `DNS_RESOLUTION_TIMEOUT_SECONDS` | `3` | runtime resolver timeout |
| Outbound | `OUTBOUND_CONNECT_TIMEOUT_SECONDS` | `5` | runtime HTTP connect/pool timeout |
| Outbound | `OUTBOUND_READ_TIMEOUT_SECONDS` | `10` | runtime read/write timeout |
| Outbound | `OUTBOUND_TOTAL_TIMEOUT_SECONDS` | `15` | runtime whole probe timeout |
| Outbound | `OUTBOUND_USER_AGENT` | `FetchNow/1.0` | runtime fixed server header |
| Response | `OUTBOUND_ALLOWED_CONTENT_TYPES` | `text/html,application/json,text/plain` | runtime MIME allowlist |
| Response | `OUTBOUND_MAX_RESPONSE_BYTES` | `1048576` | runtime hard response ceiling |
| Probe | `OUTBOUND_PROBE_BODY_BYTES` | `131072` | runtime soft bounded-GET stop |
| Wrapper | `WRAPPER_RESOLUTION_MAX_DEPTH` | `3` | max wrapper hops (1–8); foundation only |
| Worker/capacity | `WORKER_POLL_INTERVAL_SECONDS` | `5` | runtime heartbeat interval |
| Worker/capacity | `WORKER_CONCURRENCY` | `2` | Settings/logged only; media jobs absent |
| Worker/capacity | `API_CONCURRENCY_LIMIT` | `32` | Settings only; enforcement absent |
| Worker/capacity | `METADATA_CONCURRENCY` | `4` | planned; Settings не читает |
| Worker/capacity | `MAX_SOURCE_DURATION_SECONDS` | `3600` | planned media policy |
| Worker/capacity | `MAX_SOURCE_FILE_BYTES` | `536870912` | planned media policy |
| Worker/capacity | `MAX_OUTPUT_FILE_BYTES` | `536870912` | planned media policy |
| Storage | `TEMP_STORAGE_PATH` | `/var/lib/fetchnow/tmp` | Settings/Compose mount; pipeline absent |
| Storage | `TEMP_STORAGE_MAX_BYTES` | `10737418240` | Settings only; enforcement absent |
| Storage | `TEMP_STORAGE_SOFT_LIMIT_BYTES` | `8589934592` | planned; Settings не читает |
| Storage | `MIN_FREE_DISK_BYTES` | `1073741824` | planned; Settings не читает |
| TTL/storage | `JOB_ABSOLUTE_TTL_SECONDS` | `3600` | planned; jobs отсутствуют |
| TTL/storage | `READY_FILE_TTL_SECONDS` | `900` | planned; delivery отсутствует |
| Application | `ABUSE_CONTACT_EMAIL` | пусто | planned; Settings не читает |
| Compose | `GATEWAY_PORT` | `80` | Compose interpolation; staging target `127.0.0.1:8091` |
| Web build | `PUBLIC_SITE_URL` | local example | Astro build argument, не backend runtime |

## Диагностика ресурсов

```bash
df -hT
df -ih
sudo du -xhd1 /var/lib/docker /srv/fetchnow-staging 2>/dev/null
docker system df
docker stats --no-stream
free -h
```

`du` — потенциально тяжёлая read-only операция. Сравнивайте тренд и filesystem, где расположен Docker data root (`docker info`). CPU/RAM pressure проявляется throttling/OOM/restarts; увеличивать concurrency при этом нельзя.

## Действия и ограничения

Soft/hard capacity enforcement, TTL cleanup и media job admission пока не реализованы. При реальном low-disk сначала прекратить новые writes доступным операционным способом, определить владельца объёма, применить targeted log/TTL cleanup по фактической политике либо добавить capacity. PostgreSQL cleanup требует DB-aware процедур.

**ROLLBACK:** вернуть прошлые env values и recreate затронутые services после `config`/smoke. **DANGER:** никакого автоматического `docker system prune`, ручного удаления `/var/lib/docker`, PostgreSQL files или ещё используемых artifacts.
