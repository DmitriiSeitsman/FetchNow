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

## Текущее состояние продукта после PR2

Реализованы URL validation (`POST /api/v1/media/validate`) и диагностический safe outbound probe (`POST /api/v1/media/probe`). Probe выполняет только контролируемый HEAD либо ограниченный GET и возвращает безопасные HTTP metadata; это не скачивание и не извлечение media metadata.

Пока не реализованы yt-dlp, ffmpeg, media download pipeline, direct-download tickets, processed jobs, PostgreSQL job queue, реальная обработка worker, anonymous sessions, Turbo/payments/recovery links, runtime file lifecycle/cleanup worker, rate limiting, host Nginx/TLS staging publish, automatic deploy/rollback, отдельный StorageProvider или S3 implementation.

**PRD1A (Compose contract):** staging file set + project-name volume isolation реализованы в репозитории.

**PRD1B (PostgreSQL backup):** logical dump / restore-verify / retention tooling реализованы в репозитории; host Nginx/TLS, off-host copy, scheduling и deploy automation остаются PRD1C–PRD1D. См. [главу 15](15-backups-and-restore.md).

Архитектурные основания URL/DNS и outbound-защиты: [ADR 0004](../adr/0004-provider-registry-and-dns-validation.md) и [ADR 0005](../adr/0005-safe-outbound-http-and-redirects.md).

## Как читать

Сначала главы 01–06, затем 07–10 для устройства FetchNow, после них 11–16 для операций. Главы 17–20 посвящены эксплуатации и переносу, 21–23 — справке и обучению.

### Быстрый маршрут для первого deployment

[Основы сервера](01-server-basics.md) → [сеть](03-networking-and-ports.md) → [Compose](06-docker-compose.md) → [контейнеры FetchNow](07-fetch-now-containers.md) → [первое развёртывание](11-first-staging-deployment.md) → [healthchecks](13-healthchecks-and-smoke-tests.md) → [backup](15-backups-and-restore.md).

### Маршрут для диагностики

[Healthchecks](13-healthchecks-and-smoke-tests.md) → [логи и диагностика](14-logs-and-diagnostics.md) → [ёмкость](17-capacity-and-disk-management.md) → [инциденты](19-incident-response.md).

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
