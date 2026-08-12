# Infrastructure Handbook FetchNow

Этот handbook — одновременно учебник для владельца проекта, runbook для планового staging и журнал инфраструктурных решений. Он объясняет не только команды, но и причины, проверки и безопасный откат. Фактическое развёртывание staging репозиторием не подтверждено: перед каждым изменением нужен preflight.

## Обозначения

- **INFO** — объяснение без действия.
- **CHECK** — безопасная проверка, обычно только чтение.
- **WARNING** — изменение системы или условие, требующее внимания.
- **DANGER** — риск простоя, потери данных либо ослабления защиты.
- **ROLLBACK** — способ безопасно отменить изменение.

Главное правило: сначала прочитать команду, затем понять каждый аргумент и только потом выполнять. После каждого шага проверяйте FetchNow и соседние сервисы; успешное завершение команды ещё не доказывает исправность системы.

## Целевая схема

```text
Internet
  -> DNS: staging.fetchnow.online -> 77.239.107.143
  -> host Nginx :443
      +-> staging.fetchnow.online -> 127.0.0.1:8091
      |    -> Docker gateway :8080
      |         +-> web :8080
      |         +-> api :8000
      |         +-> worker (HTTP-порта нет)
      |         `-> postgres :5432 (только Compose network)
      +-> CryptoBot -> 127.0.0.1:8000
      `-> JustTwo API -> 127.0.0.1:8080
```

Это целевая/плановая staging-архитектура, а не утверждение о текущем состоянии сервера. Docker не должен занимать публичные `80/443` или соседние `8000/8080`. Для staging требуется project name `fetchnow-staging`, root `/srv/fetchnow-staging`, пользователь `cryptobot` и loopback-публикация `127.0.0.1:8091`.

## Текущее состояние продукта после PR4

Реализованы URL validation (`POST /api/v1/media/validate`), диагностический safe outbound probe (`POST /api/v1/media/probe`), wrapper resolve (`POST /api/v1/media/resolve`, включая Yandex Preview), **внутренний** media inspection foundation (metadata-only VK/Rutube через hardened yt-dlp adapter), и durable media-inspection jobs (PR5: PostgreSQL queue, client access tokens; `MEDIA_JOBS_ENABLED=false` / `MEDIA_INSPECTION_ENABLED=false` по умолчанию). API не запускает yt-dlp inline. Media bytes не скачиваются для delivery. Staging enablement inspection/jobs path не выполнен.

Пока не реализованы media download pipeline, ffmpeg, direct-download tickets, processed jobs, PostgreSQL job queue, реальная обработка worker jobs, anonymous sessions, Turbo/payments/recovery links, runtime file lifecycle/cleanup worker, rate limiting, host Nginx/TLS staging publish, automatic deploy/rollback, отдельный StorageProvider или S3 implementation.

**PRD1A (Compose contract):** staging file set + project-name volume isolation реализованы в репозитории.

**PRD1B (PostgreSQL backup):** logical dump / restore-verify / retention tooling реализованы в репозитории. См. [главу 15](15-backups-and-restore.md).

**PRD1C1 (release identity / preflight / health):** immutable `FETCHNOW_RELEASE_REVISION`, revision-tagged images + OCI labels, read-only staging preflight и Compose+HTTP health gate реализованы в репозитории. Staging revision must already be contained in local `refs/remotes/origin/main` (feature-branch descendants rejected; preflight does not fetch). См. [главу 24](24-release-preflight-health.md).

**PRD1C2 (materialize + image build):** exact `git archive` snapshot under explicit deploy root, application image build, immutable `release.json`, atomic publish. См. [главу 25](25-release-materialization-build.md).

**PRD1C3A (deployment transaction):** application-only activation from immutable image IDs, stabilization, journaled rollback and explicit recover are implemented in the repository; no database mutation occurs in this path. См. [главу 26](26-deployment-transaction-rollback.md). This does not claim staging is deployed.

**PRD1C3B2B1 (backup verification foundation):** exact-head restore verification, attestation schema v2, and one-lock backup-root session API are implemented; no production migration execution or deployment-state mutation. См. [главу 15](15-backups-and-restore.md).

**PRD1C3B1 (migration compatibility + deploy-plan):** strict `deploy/migrations/compatibility.json` contract and read-only `deploy-plan` command are implemented in the repository; no backup, migration, or deployment mutation occurs in this path. См. [главу 27](27-migration-compatibility-deployment-planning.md).

**PRD1C3B2A (dual application/database state):** `current.json` schema v2 separates application identity from database schema identity and enforces an explicit compatibility envelope across rollout/recover/already-active and deploy-plan. No database mutation. См. [главу 28](28-dual-application-database-state.md).

**PRD1C3B2B2 (verified migration transaction):** journaled forward migration under rollout lock with B2B1 backup proof, retention holds, Alembic from target release image, atomic database-side `current.json` commit, and explicit recovery — without application activation. См. [главу 29](29-verified-migration-transaction.md).

Архитектурные основания URL/DNS и outbound-защиты: [ADR 0004](../adr/0004-provider-registry-and-dns-validation.md) и [ADR 0005](../adr/0005-safe-outbound-http-and-redirects.md).

## Как читать

Сначала главы 01–06, затем 07–10 для устройства FetchNow, после них 11–16 для операций. Главы 17–20 посвящены эксплуатации и переносу, 21–23 — справке и обучению.

### Быстрый маршрут для первого deployment

[Основы сервера](01-server-basics.md) → [сеть](03-networking-and-ports.md) → [Compose](06-docker-compose.md) → [контейнеры FetchNow](07-fetch-now-containers.md) → [release preflight/health](24-release-preflight-health.md) → [первое развёртывание](11-first-staging-deployment.md) → [healthchecks](13-healthchecks-and-smoke-tests.md) → [backup](15-backups-and-restore.md).

### Маршрут для диагностики

[Release health gate](24-release-preflight-health.md) → [Healthchecks](13-healthchecks-and-smoke-tests.md) → [логи и диагностика](14-logs-and-diagnostics.md) → [ёмкость](17-capacity-and-disk-management.md) → [инциденты](19-incident-response.md).

### Маршрут для миграции

[Backup/restore](15-backups-and-restore.md) → [rollback](16-rollback.md) → [capacity](17-capacity-and-disk-management.md) → [миграция сервера](20-server-migration.md).

## Все главы

1. [Основы сервера](01-server-basics.md)
2. [Файловая система и пользователи](02-linux-filesystem-and-users.md)
3. [Сеть и порты](03-networking-and-ports.md)
4. [DNS](04-dns.md)
5. [Основы Docker](05-docker-basics.md)
6. [Docker Compose](06-docker-compose.md)
7. [Контейнеры FetchNow](07-fetch-now-containers.md)
8. [PostgreSQL](08-postgresql.md)
9. [Host Nginx](09-host-nginx.md)
10. [TLS и Certbot](10-tls-and-certbot.md)
11. [Первое staging-развёртывание](11-first-staging-deployment.md)
12. [Обычное развёртывание](12-routine-deployment.md)
13. [Healthchecks и smoke tests](13-healthchecks-and-smoke-tests.md)
14. [Логи и диагностика](14-logs-and-diagnostics.md)
15. [Backup и restore](15-backups-and-restore.md)
16. [Rollback](16-rollback.md)
17. [Ёмкость и диск](17-capacity-and-disk-management.md)
18. [Базовая безопасность](18-security-baseline.md)
19. [Реакция на инциденты](19-incident-response.md)
20. [Миграция сервера](20-server-migration.md)
21. [Справочник команд](21-command-reference.md)
22. [Учебные упражнения](22-learning-exercises.md)
23. [Глоссарий](23-glossary.md)
24. [Release identity, preflight & health (PRD1C1)](24-release-preflight-health.md)
25. [Release materialization & image build (PRD1C2)](25-release-materialization-build.md)
26. [Deployment transaction & rollback (PRD1C3A)](26-deployment-transaction-rollback.md)
27. [Migration compatibility & deploy-plan (PRD1C3B1)](27-migration-compatibility-deployment-planning.md)
28. [Dual application/database state (PRD1C3B2A)](28-dual-application-database-state.md)
29. [Verified migration transaction (PRD1C3B2B2)](29-verified-migration-transaction.md)
