# SEC-00 — Evidence matrix, verification plan, discrepancies

Date (UTC): 2026-09-19. Object: `https://fetchnow.online` and the SEC-00 documentation worktree.

**SEC-00 result:** documentation / evidence baseline only. This package does **not** remediate vulnerabilities. Production mutations during collection: **NONE**.

Raw sanitized capture notes (operator-local, not for public paste of secrets): `security-reviews/2026-09-18/prod-baseline/`.

## 1. Revision and worktree identity

| Item | Value | Class |
|------|-------|-------|
| SEC-00 branch | `codex/security-sec-00-baseline-evidence` | local |
| HEAD / baseline | `14aa485e180f8da64c061fb47a250bc4cf6297a5` (= `origin/main`) | local |
| Production checkout HEAD | `14aa485e180f8da64c061fb47a250bc4cf6297a5` | **production-verified** |
| Production checkout dirty | 0 | **production-verified** |
| `current.json` app.revision | `14aa485e180f8da64c061fb47a250bc4cf6297a5` | **production-verified** |
| `current.json` deployment_id | `be9acd6c-a66a-42b1-937c-9f5f4c2985b3` | **production-verified** |
| Release manifest SHA match | yes (`6e0a3c96…`) | **production-verified** |
| Live image IDs vs current | api/web/gateway/worker prefixes match current | **production-verified** |
| Compose authority (app services) | release `…/releases/14aa485…/source/{compose.yaml,compose.production.yaml}` + deployment images override | **production-verified** |
| Postgres compose working_dir | older release `21df45d0…/source` (DB not recreated on app rollout) | **production-verified** |

Parallel unfinished work (**not in this PR**): separate worktree branch `feat/reliability-a-gateway-routing` with dirty Reliability A changes. Backup archive exists at `.worktree-backups/reliability-a-pre-sec00-20260919T094726Z.tar.gz` (sha256 `211bb382…`) — do not unpack/commit here.

## 2. Public / host edge

| Check | Result | Class |
|-------|--------|-------|
| Host TLS terminator | Ubuntu host Nginx `1.28.3`; listens `:443`/`:80` on all interfaces | **production-verified** |
| Gateway publish | `127.0.0.1:8091->8080` only | **production-verified** |
| HTTP→HTTPS | `:80` → `301 https://fetchnow.online/` | **production-verified** |
| HSTS | **absent** on HTTPS responses | **production-verified** (FN-06) |
| CDN | no `Via`/`CF-*`/`X-Cache` on sampled responses | public + host |
| Host `limit_req` / `limit_conn` / `real_ip` for FetchNow vhost | **not present** in extracted nginx config | **production-verified** |
| External firewall/WAF rate limit | **unknown** (not inspected) | unknown |
| Co-tenancy | same host Nginx also serves other enabled sites (non-FetchNow) | **production-verified** |
| Diagnostic paths | `/metrics`, `/nginx_status`, startup → 404 publicly | public |
| Test checkout | `testCheckoutAvailable: true` | **production-verified** |
| SEO | `PUBLIC_SEARCH_INDEXING_ENABLED=false`; `X-Robots-Tag: noindex,nofollow` | **production-verified** |
| Robokassa | `ROBOKASSA_MODE=test`; `PREMIUM_TEST_CHECKOUT_VISIBLE=true` | **production-verified** (env keys only) |

## 3. Runtime-config authority drift (confirmed discrepancy)

| Field | Application (`current.json`) | Runtime-config |
|-------|------------------------------|----------------|
| revision | `14aa485…` (live app) | `b611479…` |
| deployment_id | `be9acd6c-…` | `1cffd714-…` |
| updated_at_utc | 2026-09-16T19:41:11Z | 2026-09-15T17:37:24Z |

Also: `database.schema_release_revision = b611479…` (matches runtime-config revision, **not** live app revision). Live app is inside `compatible_application_revisions` (23 entries).

**Impact:** config-rollout / runtime-config tooling still binds identity to a prior revision/deployment while application images already advanced. Health/release authority for images is `current.json`; runtime flags are served from drifted runtime-config. Operators can mis-target config rollouts.

**Future remediation scope (do not implement in SEC-00):** canonical runtime-config re-bind / config-rollout after application commit so `revision`+`deployment_id` match `current.json` application; document fail-closed checks in preflight. Candidate follow-up PR label: **SEC-RC / config-authority drift** (separate from SEC-01–12 unless folded into release hardening).

## 4. Resource / security limits (containers)

All FetchNow project containers: `Privileged=false`, no Docker socket mounts, `PublishAllPorts=false`, log driver `local` `max-size=20m` `max-file=5`, restart `unless-stopped`.

| Service | CPU (NanoCpus) | Memory | User | Health | Notes |
|---------|----------------|--------|------|--------|-------|
| api | 0.50 | 384 MiB | `fetchnow` (uid 10001) | healthy | ~6 procs |
| worker | 0.50 | 384 MiB | uid 10001 | none | tmp volume RW; concurrency 2 |
| delivery | 0.50 | 384 MiB | uid 10001 | healthy | tmp volume RO |
| web | 0.25 | 64 MiB | uid 10001 | healthy | |
| gateway | 0.25 | 64 MiB | uid 10001 | healthy | |
| postgres | 0.50 | 512 MiB | image default **uid 0** | healthy | pgdata RW; not published on host |

**Gaps (new, production-verified):**

- `PidsLimit` unset on all services.
- `CapDrop` / `SecurityOpt` (incl. `no-new-privileges`) unset.
- `ReadonlyRootfs=false` on all services.
- Single DB role (below) runs as superuser; postgres container process uid 0.

CPU/memory cgroup limits **are applied** (not YAML-only fiction).

## 5. Capacity snapshot (point-in-time)

| Metric | Value |
|--------|-------|
| Root disk | 150G, 26% used, 107G free; inodes 3% |
| `pgdata` volume | ~64M |
| `tmp` volume | ~16K (downloads/media_inspection nearly empty) |
| `media_jobs` | 118 rows, all `expired`; oldest ~519h; active leases 0; exhausted retries 5 |
| `media_download_jobs` | 67 rows, all `expired`; active leases 0; exhausted retries 4 |
| `media_delivery_grants` | 0 |
| `anonymous_clients` | 62 total; **0 created in last 24h** |
| `payment_orders` | paid 13 (all test), pending 5 (all test) |
| `premium_entitlements` | 13 total; **0 active** (all expired) |
| Worker | `WORKER_CONCURRENCY=2`, `MEDIA_DOWNLOAD_CONCURRENCY=1` |
| API | `API_CONCURRENCY_LIMIT=32` **still unused in codepaths** (confirmed earlier in sources) |

**Budget guidance:** this snapshot is **idle**. It validates emptiness and storage headroom but is **insufficient alone** to lock SEC-04/05/06 numeric limits. Need isolated load calibration (and optionally a second live sample under traffic). Candidate starts remain those in §7 of the prior draft; mark as **calibration-required**, not measured maxima.

## 6. PostgreSQL roles

| Fact | Result | Class |
|------|--------|-------|
| Host publish | postgres container port only; **not** published on host interfaces | **production-verified** |
| Application roles | **one** login role (label: `runtime_app_role`) | **production-verified** |
| Capabilities | superuser/createdb/createrole/replication/bypassrls all true | **production-verified** |
| Table ownership | all 9 public tables owned by `runtime_app_role` | **production-verified** |
| API/worker/delivery/migration/backup role separation | **absent** | **production-verified** gap |
| Live Alembic head | `0010_successful_delivery_quota` | **production-verified** |

Passwords/DSN not collected.

**Future PR scope:** least-privilege role split (migrator vs runtime vs backup) — new item beyond original FN list.

## 7. Backup / restore evidence

| Fact | Result |
|------|--------|
| Backup root | present; 7 backup dirs + `holds`; same 150G filesystem |
| Latest **passed** restore-verify | `20260914T170609Z_b6114796517f` @ 2026-09-14T17:06:12Z, attested heads **`0009_premium_entitlements`** |
| Live DB head | **`0010_successful_delivery_quota`** |
| Passed verify matches live head | **NO** |
| Attempt at 0010 | failed verify on `20260913T210741Z_…` (expected 0010) before later 0009-passed verify |
| Incomplete ops | none obvious beyond normal `.lock` |
| Canonical procedure | documented in repo (`make production-pg-backup-verify` / ch.15/21) — tooling present in checkout |

**Recoverability claim:** dumps exist and older heads have passed isolated restore-verify, but **current live head 0010 has no successful restore-verify attestation**. Status: **UNKNOWN for current-head recoverability**. Safe acceptance step (owner-commanded later): run verify against a current-head backup in disposable DB — **not** done in SEC-00.

## 8. Finding status matrix

| ID | Status | Sources | Isolated | Production |
|----|--------|---------|----------|------------|
| FN-01 | open | yes | no | idle queues; no admission budget still |
| FN-02 | **temporarily retained by owner** | yes | no | test mode + visible checkout + test paid orders confirmed |
| FN-03 | open | yes | no | 62 clients; 0/24h — abuse not observed now |
| FN-04 | open | yes | no | worker still on shared bridge with postgres |
| FN-05 | open | yes | no | not re-proven hang this run |
| FN-06 | open | yes | n/a | HSTS still absent on host responses |
| Role superuser monolith | **new confirmed** | n/a | n/a | production-verified |
| Runtime-config drift | **new confirmed** | n/a | n/a | production-verified |
| Backup verify lag vs head 0010 | **new confirmed** | n/a | n/a | production-verified |
| Missing pids/no-new-priv/ro-rootfs | **new confirmed** | n/a | n/a | production-verified |
| npm Astro/Vitest | open → SEC-02/03 | lockfile | n/a | static site |

## 9. Future PR mapping (measurements → work)

| PR | Uses these measurements |
|----|-------------------------|
| SEC-01 | ops hang risk (FN-05); no change to prod baseline |
| SEC-02/03 | npm advisories unchanged by SSH |
| SEC-04 | no host `limit_req`; client IP trust unknown at edge; need calibration |
| SEC-05 | idle queue depths; worker concurrency 2 / download 1; must load-test |
| SEC-06 | client census 62 / 0 per 24h — bootstrap budget still needed |
| SEC-08/09 | shared bridge worker↔postgres (FN-04) |
| HSTS / host headers | FN-06 — host Nginx change (owner deploy) |
| Config-authority drift | dedicated remediation after Reliability A / release tooling |
| DB role split | dedicated hardening PR |
| Backup verify @ 0010 | owner-approved acceptance drill |

Reliability A gateway-routing remains a **separate** dirty tree; do not mix diffs into SEC PRs.

## 10. SEC-00 acceptance checklist

- [x] Release/image/manifest authority traced
- [x] Compose provenance + publish/listen matrix
- [x] Applied CPU/memory limits recorded
- [x] Process/user/capability/socket matrix recorded
- [x] Disk/inode + queue/lease aggregates recorded
- [x] DB role capabilities recorded (sanitized)
- [x] Backup/restore evidence reviewed; current-head gap explicit
- [x] FN-02 retained-by-owner status
- [x] HSTS/TLS terminator confirmed
- [x] Unknowns remaining: external firewall/WAF only (and any off-host backup copy — not verified)
- [x] Docker ACL restored; unprivileged `docker ps` denied
- [x] Production mutations: NONE

**`SEC-00 — EVIDENCE COMPLETE / READY FOR FINAL REVIEW`**
