# 20. Миграция на новый сервер

Приложение переписывать не нужно: контейнеры находят друг друга через Compose DNS, а host/IP, paths, provider flags, URL/outbound limits и concurrency задаёт infrastructure/env. URLValidator/SafeHTTPClient не зависят от IP сервера; после переноса заново проверяются outbound DNS/TLS/provider behavior. Меняются capacity values, Nginx/DNS/TLS и persistent state.

## План

1. За несколько TTL заранее снизить DNS TTL; записать исходные A/AAAA/TTL.
2. Подготовить новый Ubuntu host, users, firewall, time sync, packages; выполнить security/preflight.
3. Проверить Docker/Compose; не устанавливать из непроверенного convenience script.
4. Развернуть тот же reviewed checkout/images и exact release metadata.
5. Передать env/secrets защищённым каналом, mode 600; при возможности rotate.
6. Создать DB backup, checksum, off-server copy; restore на новом container PostgreSQL и проверить Alembic/data.
7. Перенести storage. Для текущего `fetchnow_tmp` сначала определить, нужны ли какие-либо активные ephemeral artifacts; не переносить мусор как durable state.
8. Запустить новый stack на loopback, smoke через локальный Host header/temporary operator mapping без отключения TLS validation.
9. Freeze/остановить новые writes на старом сервисе, выполнить final backup и delta/storage sync.
10. Production switch: Nginx/TLS готовы, изменить DNS A, наблюдать оба hosts в rollback window.
11. Старый server drain: обслужить established work, не принимать новое; сохранить возможность быстрого DNS rollback.
12. После TTL + observation сделать final archival backup, revoke old secrets/access.
13. Decommission только после письменного gate; secure erase/provider deletion — отдельная подтверждаемая операция.

## Storage варианты

Сейчас первый вариант — local SSD и named volume: переносится database dump и согласованный storage payload. ADR описывает будущий `StorageProvider` для S3-compatible object storage, но interface/implementation в локальном `main` ещё нет. S3-вариант потребует credentials, bucket policy, encryption, lifecycle/egress и consistency review; нельзя объявлять его готовым.

## Проверка и rollback

До cutover сравнить commit, config shape, DB revision/counts, root/live/ready, worker, resources и neighbours. После cutover проверять authoritative и recursive DNS, TLS и request routing.

**ROLLBACK:** пока старый host цел и данные совместимы, вернуть A record и unfreeze прежний endpoint. Учесть writes, сделанные на новом host: два writable primaries создают split-brain. Поэтому владелец записи должен быть один. **DANGER:** не выключать старый сервер сразу, не менять MX/TXT и не переносить private keys через Git.
