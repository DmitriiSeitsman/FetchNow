# 21. Справочник команд

Risk: low — чтение; medium — контролируемое изменение; high — возможен простой/данные. `…` означает явные `--env-file .env.staging --project-name fetchnow-staging -f compose.yaml -f compose.staging.yaml`.

| Категория | Команда | Назначение | Режим | sudo | Risk / ожидаемый вывод |
|---|---|---|---|---|---|
| System | `date -u` | UTC time | read-only | нет | low; timestamp |
| System | `hostnamectl` | host/OS | read-only | нет | low; Ubuntu/hostname |
| System | `uptime` | load/uptime | read-only | нет | low; load averages |
| System | `free -h` | RAM/swap | read-only | нет | low; memory table |
| System | `systemctl --failed` | failed units | read-only | нет | low; ideally 0 |
| Network | `sudo ss -lntup` | listeners/processes | read-only | да | low; ports/PIDs |
| Network | `ip route` | routing | read-only | нет | low; default route |
| Network | `sudo ufw status verbose` | firewall | read-only | да | low; rules/status |
| Network | `curl -fsS http://127.0.0.1:8091/...` | local HTTP | read-only | нет | low; body or nonzero exit |
| DNS | `dig staging.fetchnow.online A +short` | A record | read-only | нет | low; `77.239.107.143` |
| DNS | `dig fetchnow.online NS +short` | authoritative NS | read-only | нет | low; nameservers |
| DNS | `getent ahosts NAME` | system resolution | read-only | нет | low; resolved addresses |
| Nginx | `sudo nginx -t` | validate config | read-only | да | low; successful test |
| Nginx | `sudo systemctl reload nginx` | apply valid config | modifying | да | medium; usually silent |
| Nginx | `systemctl status nginx --no-pager` | state | read-only | нет | low; active/running |
| Nginx | `sudo journalctl -u nginx --since "10 minutes ago"` | recent events | read-only | да | low; journal lines |
| TLS | `sudo certbot certificates` | cert inventory | read-only | да | low; names/expiry |
| TLS | `sudo certbot renew --dry-run` | renewal test | external/test | да | medium; simulated success |
| Docker | `docker version` | client/daemon versions | read-only | нет* | low; two version blocks |
| Docker | `docker ps -a` | containers | read-only | нет* | low; states |
| Docker | `docker images` | images | read-only | нет* | low; image table |
| Docker | `docker volume ls` | volumes | read-only | нет* | low; names/drivers |
| Docker | `docker logs --tail 200 CONTAINER` | stdout/stderr | read-only | нет* | low; log lines |
| Docker | `docker inspect CONTAINER` | runtime config | read-only | нет* | low; JSON, may expose env |
| Docker | `docker stats --no-stream` | resources | read-only | нет* | low; CPU/RAM/I/O |
| Docker | `docker system df` | disk accounting | read-only | нет* | low; size/reclaimable |
| Compose | `docker compose … config` | render/validate | read-only | нет* | low; YAML, may expose secrets |
| Compose | `docker compose … build` | build images | modifying | нет* | medium; build log |
| Compose | `docker compose … up -d` | create/recreate/start | modifying | нет* | medium; service actions |
| Compose | `docker compose … ps` | project state | read-only | нет* | low; state/health/ports |
| Compose | `docker compose … logs --tail 200` | project logs | read-only | нет* | low; log lines |
| Compose | `docker compose … restart SERVICE` | same-container restart | modifying | нет* | medium; restart status |
| PostgreSQL | `docker compose … exec postgres pg_isready` | DB readiness | read-only | нет* | low; accepting connections |
| PostgreSQL | `docker compose … run --rm api alembic current` | schema revision | read-only† | нет* | low; revision |
| PostgreSQL | `docker compose … run --rm api alembic upgrade head` | apply migrations | modifying | нет* | high; migration log |
| Git | `git status --short` | working tree | read-only | нет | low; changed paths |
| Git | `git rev-parse HEAD` | exact commit | read-only | нет | low; SHA |
| Git | `git log -1 --oneline` | release summary | read-only | нет | low; commit line |
| Logs | `sudo tail -n 200 /var/log/nginx/error.log` | Nginx errors | read-only | да | low; recent lines |
| Health | `curl -fsS https://staging.fetchnow.online/api/v1/health/live` | public liveness | read-only | нет | low; status JSON |
| Health | `curl -fsS https://staging.fetchnow.online/api/v1/health/ready` | public readiness | read-only | нет | low; status JSON/503 |
| Health | `curl -X POST .../api/v1/media/validate --data '{"url":"<PUBLIC_VK_URL>"}'` | URL/provider/DNS policy smoke | read-only† | нет | low; provider/canonical или stable error |
| Health | `curl -X POST .../api/v1/media/probe --data '{"url":"<PUBLIC_VK_URL>"}'` | внешний HEAD/bounded GET probe | external request | нет | medium; safe HTTP metadata/error |
| Disk | `df -hT` | bytes/filesystems | read-only | нет | low; usage table |
| Disk | `df -ih` | inodes | read-only | нет | low; inode table |
| Disk | `sudo du -xhd1 PATH` | directory sizes | read-only/heavy | да | medium; sizes |
| Backup | `make pg-backup-create BACKUP_ROOT=...` | logical DB dump + manifest | modifying file | нет* | medium; backup id/sha |
| Backup | `make pg-backup-verify BACKUP_ROOT=... BACKUP_ID=...` | restore drill + attestation (v2 on pass with exact heads when `--expected-alembic-head` / `--migrations-versions-dir` supplied; CLI may infer heads from worktree migrations dir) | modifying temp DB | нет* | medium; typed passed/failed + attestation path/sha |
| Backup | `make pg-backup-list BACKUP_ROOT=...` | inventory | read-only | нет | low; backup rows |
| Backup | `make pg-backup-prune-dry BACKUP_ROOT=...` | retention dry-run | read-only | нет | low; keep/delete plan |
| Release | `make release-preflight EXPECTED_REVISION=<sha>` | staging deployment preflight | read-only | нет* | low; OK/FAIL messages (no secrets) |
| Release | `make release-health EXPECTED_REVISION=<sha>` | Compose+HTTP health gate | read-only | нет* | low; service/HTTP status |
| Release | `make release-ancestry-integration` | temp-Git trusted-main ancestry + positive preflight | modifying temp dirs only | нет* | low/medium; disposable Git only |
| Release | `make release-prepare EXPECTED_REVISION=<sha> ENV_FILE=... DEPLOY_ROOT=...` | materialize archive + build images | modifying deploy-root only | нет* | medium; publishes `releases/<sha>` |
| Release | `make release-verify EXPECTED_REVISION=<sha> DEPLOY_ROOT=...` | read-only release verify | read-only | нет* | low; OK/FAIL |
| Release | `make release-build-integration` | isolated materialize/build IT | temp dirs only | нет* | medium; no compose up |
| Release | `make release-rollout EXPECTED_REVISION=<sha> ENV_FILE=... DEPLOY_ROOT=... [BOOTSTRAP=1]` | application-only image-ID rollout | modifying app containers | нет* | high; stabilized commit or rollback |
| Release | `make release-recover DEPLOYMENT_ID=<uuid> ACTION=rollback\|accept-target ENV_FILE=... DEPLOY_ROOT=...` | explicit unresolved-journal recovery | modifying app containers | нет* | high; explicit terminal action |
| Release | `make release-rollout-integration` | isolated rollout/rollback IT | unique test project only | нет* | high; `fetchnow-rollout-test-*` only |
| Release | `make release-deploy-plan EXPECTED_REVISION=<sha> DEPLOY_ROOT=...` | read-only migration-aware deploy plan (dual-state aware; emits `application_rollout_required`) | read-only | нет* | low; canonical JSON plan on stdout |
| Release | `make release-deploy-plan-test` | migration contract + deploy-plan unit tests | read-only | нет | low; pytest |
| Release | `make release-deploy-plan-integration` | isolated deploy-plan IT | unique test project only | нет* | medium; `fetchnow-deploy-plan-test-*` only |
| Release | `make release-test` | preflight/health unit tests | read-only | нет | low; pytest |

| Release | `make release-health-integration` | isolated health integration | modifying test project | нет* | medium; unique `fetchnow-health-test-*` only |

`*` Пользователь должен иметь разрешение Docker; membership в `docker` group практически даёт root-level control. `†` One-shot container создаётся/удаляется, но database не меняется.

**DANGER:** команды удаления (`docker compose down -v`, `docker system prune`, volume/file/database removal) намеренно не включены. Если они когда-либо нужны, требуется отдельный target inventory, backup и approval.
