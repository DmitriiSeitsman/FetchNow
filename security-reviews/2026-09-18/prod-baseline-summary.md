# SEC-00 production baseline summary (sanitized)

Captured UTC 2026-09-19 via read-only SSH + temporary Docker ACL. No production mutations.

## Authorities

- App revision / checkout / images: `14aa485e180f8da64c061fb47a250bc4cf6297a5`, deployment `be9acd6c-…`
- DB schema release revision / runtime-config revision: `b611479…` (drift vs app)
- Alembic head: `0010_successful_delivery_quota`
- Compose (app): release source `14aa485…` + images override for deployment `be9acd6c-…`
- Postgres container still references compose files from release `21df45d0…`

## Edge

- TLS: host Nginx 1.28.3; HTTP 301→HTTPS; **no HSTS**
- Gateway: loopback `127.0.0.1:8091` only
- No FetchNow `limit_req`/`limit_conn`/`real_ip` in host vhost extract
- Co-hosted unrelated sites on same Nginx
- `ROBOKASSA_MODE=test`, test checkout visible, SEO indexing disabled

## Limits

- CPU/memory cgroup limits applied (api/worker/delivery 0.5 CPU / 384MiB; web/gateway 0.25 / 64MiB; postgres 0.5 / 512MiB)
- No pids limit; no CapDrop/no-new-privileges; rootfs writable
- App containers uid 10001; postgres uid 0
- No docker.sock mounts

## Capacity snapshot (idle)

- Disk 26% used; tmp ~16K; pgdata ~64M
- Jobs/downloads all expired; no active leases/grants
- 62 anonymous clients (0 in 24h); 13 test paid orders; 0 active entitlements

## DB roles

- Single login role (`runtime_app_role`) with superuser and related attributes; owns all public tables; no API/worker/delivery/migration/backup separation

## Backup

- Latest passed restore-verify: 2026-09-14 against head **0009**
- Live head **0010** lacks successful restore-verify → current-head recoverability UNKNOWN

## ACL

Restored to owner/group only; unprivileged `docker ps` denied after each block and at end.
