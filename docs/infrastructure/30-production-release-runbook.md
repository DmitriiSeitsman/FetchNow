# 30. Production release runbook / design

**INFO:** This chapter is the production operator runbook. Production at
`https://fetchnow.online` is an accepted environment; do not improvise
deploys with the staging project name, staging deploy root, or ad-hoc
`docker compose up`. DNS/Nginx/TLS cutover is complete for that host and
is not changed by media-flow activation (§5.1).

Staging uses a verified release pipeline
(`preflight → prepare → verify → deploy-plan → migrate-if-required →
rollout → health`). Production reuses those same primitives as a second
canonical real environment (`fetchnow-production`) through
`make production-release-*` / `make production-pg-backup-*`.

The canonical overlay `compose.production.yaml` and
`.env.production.example` exist. A real `.env.production` with secrets
lives only on the production host and must never be committed.

## Target topology

```text
Internet
  -> DNS: fetchnow.online -> <production-public-IPv4>
  -> host Nginx :443
      +-> fetchnow.online -> 127.0.0.1:<gateway-loopback-port>
      |    -> Docker gateway :8080
      |         +-> web :8080
      |         +-> api :8000
      |         +-> delivery :8000 (internal)
      |         +-> worker (no HTTP publish)
      |         `-> postgres :5432 (Compose network only)
```

Do **not** bind this runbook to a concrete production IP. Gateway remains
loopback-only (`127.0.0.1:8091` on the current host). Host Nginx owns
public `:443`.

Staging remains on its own host and domain (`staging.fetchnow.online`).
No production operation may touch staging resources, and the reverse is
also forbidden.

## Naming contract

| Item | Production value | Staging (reference only) |
|---|---|---|
| Public domain | `fetchnow.online` | `staging.fetchnow.online` |
| Compose project | `fetchnow-production` | `fetchnow-staging` |
| Deploy root | `/srv/fetchnow-production` | `/srv/fetchnow-staging` |
| Env file | `/srv/fetchnow-production/env/.env.production` | `…/env/.env.staging` |
| Backup root | `/srv/fetchnow-production/backups` | `…/backups` |
| App checkout | `/srv/fetchnow-production/app` | `…/app` |
| Gateway publish | loopback only (`127.0.0.1:<port>`) | `127.0.0.1:8091` |
| `APP_ENV` | `production` | `staging` |
| `PUBLIC_SITE_URL` | `https://fetchnow.online` | `https://staging.fetchnow.online` |
| `PUBLIC_SEARCH_INDEXING_ENABLED` | `true` only at official launch | hard-coded `false` in `compose.staging.yaml` |

On a dedicated production host the loopback gateway port may reuse
`8091` (nothing else on that host claims it). If production later shares
a host with other services, pick an unused loopback port and pin it in
`.env.production` as `GATEWAY_PORT=127.0.0.1:<port>`. Never publish the
gateway on `0.0.0.0` or host `:80`/`:443` — host Nginx owns those.

---

## 1. Production host prerequisites

**INFO:** Concrete CPU/RAM sizing is an operator capacity decision, not a
release-tooling requirement. The release pipeline only needs enough
disk for images, release trees, and verified backups.

### Operating system and packages

- Supported Linux distribution compatible with current Docker Engine and
  Docker Compose plugin used by staging (document the exact versions in
  the bootstrap report).
- Packages: Docker Engine, Docker Compose plugin, Git, Python 3.12 (for
  release CLI), Certbot + nginx plugin (or equivalent), host Nginx,
  chrony/systemd-timesyncd, and an SSH server.
- System clock synchronized (TLS, cookies, grant TTLs, and logs depend
  on correct time).

### Operator access

- Dedicated non-root service/operator account (staging uses
  `cryptobot`; production may reuse that name on its own host or choose
  another — document it).
- SSH key auth only; disable password login for the operator account.
- Passwordless `sudo` limited to the operations this runbook needs
  (directory create, nginx reload, certbot) — prefer explicit sudoers
  rules over blanket root.

### Filesystem layout

Mirror the staging layout under a **production-only** root:

```text
/srv/fetchnow-production/
├── app/          # Git worktree / checkout used by release tooling
├── env/          # secrets; mode 0700; never in Git
├── backups/      # verified logical dumps; mode 0700
├── logs/         # optional host-side exports
├── releases/     # immutable prepared trees + release.json
├── deployments/  # application rollout journals
├── migrations/   # migration journals
├── locks/        # rollout / backup-root locks
└── state/        # current.json (schema v2)
```

Create with restrictive ownership (example pattern from
[chapter 02](02-linux-filesystem-and-users.md)):

```bash
sudo install -d -o <operator> -g <operator> -m 0750 /srv/fetchnow-production
sudo install -d -o <operator> -g <operator> -m 0750 /srv/fetchnow-production/{app,logs,releases,deployments,migrations,locks,state}
sudo install -d -o <operator> -g <operator> -m 0700 /srv/fetchnow-production/{env,backups}
```

### Network / firewall

- Public inbound: SSH (operator), `80`, `443`.
- Docker must **not** publish PostgreSQL, API, worker, delivery, or web
  on the host.
- Gateway publish is loopback-only; host Nginx proxies to it.
- Outbound HTTPS allowed for providers, image pulls, and Certbot.

### Disk

- Free space above `MIN_FREE_DISK_BYTES` (staging example is 1 GiB; raise
  for production if artifact volume grows).
- Separate headroom for `releases/`, Docker image layers, and
  `backups/` (a verified restore temporarily needs dump + restore space).
- Document volume mount points if the host uses a dedicated data disk.

### Logging

- Application logs via Compose (`docker compose … logs`).
- Host Nginx access/error logs for `fetchnow.online`.
- Do not log secrets, Bearer tokens, delivery cookies, raw provider
  URLs with credentials, or dump SQL with passwords.

### CHECK before any production bootstrap

- Host is **not** the staging server.
- DNS for `fetchnow.online` is not yet required to resolve for
  filesystem/env prep, but must resolve to this host before public TLS
  and public smoke.
- No staging env file, backup, or Compose project is mounted or named
  into this root.
- Local Docker image store already contains `postgres:16.9-alpine`
  (`docker image inspect postgres:16.9-alpine`). If absent, pull it
  explicitly as a host prerequisite: `docker pull postgres:16.9-alpine`.
  That pull is allowed. Manual `docker compose up` / Alembic remains
  forbidden. `bootstrap-db` itself never pulls images (`--pull never`);
  that behavior is intentional and must not change.

---

## 2. DNS / TLS / gateway

**WARNING:** Do not perform these steps until the production host exists
and the operator intentionally publishes the domain.

### Sequence

1. **DNS.** Create `A` (and `AAAA` if used) for `fetchnow.online` →
   production public IP. Prefer a short TTL during cutover. Optional
   `www` is out of scope unless product requires it.
2. **Host Nginx site.** Add a `server_name fetchnow.online` block that
   proxies to `http://127.0.0.1:<gateway-loopback-port>` with the same
   safety posture as staging ([chapter 09](09-host-nginx.md)): no
   buffering of large downloads that would break Range/streaming
   assumptions; forward required headers; do not expose upstream
   ports publicly.
3. **`nginx -t` then reload.** Neighbour vhosts on a shared host must
   remain healthy; on a dedicated production host, still validate
   syntax before reload.
4. **TLS.** Issue a certificate for `fetchnow.online` (Certbot nginx
   plugin is the staging pattern — [chapter 10](10-tls-and-certbot.md)).
   Confirm chain, hostname, and renewal timer.
5. **Public HTTPS health.** `curl -fsS https://fetchnow.online/api/v1/health/live`
   and `/api/v1/health/ready` must succeed with certificate validation
   enabled. Loopback health remains the release CLI gate; public HTTPS
   is an additional operator smoke.

### Boundaries

- Container gateway never terminates public TLS.
- Canonical overlay is repo-root `compose.production.yaml` (loopback
  gateway, fail-closed secrets/SHA, `APP_ENV=production`). Release Make
  selects it via `make production-release-*`. The retired fragment
  `deploy/compose/compose.prod.yaml` (host `:80`) no longer exists and
  must not be reintroduced.

---

## 3. Production environment contract

**DANGER:** Never copy staging secrets into production. Never commit
`.env.production`. Never paste real passwords into this handbook.

### File

- Path: `/srv/fetchnow-production/env/.env.production`
- Mode: `600`, owner = operator account
- Start from `.env.production.example` (placeholders only; never commit
  a real `.env.production`). `make production-release-*` reads
  `/srv/fetchnow-production/env/.env.production` on the production host.

### Classes of values that must differ from staging

| Class | Production expectation |
|---|---|
| Identity | `COMPOSE_PROJECT_NAME=fetchnow-production`, `APP_ENV=production` |
| Public URL | `PUBLIC_SITE_URL=https://fetchnow.online` |
| Paths | `FETCHNOW_DEPLOY_ROOT=/srv/fetchnow-production`, `FETCHNOW_BACKUP_ROOT=/srv/fetchnow-production/backups` |
| Gateway | `GATEWAY_PORT=127.0.0.1:<port>` (loopback) |
| Database | Unique `POSTGRES_PASSWORD` (≥32 URL-safe chars); dedicated DB volume via project name |
| Release pin | `FETCHNOW_RELEASE_REVISION=<full-40-char-sha>` matching the staged/accepted release |
| Media flags | Explicit operator choice; runtime for api/worker/delivery; do not inherit staging enablement blindly |
| UI flag | `PUBLIC_MEDIA_FLOW_ENABLED` is a **web build arg** (independent of staging). Changing it requires a new SHA `prepare`; runtime env alone cannot enable the baked Astro UI |
| Search indexing | `PUBLIC_SEARCH_INDEXING_ENABLED=true` only at official launch after payments; keep `false` for downloader activation. Staging Compose hard-codes `false` |

### Secrets handling

- Generate production DB password with a CSPRNG; reject placeholders.
- Store only under `env/` with mode `600`.
- Preflight already rejects weak/placeholder staging passwords; production
  must keep the same strength rules.
- Delivery grant cookie name/flags are code-defined
  (`__Secure-fetchnow_delivery`, `Secure; HttpOnly; SameSite=Strict`) and
  require HTTPS — they are not env toggles.

### Allowed origins / domains

- Browser clients use `PUBLIC_SITE_URL` as the site identity for the web
  build.
- Provider allowlists and outbound policy stay env-driven
  (`PROVIDER_*`, `URL_*`, `OUTBOUND_*`) and must be reviewed for
  production traffic expectations.

### Release revision

- Always a full lowercase 40-character Git SHA.
- Production receives **the same exact SHA** that passed staging
  acceptance for that release (see §5).
- Abbreviated SHAs, branch names, and `latest` are forbidden.

---

## 4. Initial production bootstrap

Bootstrap is a **one-time** host bring-up. It is not the same as a later
routine release. Database initialization and the first application
activation are separate gates.

### High-level sequence

```text
host prerequisites
  → filesystem + env
  → repository / release tooling checkout
  → production Make wrappers available (STOP if missing)
  → gateway loopback + host Nginx + TLS (as required for later health)
  → preflight
  → prepare + verify (immutable SHA)
  → deploy-plan
  → if initial_bootstrap:
        production-release-bootstrap-db
        verify exact DB heads
        production-release-rollout BOOTSTRAP=1
        production-release-health
  → production smoke
  → first verified backup
  → bootstrap report
```

### Step detail

1. **Host prerequisites.** §1 CHECKs green.
2. **Filesystem / env.** Create `/srv/fetchnow-production/…` and
   `.env.production` (mode `600`). Do not enable public media flags
   until the stack is healthy.
3. **Repository.** Clone or fetch FetchNow into
   `/srv/fetchnow-production/app` at the exact accepted SHA (or keep
   `main` and check out that SHA before prepare). Working tree must be
   clean for preflight rules that require it.
4. **Production wrappers.** Confirm `make production-release-preflight`
   (and the rest of `production-release-*`) hardcode `fetchnow-production`
   and `compose.production.yaml` ([§12](#12-production-parameterization-shipped-interface)).
   **STOP** if those targets are missing.
5. **Do not** start PostgreSQL or run Alembic by hand. Before the first
   `production-release-bootstrap-db`, ensure `postgres:16.9-alpine` exists
   in the local Docker image store (`docker image inspect
   postgres:16.9-alpine`). If it is absent, `docker pull
   postgres:16.9-alpine` is an allowed operator prerequisite. The official
   `production-release-bootstrap-db` transaction starts **only** the
   production postgres service from the immutable release snapshot
   (`--pull never`; it never pulls external images),
   waits until it is healthy, proves the database is **structurally
   fresh** (missing `alembic_version` is not enough; unexpected user
   tables/sequences/views fail closed), and applies Alembic to the
   target release heads. Volume names stay
   project-scoped (`fetchnow-production_*`). Storage-init still runs
   during application bootstrap rollout — do not invent a parallel path.
   Manual `docker compose up` / `docker compose run … alembic upgrade head`
   is not part of production bootstrap.
6. **Gateway / TLS.** Bring loopback gateway up enough for local
   health, then host Nginx + certificate (§2). Public DNS may wait
   until local smoke passes if cutover risk requires it — document the
   chosen order in the bootstrap report.
7. **Preflight.** Read-only gate against production env + deploy root +
   expected SHA.
8. **Prepare + verify.** Materialize immutable tree, build revision-tagged
   images, verify `release.json`.
9. **Deploy-plan.** On a proven-empty host this returns an initial
   bootstrap plan (`initial_bootstrap=true`,
   `initial_schema_required=true`, `migration_required=false`,
   `verified_backup_required=false`). If the plan is an upgrade plan
   instead, stop — this is not first deployment.
10. **Initial schema.** Run `make production-release-bootstrap-db
    EXPECTED_REVISION=<sha>`. Confirm live Alembic heads exactly equal
    the target release heads. This command does **not** start
    api/worker/web/delivery/gateway and does **not** publish
    `current.json`. Do not use `production-release-migrate` here: that
    transaction assumes an existing `current.json` and is an upgrade
    path with verified backup.
11. **Bootstrap rollout.** First application activation requires explicit
    bootstrap acknowledgement: `make production-release-rollout
    EXPECTED_REVISION=<sha> BOOTSTRAP=1`. Refuses if application
    containers or `current.json` already exist. Requires healthy
    postgres and exact DB heads. Publishes the first `current.json`
    only after stabilized success.
12. **Health.** Managed health gate (Compose state + loopback live/ready
    + image ID binding).
13. **Smoke.** §8.
14. **Backup.** Create + restore-verify a logical dump under
    `/srv/fetchnow-production/backups`.
15. **Report.** SHA, image IDs, migration heads, bootstrap id, deployment
    id, smoke results, cert expiry, neighbour impact (if any), known
    deviations.

**DANGER:** Do not run bootstrap against staging paths. Do not use
`docker compose down -v` on a database that has accepted data. Do not
skip health because “TLS is not ready yet” without recording that
public smoke remains open.

---

## 5. Normal production release

### Preconditions (GO)

- Target SHA is an ancestor of `origin/main` (release ancestry rules).
- **The same exact SHA** completed staging acceptance for this release:
  staging `prepare`/`rollout`/`health` and staging smoke for that SHA
  are green, and the operator records the staging deployment id / report.
- Production Make wrappers are present on the production host checkout.
- No unresolved application or migration journals on production.
- Working tree on the production app checkout is clean at the target SHA.
- `.env.production` has `FETCHNOW_RELEASE_REVISION` pinned to that SHA
  (and only that key changes for a pin bump).

### Preferred sequence

```text
preflight
  → prepare (immutable SHA)
  → verify-release
  → deploy-plan
  → [if migration_required] verified backup + migrate
  → rollout (application images only)
  → managed health gate
  → production smoke
  → release report
```

### Operator commands

After a real production env exists on the production host:

```bash
export EXPECTED_REVISION=<40-char-sha>   # same SHA that passed staging

make production-release-preflight \
  EXPECTED_REVISION=$EXPECTED_REVISION

make production-release-prepare \
  EXPECTED_REVISION=$EXPECTED_REVISION

make production-release-verify \
  EXPECTED_REVISION=$EXPECTED_REVISION

make production-release-deploy-plan \
  EXPECTED_REVISION=$EXPECTED_REVISION

# First deployment only, when deploy-plan prints initial_bootstrap=true:
make production-release-bootstrap-db \
  EXPECTED_REVISION=$EXPECTED_REVISION
make production-release-rollout \
  EXPECTED_REVISION=$EXPECTED_REVISION BOOTSTRAP=1

# Later deployments, when migration_required=true:
make production-release-migrate \
  EXPECTED_REVISION=$EXPECTED_REVISION

make production-release-rollout \
  EXPECTED_REVISION=$EXPECTED_REVISION

make production-release-health \
  EXPECTED_REVISION=$EXPECTED_REVISION
```

Recovery:

```bash
make production-release-recover \
  DEPLOYMENT_ID=<uuid> \
  ACTION=rollback|accept-target

make production-release-migration-recover \
  MIGRATION_ID=<uuid> \
  ACTION=accept_source|accept_target
```

Backup:

```bash
make production-pg-backup-create

make production-pg-backup-verify \
  BACKUP_ID=<id>
```

Do **not** redirect staging `release-*` recipes with a different
`DEPLOY_ROOT` and call that production. Staging recipes keep staging-safe
defaults; production wrappers hardcode the canonical production bundle.

### Promotion rule

```text
feature work → main → staging release @ SHA
  → staging acceptance
  → production release @ SAME SHA
```

Never deploy a SHA to production that has not completed staging
acceptance. Never “hotfix only on production.”

---

## 5.1 Production media-flow activation

This is a **controlled enablement of the existing downloader**, not a
new product surface. SEO/indexing, payments, Premium, muxing/transcoding,
and extra providers are out of scope.

`.env.production.example` stays fail-closed. A fresh production install
must not start downloading until the operator copies the activation
bundle below into the host env and ships a **new** immutable revision.

### Why a new revision is required

`PUBLIC_MEDIA_FLOW_ENABLED` is baked into the web image at prepare
(Compose build arg → Astro `import.meta.env`). Backend/worker
`MEDIA_*` flags are runtime. The currently accepted production SHA was
prepared with the UI flag off. Idempotent re-prepare never rebuilds a
finalized SHA, so flipping only runtime flags cannot enable the browser
input. Checkout the merged SHA, pin `FETCHNOW_RELEASE_REVISION`, set the
bundle, then run the official production pipeline.

### Canonical activation bundle (host `.env.production`)

```env
PUBLIC_MEDIA_FLOW_ENABLED=true

MEDIA_INSPECTION_ENABLED=true
MEDIA_INSPECTION_YTDLP_PATH=/opt/venv/bin/yt-dlp

MEDIA_JOBS_ENABLED=true
MEDIA_DOWNLOADS_ENABLED=true
MEDIA_DELIVERY_ENABLED=true
MEDIA_BROWSER_DELIVERY_ENABLED=true

MEDIA_MUXING_ENABLED=false
PUBLIC_SEARCH_INDEXING_ENABLED=false
```

Keep indexing and muxing **false**. Do not enable
`PUBLIC_SEARCH_INDEXING_ENABLED` until payments exist. Worker yt-dlp is
already the canonical image path `/opt/venv/bin/yt-dlp` (verified
`2026.07.04` on the accepted production worker); do not `pip install`
inside live containers.

OK.ru and Dzen remain in the capability matrix and have landing pages,
but public progressive options for those providers require muxing
([ADR 0017](../adr/0017-provider-capability-matrix.md)). With
`MEDIA_MUXING_ENABLED=false`, do **not** treat OK/Dzen navigation links
as a live download smoke path. VK and RUTUBE are the real-download
fixtures for this activation.

#### Later PRD1E-B1 muxing activation (not performed by this PR)

After the PRD1E-B1 revision has passed staging and been explicitly accepted for
production, the controlled production env delta is:

```env
MEDIA_MUXING_ENABLED=true
MEDIA_MUXING_FFMPEG_PATH=/usr/bin/ffmpeg
MEDIA_MUXING_FFPROBE_PATH=/usr/bin/ffprobe
PUBLIC_SEARCH_INDEXING_ENABLED=false
```

Verify the two absolute executables in the immutable worker image before
rollout. This activation adds stream-copy muxing only; it does not authorize a
database migration, transcoding, quota, delivery throttling, Premium, payment,
SEO, or host configuration changes. Roll back by restoring
`MEDIA_MUXING_ENABLED=false` and rolling out the accepted revision; existing
direct progressive downloads remain available.

#### Later PRD1E-B2 quota rollout (not performed by implementation task)

Quota rollout is deliberately split so schema compatibility and accounting
health are verified before new admission depends on them:

1. Deploy the accepted B2 code/configuration with
   `FREE_DOWNLOAD_QUOTA_ENABLED=false`, `FREE_DOWNLOAD_LIMIT=3`,
   `FREE_DOWNLOAD_WINDOW_SECONDS=86400`, and
   `PUBLIC_SEARCH_INDEXING_ENABLED=false`.
2. Use the canonical production release plan, verified backup, and migration
   tooling to advance PostgreSQL to `0007_free_download_quota`. Do not run
   Alembic manually.
3. Verify API/worker/delivery health, the exact database head, and quota
   reconciliation while admission remains disabled. Existing reservation
   finalization is flag-independent.
4. In a separate explicitly approved activation, set only
   `FREE_DOWNLOAD_QUOTA_ENABLED=true` (keeping limit 3, window 86400, and SEO
   false). Before editing the env, initialize sanitized active-config state with
   `make production-release-config-rollout EXPECTED_REVISION=<active-sha> INIT_CONFIG=1`.
   After the atomic env edit, apply it with
   `make production-release-config-rollout EXPECTED_REVISION=<active-sha>` and
   run official health. This recreates only API for the quota flag and keeps the
   accepted images, source deployment identity, and database state unchanged.

Rollback admission by restoring `FREE_DOWNLOAD_QUOTA_ENABLED=false`; do not
downgrade the additive migration, then run the same canonical config rollout.
An unhealthy activation automatically restores the previous sanitized config.
Workers continue consuming/releasing existing reservations. See
[chapter 31](31-runtime-config-rollout.md). This sequence does not authorize B3
throttling, Premium/payments, provider smoke, raw Compose activation, or manual
container edits.

#### Later PRD1E-B3 delivery-rate rollout (not performed by implementation task)

1. Deploy the accepted B3 source with
   `FREE_DELIVERY_RATE_LIMIT_ENABLED=false`,
   `FREE_DELIVERY_RATE_BYTES_PER_SECOND=524288`, and
   `PUBLIC_SEARCH_INDEXING_ENABLED=false`. DB remains
   `0007_free_download_quota`; B3 has no migration and requires no host Nginx
   change.
2. Run full release health and verify full/Range delivery while shaping remains
   off. Reinitialize runtime config state for the new deployment with the
   canonical `INIT_CONFIG=1` transaction; legacy schema-1 state is read but not
   silently upgraded without this gate.
3. In a separate operator-approved activation, keep rate `524288`, change only
   `FREE_DELIVERY_RATE_LIMIT_ENABLED=true`, and run
   `make production-release-config-rollout EXPECTED_REVISION=<active-sha>`.
   Exactly `api` and `delivery` must be recreated with the same immutable image ID.
4. Run official health, then controlled full and Range measurement smoke. At
   512 KiB/s expect roughly 3m20s/100 MiB, 16m40s/500 MiB, and 34m08s/1 GiB.
   Output cadence is about 125 ms per 64 KiB chunk, so existing gateway/host
   inactivity timeouts remain unchanged.

Rollback by restoring `FREE_DELIVERY_RATE_LIMIT_ENABLED=false` through the same
canonical config transaction. API, worker, web, gateway, PostgreSQL, DB head,
SEO, provider acquisition, and muxing configuration must remain unchanged.
Parallel responses may aggregate approximately N × 512 KiB/s; B3 is not a
global or per-identity bandwidth cap.

#### Later PRD2-A1 Robokassa test-mode activation (not performed by implementation task)

A1 is test-only. Live merchant activation (`Запрос на активацию`) is **not**
required for A1 test integration. The FetchNow merchant exists with separate
test Password #1 and Password #2 configured; live payments remain impossible
(`ROBOKASSA_MODE=live` is rejected at startup).

**Confirmed merchant cabinet settings (operator evidence, 2026-09-01):**

| Setting | Value | Method |
|---|---|---|
| MerchantLogin | `fetchnow` | — |
| Classic TEST signature algorithm | SHA256 | — |
| ResultURL | `https://fetchnow.online/api/v1/payments/robokassa/result` | POST |
| SuccessURL | `https://fetchnow.online/payment/success/` | GET |
| FailURL | `https://fetchnow.online/payment/fail/` | GET |

ResultURL is the **only** payment-authoritative transition source. SuccessURL and
FailURL are browser UX only and must not mutate order state.

**Operator test activation env bundle** (deploy-time API recreate only; never
runtime-config; passwords live only in host secrets):

```env
ROBOKASSA_MODE=test
ROBOKASSA_MERCHANT_LOGIN=fetchnow
ROBOKASSA_SIGNATURE_ALGORITHM=sha256
ROBOKASSA_TEST_AMOUNT_MINOR=100
ROBOKASSA_RECEIPT_TAX=<verify separately>
ROBOKASSA_RECEIPT_PAYMENT_METHOD=<verify separately>
ROBOKASSA_ORDER_TTL_SECONDS=3600
PUBLIC_SEARCH_INDEXING_ENABLED=false
```

Store `ROBOKASSA_TEST_PASSWORD1` and `ROBOKASSA_TEST_PASSWORD2` **without**
surrounding single or double quotes in `.env.production`. Literal quote
characters in the secret value cause Robokassa signature error 29; the API
rejects wrapped secrets at startup rather than stripping them silently.

`ROBOKASSA_TEST_AMOUNT_MINOR=100` means **1.00 RUB** and is explicitly
test-only. It is not the future Premium price, a commercial offer, or a
production/live tariff. Production commercial pricing remains undecided.

**Fiscalization — open until verified separately** (does not block committing
A1 code; may block later fiscal/live acceptance):

- Робочеки СМЗ configuration
- connection/authorization with «Мой налог»
- exact `ROBOKASSA_RECEIPT_TAX` value
- exact `ROBOKASSA_RECEIPT_PAYMENT_METHOD` value (`full_payment` vs
  `full_prepayment`)
- whether buyer email is required
- test-mode receipt behavior in Robokassa

Schema rollout may ship with `ROBOKASSA_MODE=disabled` and migration
`0008_payment_orders`. Test activation is a separate operator-approved API
recreate after fiscal profile values are chosen for the test receipt.

### Post-merge operator sequence

Use only `make production-release-*`. No manual `docker compose up`, no
manual container edits, no pip inside live containers, no manual Alembic.

1. Update the production checkout to the new accepted merge SHA
   (`/srv/fetchnow-production/app`, clean worktree).
2. Set `FETCHNOW_RELEASE_REVISION=<new-40-char-sha>` in
   `/srv/fetchnow-production/env/.env.production`.
3. Set the canonical activation bundle above. Do not change
   `GATEWAY_PORT=127.0.0.1:8091`.
4. Keep `PUBLIC_SEARCH_INDEXING_ENABLED=false` and
   `MEDIA_MUXING_ENABLED=false`.
5. `make production-release-preflight EXPECTED_REVISION=<sha>`
6. `make production-release-prepare EXPECTED_REVISION=<sha>`
   (rebuilds web with `PUBLIC_MEDIA_FLOW_ENABLED=true` from the env file)
7. `make production-release-verify EXPECTED_REVISION=<sha>`
8. `make production-release-deploy-plan EXPECTED_REVISION=<sha>`
9. Migrate **only** if the plan requires it
   (`make production-release-migrate EXPECTED_REVISION=<sha>`).
   This activation does not introduce a schema change by itself.
10. `make production-release-rollout EXPECTED_REVISION=<sha>`
11. `make production-release-health EXPECTED_REVISION=<sha>`
12. Public production downloader smoke (§8, activation subset)
13. Confirm indexing is still disabled (`X-Robots-Tag: noindex, nofollow`,
    empty sitemap, robots without a Sitemap line)

---

## 6. Migration policy

Authoritative mechanics:
[chapter 27](27-migration-compatibility-deployment-planning.md),
[chapter 28](28-dual-application-database-state.md),
[chapter 29](29-verified-migration-transaction.md).

### How necessity is decided

- `deploy-plan` compares live DB heads to target release source heads.
- Forward transition → `migration_required=true` and
  `verified_backup_required=true`.
- Equal heads → no migration; application rollout may still be required.
- Downgrade or divergent heads → **hard error**; stop the release.

### Backup timing

- Forward migrations create a verified backup (restore-verify attestation)
  under the backup root **before** Alembic mutates schema.
- Application-only rollouts do **not** create DB backups by themselves;
  schedule regular `pg-backup-create` / `pg-backup-verify` separately.

### Stop-gates

- Unresolved deployment or migration journals.
- Backup create or restore-verify failure.
- Alembic failure after `migration_started` → recovery state (not silent
  success).
- Post-migration head mismatch.
- Current application health regression during migrate (containers must
  remain on the **previous** app revision while schema advances).

### Application rollback vs DB rollback

- `release-rollout` / `release-recover` never roll back the database.
- After a committed forward migration, previous application images are
  allowed only if the compatibility envelope says so.
- Automatic Alembic downgrade is **forbidden**.
- Restoring a logical dump is an escalated operator decision with an
  explicit RPO, not part of normal release-recover.

### Forbidden in normal flow

- Manual `psql` schema edits.
- Host/worktree `alembic upgrade` outside the release migration
  transaction.
- Skipping verified backup on forward migrations.
- Running migrate and application rollout as an improvised single shell
  script that ignores journals/locks.

---

## 7. Rollback / recovery

Authoritative mechanics:
[chapter 16](16-rollback.md),
[chapter 26](26-deployment-transaction-rollback.md),
[chapter 29](29-verified-migration-transaction.md).

### What is recorded as previous revision

- `state/current.json` (schema v2) holds application revision + image IDs
  and database heads / compatibility envelope.
- Each rollout writes an immutable deployment journal under
  `deployments/<deployment-id>/`.
- Successful commit updates application identity only after stabilization.

### Automatic behaviour (existing tooling)

- If the target becomes unhealthy after activation begins, tooling
  **attempts** to restore previous application image IDs, subject to
  live DB heads matching saved heads and the compatibility envelope.
- That is **not** a guarantee of success. If automatic rollback fails,
  the journal ends in a recovery-required state (`rollback_failed`).

### When to run `release-recover`

- Unresolved deployment journal after failed rollout / failed automatic
  rollback.
- Operator has inspected evidence and chooses:
  - `ACTION=rollback` — restore previous application images, or
  - `ACTION=accept-target` — only after manually verifying the target is
    healthy and should become current.

### Failed health gate (before commit)

- Treat as failed rollout. Do not declare the release successful.
- Preserve journals and logs. Follow recover if the transaction left
  unresolved state.

### Failed health gate (after commit) / post-smoke regression

- Prefer forward fix on a new SHA through staging → production.
- Application recover/rollback only if compatibility allows and the
  regression is confirmed in the new revision.

### Migration-related failure

- Use `release-migration-recover` actions
  (`accept_source` / `accept_target` / `release_hold`) as documented in
  chapter 29.
- No automatic restore/downgrade.
- Divergent heads block accept actions until an operator resolves schema
  reality.

### Post-recovery checks

- `release-health` for the intended current SHA.
- Loopback live/ready.
- Production smoke subset (§8) including public HTTPS if published.
- Confirm `current.json` application and database sections match the
  chosen resolution.
- Confirm no restart loops on gateway/api/web/delivery/postgres.

**INFO:** This runbook does **not** promise automatic rollback for every
failure mode. Where tooling journals a recovery requirement, human
decision is mandatory.

---

## 8. Production smoke

Run after every successful health gate. Prefer loopback first, then
public HTTPS.

### Minimum set (always)

| Check | Expectation |
|---|---|
| Public web `GET https://fetchnow.online/` | 200, FetchNow HTML |
| Public legal `GET /privacy/`, `/terms/`, `/copyright/` | 200 |
| Loopback `GET /api/v1/health/live` | 200 `{"status":"ok"}` |
| Loopback `GET /api/v1/health/ready` | 200 (DB reachable) |
| Public HTTPS live/ready | Same, with TLS validation on |
| `X-Robots-Tag` on public HTML | `noindex, nofollow` while indexing is off |
| Compose `ps` | gateway/api/web/delivery/postgres healthy; worker running; restarts=0 since rollout |
| Image revision labels | `org.opencontainers.image.revision` equals expected SHA |
| Gateway publish | still `127.0.0.1:8091` (not `0.0.0.0`) |
| Capability contract | Job/API responses that expose capabilities match the SHA under test (no staging env bleed) |
| Logs since rollout | No `ERROR` / Traceback on worker (ignore benign logger-name noise) |

### Feature flags

If media/download/browser-delivery flags are still disabled on
production, record that in the smoke report and limit checks to web +
health + compose + indexing headers. Do not invent fake end-to-end
success.

### Downloader activation subset

Run the following **only after** §5.1 bundle is live on the new SHA.

| Check | Expectation |
|---|---|
| Public home URL input | Enabled (`data-flow-url` / `#media-link`); no “Скачивание появится в следующем релизе” |
| Provider landings `/vk/`, `/rutube/`, `/ok/`, `/dzen/` | 200, same enabled input, indexing still off |
| URL validation | Safe public VK and RUTUBE URLs → 200; unknown/private/credentials still fail closed ([chapter 13](13-healthchecks-and-smoke-tests.md)) |
| Inspection + job enqueue | One VK and one RUTUBE fixture reach inspected |
| Worker download | Same fixtures reach download `ready` (no muxing) |
| Browser grant + content | Grant issue + artifact through the browser flow under HTTPS |
| Indexing still disabled | `noindex,nofollow` meta + `X-Robots-Tag`; sitemap has no `<loc>`; robots has no `Sitemap:` |
| OK.ru / Dzen | Landing + capability JSON only. Do **not** require a real progressive download while `MEDIA_MUXING_ENABLED=false` |

### Provider fixtures

- Use only **safe public** media URLs already approved for operator smoke
  (VK and RUTUBE). Never commit secret or private URLs.
- Do not add YouTube or Instagram as smoke fixtures.
- Provider blips are not automatic NO-GO for process liveness; correlate
  with live/ready and logs ([chapter 13](13-healthchecks-and-smoke-tests.md)).

---

## 9. GO / NO-GO gates

### GO — production rollout allowed

- Parameterization milestone available on the production host.
- Target SHA = staging-accepted SHA for this release.
- Preflight, prepare, and verify green on production.
- Deploy-plan computed; migrations completed successfully when required.
- No unresolved journals.
- Env pin matches expected SHA; secrets mode `600`.
- Disk and clock CHECKs green.

### NO-GO — stop before mutation

- Tooling still staging-only.
- SHA not on `origin/main` ancestry / not staging-accepted.
- Preflight/prepare/verify/deploy-plan failure.
- Divergent or downgrade DB plan.
- Unresolved prior journals.
- Staging and production resources would be shared or crossed.

### NO-GO — stop / recover after mutation

- Rollout fails stabilization.
- Health gate fails.
- Automatic rollback fails → explicit `release-recover`.
- Migration enters recovery-required state.
- Smoke finds restart loops, wrong revision labels, or critical errors.

### Release is successful only when

1. Rollout committed for the expected SHA,
2. Health gate passed,
3. Smoke passed (scoped to enabled flags),
4. `current.json` application revision equals the expected SHA,
5. Report filed (SHA, deployment id, migration id if any, smoke, residual risks).

Anything less remains an incomplete or failed release.

---

## 10. Separation from staging

| Resource | Isolation rule |
|---|---|
| Host | Different server from staging |
| Domain | `fetchnow.online` ≠ `staging.fetchnow.online` |
| Deploy root | `/srv/fetchnow-production` only |
| Compose project | `fetchnow-production` only |
| Env / secrets | Distinct files; no copies from staging |
| Database | Distinct volume and credentials |
| Release state | Distinct `state/`, journals, locks |
| Backups | Distinct `backups/`; never restore staging dumps into production by habit |
| Images | May share a Docker host tag namespace only on the same machine; production host builds/pulls for its own deploys |

**DANGER:** Never point production Make/CLI at staging `DEPLOY_ROOT` “just
to test.” Never reuse staging `POSTGRES_PASSWORD`. Never attach staging
volumes to the production project.

---

## 11. Mapping to existing chapters

| Concern | Detail chapter |
|---|---|
| Host filesystem | [02](02-linux-filesystem-and-users.md) |
| Ports / loopback gateway | [03](03-networking-and-ports.md) |
| Compose contract | [06](06-docker-compose.md) |
| Host Nginx | [09](09-host-nginx.md) |
| TLS | [10](10-tls-and-certbot.md) |
| Staging bootstrap analogue | [11](11-first-staging-deployment.md) |
| Manual smoke catalogue | [13](13-healthchecks-and-smoke-tests.md) |
| Media-flow activation | [§5.1](#51-production-media-flow-activation) |
| Backups | [15](15-backups-and-restore.md) |
| Rollback decision table | [16](16-rollback.md) |
| Preflight / health | [24](24-release-preflight-health.md) |
| Prepare / verify | [25](25-release-materialization-build.md) |
| Rollout / recover | [26](26-deployment-transaction-rollback.md) |
| Deploy-plan | [27](27-migration-compatibility-deployment-planning.md) |
| Dual state | [28](28-dual-application-database-state.md) |
| Migrate / migration recover | [29](29-verified-migration-transaction.md) |

---

## 12. Production parameterization (shipped interface)

PRD1D-B parameterized the staging-proven transactional pipeline for a
second real environment. This is **not** a production deployment.

### Canonical identities

| Item | Staging | Production |
|---|---|---|
| Compose project | `fetchnow-staging` | `fetchnow-production` |
| Overlay | `compose.staging.yaml` | `compose.production.yaml` |
| Env filename | `.env.staging` | `.env.production` |
| Deploy root | `/srv/fetchnow-staging` | `/srv/fetchnow-production` |
| Backup root | `/srv/fetchnow-staging/backups` | `/srv/fetchnow-production/backups` |
| `APP_ENV` | `staging` | `production` |
| `PUBLIC_SITE_URL` | `https://staging.fetchnow.online` | `https://fetchnow.online` |

`fetchnow-prod` is forbidden and is not an alias. Ephemeral
`fetchnow-*-test-*` projects may use either overlay as a *shape* and
must never become aliases of real environments. Test cleanup refuses
the real names even if invoked directly.

### Source contract v3

New prepare emits `source_contract_version=3` and hashes
`compose.production.yaml` in addition to the v2 set. Manifest field
`compose_overlay` is required on v3 (`compose.staging.yaml` or
`compose.production.yaml`). Legacy v1/v2 releases remain valid; they
omit the field and mean the staging overlay. Production cannot activate
a v1/v2 snapshot.

Runtime overlay selection after prepare uses the immutable snapshot plus
`release.json`, never the live Git checkout. Image tags remain the
exact 40-character SHA (`latest` / branch tags are forbidden).

### Make interface

Existing `release-*` / `pg-backup-*` recipes keep staging-safe defaults
and work without new arguments. `production-*` wrappers hardcode the
canonical production bundle as recipe literals so command-line
`PROJECT_NAME` / `COMPOSE_OVERLAY` / `ENV_FILE` / `DEPLOY_ROOT` /
`BACKUP_ROOT` overrides cannot turn them into staging operations.

Exact production commands are listed in §5.

### Remaining residual work (not media-flow activation)

- Off-host backup copy (same residual risk as staging PRD1B).
- Unified migrate→rollout orchestrator (operators still run deploy-plan
  then migrate-if-required then rollout).
- Official search-indexing launch (payments first; keep
  `PUBLIC_SEARCH_INDEXING_ENABLED=false`).
- OK.ru / Dzen progressive download (requires a later muxing activation,
  not this bundle).

---

## 13. Remaining production residuals

Accepted production at `https://fetchnow.online` already cleared host,
DNS, TLS, Nginx, loopback gateway, bootstrap, and fail-closed media
flags. Treat the following as still open — they are **not** unblocked by
§5.1 media-flow activation:

1. Off-host backup copy (same residual as staging PRD1B).
2. Staging acceptance remains the promotion gate for every new SHA.
3. Search indexing stays disabled until payments exist.
4. Muxing/transcoding stays disabled; OK/Dzen real progressive download
   is out of scope until muxing is explicitly activated.

Media-flow activation after merge is a routine production release of a
new SHA with the §5.1 env bundle. It is not a host/DNS/TLS change.
