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
| Providers | `PROVIDER_OK_ENABLED` | `true` | runtime exact registry flag |
| Providers | `PROVIDER_DZEN_ENABLED` | `true` | runtime exact registry flag |
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
| Worker/capacity | `MAX_SOURCE_DURATION_SECONDS` | `7200` | planned media policy |
| Worker/capacity | `MAX_SOURCE_FILE_BYTES` | `3221225472` | planned media policy |
| Worker/capacity | `MAX_OUTPUT_FILE_BYTES` | `3221225472` | planned media policy |
| Storage | `TEMP_STORAGE_PATH` | `/var/lib/fetchnow/tmp` | Settings/Compose mount; pipeline absent |
| Storage | `TEMP_STORAGE_MAX_BYTES` | `10737418240` | Settings only; enforcement absent |
| Storage | `TEMP_STORAGE_SOFT_LIMIT_BYTES` | `8589934592` | planned; Settings не читает |
| Storage | `MIN_FREE_DISK_BYTES` | `1073741824` | planned; Settings не читает |
| TTL/storage | `JOB_ABSOLUTE_TTL_SECONDS` | `3600` | planned; jobs отсутствуют |
| TTL/storage | `READY_FILE_TTL_SECONDS` | `900` | planned; delivery отсутствует |
| Application | `ABUSE_CONTACT_EMAIL` | пусто | planned; Settings не читает |
| Compose | `GATEWAY_PORT` | `80` | Compose interpolation; staging target `127.0.0.1:8091` |
| Web build | `PUBLIC_SITE_URL` | local example | Astro build argument, не backend runtime |
| Web build | `PUBLIC_SEARCH_INDEXING_ENABLED` | `false` | Fail-closed robots meta + web nginx `X-Robots-Tag`; staging hard-coded `false` |

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

## Media artifact GC (worker)

Physical cleanup of private download artifacts under `MEDIA_DOWNLOAD_TEMP_ROOT`
(`/var/lib/fetchnow/tmp/downloads` by default) is performed **only by the worker**
hygiene loop (`expire_due_jobs` → `delete_artifact` → `ArtifactStore.reconcile`).

Facts for operators:

- After artifact/job TTL, DB artifact pointers are cleared **before** filesystem
  delete is attempted. Delivery cannot reopen an expired artifact even if the
  file briefly remains on disk.
- A transient `delete_artifact` failure does **not** leave the file forever:
  the directory becomes an unreferenced publication and later `reconcile`
  cycles retry removal after orphan grace (`MEDIA_DOWNLOAD_ORPHAN_GRACE_SECONDS`,
  floor 60s).
- If the worker is down, **delivery already stops at expiry** (no DB pointer /
  grant validity). Physical files may linger until the worker recovers; on the
  next successful hygiene polls, reconcile deletes them.
- Do **not** use ad-hoc `rm` on the volume as a routine flow. Prefer restoring
  the worker and waiting for hygiene, or investigating structured logs first.

### Observability (structured log events)

Worker / artifact logs (no URLs, cookies, Bearer, or media bytes):

| Event | Meaning |
|---|---|
| `artifact_delete_failed` | Physical delete left the dir (expire or reconcile) |
| `artifact_delete_retry` | Reconcile retrying an unreferenced publication |
| `artifact_delete_succeeded_after_retry` | Reconcile removed a previously leftover dir |
| `orphan_artifact_detected` | Unreferenced published dir selected for removal |
| `orphan_artifact_deleted` | Orphan publication removed |
| `download_orphan_sweep_removed count=N` | Bounded reconcile batch size |

### Checking leftover publications (read-only)

```bash
# Count published artifact directories on the staging/production host volume
# (path via MEDIA_DOWNLOAD_TEMP_ROOT; default shown).
docker compose --env-file /srv/fetchnow-staging/env/.env.staging \
  --project-name fetchnow-staging \
  -f compose.yaml -f compose.staging.yaml \
  exec -T worker sh -c 'find /var/lib/fetchnow/tmp/downloads/published -mindepth 1 -maxdepth 1 -type d ! -name ".partial_*" | wc -l'

# Confirm worker is running and emitting hygiene / orphan events
docker compose ... logs --since 30m worker 2>&1 | grep -E 'artifact_delete_|orphan_artifact_|download_orphan_sweep'
```

After worker recovery, wait at least one poll interval plus orphan grace before
expecting counts to drop. Manual filesystem surgery remains an emergency-only
last resort after confirming no active referenced artifacts in DB.

## Действия и ограничения

Soft/hard capacity enforcement, TTL cleanup и media job admission пока не реализованы. При реальном low-disk сначала прекратить новые writes доступным операционным способом, определить владельца объёма, применить targeted log/TTL cleanup по фактической политике либо добавить capacity. PostgreSQL cleanup требует DB-aware процедур.

**ROLLBACK:** вернуть прошлые env values и recreate затронутые services после `config`/smoke. **DANGER:** никакого автоматического `docker system prune`, ручного удаления `/var/lib/docker`, PostgreSQL files или ещё используемых artifacts.
