# 11. Первое staging-развёртывание

Это чек-лист будущей операции, а не свидетельство, что staging уже развёрнут. Каждый gate обязателен; команды выполняются на соответствующей машине только оператором.

## STOP conditions

Остановиться, если DNS указывает не на `77.239.107.143`; `nginx -t` invalid; свободно меньше `MIN_FREE_DISK_BYTES` (example 1 GiB, пока planned-only) или согласованного большего порога; CryptoBot/JustTwo не active; `8091` занят; rendered Compose публикует PostgreSQL/API; database/temp volume names совпадают с local или не являются staging-specific; secret env отсутствует или доступен group/other; нет проверенного backup/rollback пути.

## Этапы и gates

1. **Local tests.** `make lint && make typecheck && make test && make build && make check`. **Gate:** все применимые проверки green.
2. **Git status.** `git status --short`, `git rev-parse HEAD`. **Gate:** известный commit, нет неожиданных файлов.
3. **Server preflight.** Выполнить блок из [главы 01](01-server-basics.md). **Gate:** соседи active, Nginx valid, ресурсы достаточны.
4. **Docker check.** `docker version`, `docker compose version`, `docker info`. **Gate:** client/server доступны; установка Docker не входит в этот handbook-run.
5. **Directories.** Создать структуру из [главы 02](02-linux-filesystem-and-users.md). **Gate:** owner/modes проверены `stat`.
6. **Checkout/release.** Получить ровно reviewed commit в `/srv/fetchnow-staging/releases/<release-id>`, направить `app` по принятой схеме. **Gate:** `git rev-parse HEAD` совпадает с report.
7. **Staging env.** Создать вне Git, `GATEWAY_PORT=127.0.0.1:8091`, уникальный PostgreSQL password. **Gate:** `stat` mode 600/700; секреты не печатать.
8. **Compose config.** Явные `-p/-f`, как в главе 06. **Gate:** services `gateway/web/api/worker/postgres`; только loopback 8091 published; БД не published; volume names staging-specific. Текущий Compose проваливает последний gate, поэтому deployment блокирован до отдельного Compose-изменения.
9. **Image build.** `docker compose ... build`. **Gate:** build success, expected local images/release labels recorded.
10. **Database start.** `docker compose ... up -d postgres`. **Gate:** `postgres` healthy.
11. **Migrations.** Сначала backup (если БД не новая), `alembic current`, затем `upgrade head`. **Gate:** head revision и DB readiness.
12. **Stack start.** `docker compose ... up -d`. **Gate:** `ps` healthy, worker running.
13. **Localhost smoke.** Отдельно проверить `curl -fsS http://127.0.0.1:8091/api/v1/health/live`, `/api/v1/health/ready` и `/`. **Gate:** 200.
14. **Host Nginx.** Добавить staging server block. **Gate:** `nginx -t`, reload, HTTP local/vhost test, neighbours still healthy.
15. **TLS.** Certbot по [главе 10](10-tls-and-certbot.md). **Gate:** valid chain/hostname and renewal timer.
16. **Public smoke.** HTTPS `/`, live, ready и отдельные validate/probe checks из главы 13. **Gate:** core endpoints 200 без отключения TLS validation; внешний probe оценивается отдельно и не делает liveness failed.
17. **Neighbour checks.** Status units и их документированные local/public probes. **Gate:** без регрессий.
18. **Persistence restart.** Записать DB marker безопасным test-процессом, restart/recreate по согласованному сценарию, сверить marker. **Gate:** данные сохранены; не использовать production records.
19. **Resources.** `docker stats --no-stream`, `docker system df`, `free -h`, `df -hT`. **Gate:** запас выше thresholds.
20. **Backup.** Сделать timestamped logical dump и проверить его структуру/restore в изолированной DB. **Gate:** off-server copy запланирована/подтверждена.
21. **Report.** Commit, config hash без secrets, migrations, timestamps, smoke, neighbours, backup, известные отклонения. **Gate:** другой человек может повторить/откатить.

## Безопасная отмена

До DNS/Nginx включения достаточно остановить только project без `-v`. После публикации вернуть прежний Nginx site, test/reload, затем остановить FetchNow. После migration приложение откатывается только при schema compatibility; иначе restore требует отдельного решения.

**DANGER:** не продолжать через failed gate, не импровизировать с `chmod 777`, `down -v`, firewall/TLS bypass или портами соседей.
