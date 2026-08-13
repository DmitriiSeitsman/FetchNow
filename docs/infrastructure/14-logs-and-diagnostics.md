# 14. Логи и диагностика

## Источники

- `docker compose ... logs SERVICE` — stdout/stderr gateway, web, API, worker, PostgreSQL.
- `/var/log/nginx/access.log`, `/var/log/nginx/error.log` и `journalctl -u nginx` — host Nginx (пути подтвердить config).
- Request ID (`X-Request-ID`) связывает gateway и API events; искать без source URL/token.
- `df`, `free`, `docker stats`, `ss` — disk, memory/CPU и sockets.

PR1/PR2 разрешают структурированные поля: request ID, provider ID, normalized hostname, HTTP method/status, redirect count, bytes read, duration и stable error code. Запрещены raw URL, query, fragment, credentials, полный `Location`, resolved IP, response body, cookies, `Authorization` и CDN/download URL. `httpx`/`httpcore` INFO подавлены, потому что могут печатать полный URL.

```bash
docker compose --env-file .env.staging --project-name fetchnow-staging \
  -f compose.yaml -f compose.staging.yaml ps
docker compose --env-file .env.staging --project-name fetchnow-staging \
  -f compose.yaml -f compose.staging.yaml logs --since 10m --tail 300 SERVICE
sudo journalctl -u nginx --since "10 minutes ago" --no-pager
sudo tail -n 200 /var/log/nginx/error.log
df -hT
df -ih
free -h
docker stats --no-stream
sudo ss -lntup
```

## Диагностическое дерево

Всегда идти от read-only проверок к минимальному изменению.

| Симптом | Сначала проверить | Затем | Возможное изменение после доказательства |
|---|---|---|---|
| Домен не открывается | `dig A`, `curl -v`, ports 80/443 | Nginx status/journal | DNS/firewall/vhost по change plan |
| TLS error | hostname, time, `certbot certificates` | SNI/chain/expiry | controlled renew/config rollback |
| 502 | `curl 127.0.0.1:8091`, gateway `ps/logs` | api/web health | recreate failed service |
| 504 | request ID, gateway/api timing | CPU/RAM/provider | устранить зависание, не просто увеличить timeout |
| API not ready | live vs ready | postgres health/logs/network | восстановить DB connectivity |
| Worker not running | `ps -a`, worker logs/exit code | memory/signal/config | recreate worker |
| PostgreSQL unhealthy | `pg_isready`, DB logs, disk | credentials/volume mount | repair only with backup |
| Disk full | `df -hT`, `df -ih`, `du`, `docker system df` | logs/temp/DB ownership | targeted cleanup under policy |
| Permission denied | `id`, `namei -l`, `stat` | mount/UID 10001 | exact owner/mode fix |
| DNS mismatch | authoritative and recursive `dig` | TTL/delegation | correct A record |

## Диагностика download job (PR10)

Искать structured events worker-а: `download_job_attempt_started`,
`download_video_*`, `download_audio_*`, `media_mux_*`, `media_verify_*`,
`artifact_publish_*`, `download_retry_scheduled`, `download_job_cancelled`,
`download_job_failed`, `download_job_ready`.

Оператор должен ответить без URL / token / provider format token / raw stderr:

| Вопрос | Где смотреть |
|---|---|
| На какой стадии упала задача? | последнее `*_failed` event + `stage` / public `progress_stage` |
| Ошибка retryable? | поле `retryable` на stage event |
| Сколько попыток? | `attempt_count` / public `attempt` vs `maxAttempts` |
| Задача отменена? | event `download_job_cancelled` или public API `state=cancelled` / `progress_stage=cancelled` + `cancel_requested_at` (stored `public_state` remains `expired`) |
| Потерян ли lease? | `failure_class=LEASE_LOST` или fence mismatch; не `DOWNLOAD_TOOL_FAILED` из-за cancel |

Запрещено печатать submitted/canonical URL, query, Bearer, format token, argv,
пути и исходный stderr. Fingerprint строится только из sanitized categorical
полей.

## Диагностика validate/probe

### `UNSUPPORTED_PROVIDER` от validate

Проверить normalized hostname, exact allowlist и флаги `PROVIDER_VK_ENABLED`/`PROVIDER_RUTUBE_ENABLED`. Учесть trailing dot и IDNA normalization. `vk.com.attacker.example` и `notvk.com` не VK: suffix matching не используется. Unknown hostname не должен доходить до DNS.

### `BLOCKED_DESTINATION` от validate

Проверить literal/nonstandard private IP, loopback/link-local, DNS empty result и mixed public/private A/AAAA. Resolver details и IP допустимы только во внутренней диагностике с ограниченным доступом; клиенту их не раскрывать.

### `SOURCE_UNAVAILABLE` от probe

Проверить внешний status, provider HEAD fallback policy, redirect policy, TLS, timeout, MIME allowlist и возможную datacenter bot protection. VK fallback — `418/405/501`, Rutube — `405/501`; статус fallback не является успехом, он только разрешает bounded GET. Не добавлять cookies/Authorization, не отключать TLS и не имитировать полный browser fingerprint.

### `BLOCKED_DESTINATION` от probe

Проверить redirect destination, private/mixed redirect DNS, `DNS_SET_MISMATCH`, смену provider и HTTPS downgrade. Полный `Location`, query и IP не помещать в report/log.

```bash
sudo du -xhd1 /var /srv 2>/dev/null
docker system df
docker compose --env-file .env.staging --project-name fetchnow-staging \
  -f compose.yaml -f compose.staging.yaml logs postgres --tail 200
```

`du` может быть I/O-heavy — запускать вне пика. Не публиковать raw logs: они могут содержать IP/user agent; source URLs, headers и secrets должны быть redacted согласно [logging policy](../security/logging-and-privacy.md).

## Безопасное восстановление

Перед restart сохранить timestamp, request ID, status, logs и config/reference. Менять только один слой и повторять проверки. **DANGER:** не удалять containers/volumes/logs в панике, не запускать prune и не повышать права до 777.
