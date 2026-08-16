# 30. Production release runbook / design

**INFO:** This chapter is a production-grade **design and operator runbook**.
It does **not** claim that a production host exists, that production is
deployed, or that production Make/CLI targets are already available.

Staging today uses a verified release pipeline
(`preflight → prepare → verify → deploy-plan → migrate-if-required →
rollout → health`). Production must reuse those same primitives on a
**separate** host, with independent env, database, volumes, secrets,
release state, and backups.

**Current tooling boundary (stop-gate):** Make recipes and the release CLI
allowlist are still staging-shaped (`fetchnow-staging`,
`compose.staging.yaml`). Passing `--project-name fetchnow-production`
is **not** supported until the parameterization milestone in
[§12](#12-future-milestone-production-parameterization) lands. Do not
improvise production deploys with the staging project name, staging
deploy root, or `deploy/compose/compose.prod.yaml` as a substitute.

## Target topology (planned)

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

Do **not** bind this runbook to a concrete production IP. The production
host is provisioned later; DNS and TLS are operator actions on that host.

Staging remains on its own host and domain (`staging.fetchnow.online`).
No production operation may touch staging resources, and the reverse is
also forbidden.

## Naming contract (planned)

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
- `deploy/compose/compose.prod.yaml` currently defaults gateway publish
  to host `:80` and is **not** on the release path — do not use it for
  production until replaced by a loopback fail-closed overlay accepted
  by the source contract (see §12).

---

## 3. Production environment contract

**DANGER:** Never copy staging secrets into production. Never commit
`.env.production`. Never paste real passwords into this handbook.

### File

- Path: `/srv/fetchnow-production/env/.env.production`
- Mode: `600`, owner = operator account
- Start from a **production example** once the parameterization
  milestone ships (today only `.env.staging.example` exists). Until
  then, derive the contract from `.env.staging.example` and this
  chapter, substituting production names/paths/domains.

### Classes of values that must differ from staging

| Class | Production expectation |
|---|---|
| Identity | `COMPOSE_PROJECT_NAME=fetchnow-production`, `APP_ENV=production` |
| Public URL | `PUBLIC_SITE_URL=https://fetchnow.online` |
| Paths | `FETCHNOW_DEPLOY_ROOT=/srv/fetchnow-production`, `FETCHNOW_BACKUP_ROOT=/srv/fetchnow-production/backups` |
| Gateway | `GATEWAY_PORT=127.0.0.1:<port>` (loopback) |
| Database | Unique `POSTGRES_PASSWORD` (≥32 URL-safe chars); dedicated DB volume via project name |
| Release pin | `FETCHNOW_RELEASE_REVISION=<full-40-char-sha>` matching the staged/accepted release |
| Media flags | Explicit operator choice; do not inherit staging enablement blindly |
| UI flag | `PUBLIC_MEDIA_FLOW_ENABLED` independent of staging |

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
  → parameterization milestone available (STOP if not)
  → database / storage bring-up
  → gateway loopback + host Nginx + TLS
  → preflight
  → prepare + verify (immutable SHA)
  → deploy-plan
  → initial schema (migrate or approved empty-DB path)
  → BOOTSTRAP application rollout
  → health
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
4. **Parameterization gate.** Confirm Make/CLI accept
   `fetchnow-production` and a production compose overlay (§12).
   **STOP** if they still hard-require `fetchnow-staging`.
5. **Database / storage.** Start PostgreSQL only for the production
   project; confirm volume names are project-scoped
   (`fetchnow-production_*`). Run storage-init as defined by the
   release tooling (rollout runs it; do not invent a parallel path).
6. **Gateway / TLS.** Bring loopback gateway up enough for local
   health, then host Nginx + certificate (§2). Public DNS may wait
   until local smoke passes if cutover risk requires it — document the
   chosen order in the bootstrap report.
7. **Preflight.** Read-only gate against production env + deploy root +
   expected SHA.
8. **Prepare + verify.** Materialize immutable tree, build revision-tagged
   images, verify `release.json`.
9. **Deploy-plan.** Confirm migration/backup requirements for the empty
   or initial database.
10. **Initial schema.** Use the verified migration transaction (or the
    approved empty-DB first-migration path from
    [chapter 29](29-verified-migration-transaction.md)). No ad-hoc SQL.
11. **Bootstrap rollout.** First application activation requires explicit
    bootstrap acknowledgement (staging today: `BOOTSTRAP=1`). Refuses if
    application containers or `current.json` already exist.
12. **Health.** Managed health gate (Compose state + loopback live/ready
    + image ID binding).
13. **Smoke.** §8.
14. **Backup.** Create + restore-verify a logical dump under
    `/srv/fetchnow-production/backups`.
15. **Report.** SHA, image IDs, migration heads, deployment id, smoke
    results, cert expiry, neighbour impact (if any), known deviations.

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
- Production parameterization milestone is merged and available on the
  production host checkout.
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

### Intended operator commands (after §12)

Command shapes mirror staging; only project, env, and deploy root change.
Exact Make variable names must match the parameterization PR — until then
these are **design targets**, not runnable recipes:

```bash
# On the production host, at /srv/fetchnow-production/app @ <sha>
export EXPECTED_REVISION=<40-char-sha>   # same SHA that passed staging
export ENV_FILE=/srv/fetchnow-production/env/.env.production
export DEPLOY_ROOT=/srv/fetchnow-production
export BACKUP_ROOT=/srv/fetchnow-production/backups

# 1) pin FETCHNOW_RELEASE_REVISION in ENV_FILE to EXPECTED_REVISION (mode 600)
# 2) preflight → prepare → verify → deploy-plan
# 3) if plan.migration_required: release-migrate (creates verified backup)
# 4) release-rollout
# 5) release-health
# 6) production smoke (§8)
```

Until §12 ships, **STOP** rather than redirecting staging Make targets
with a different `DEPLOY_ROOT` — project name and compose overlay remain
hard-coded to staging inside those recipes and inside prepared release
runtime compose selection.

### Promotion rule

```text
feature work → main → staging release @ SHA
  → staging acceptance
  → production release @ SAME SHA
```

Never deploy a SHA to production that has not completed staging
acceptance. Never “hotfix only on production.”

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

### Minimum set

| Check | Expectation |
|---|---|
| Public web `GET https://fetchnow.online/` | 200, FetchNow HTML |
| Loopback `GET /api/v1/health/live` | 200 `{"status":"ok"}` |
| Loopback `GET /api/v1/health/ready` | 200 (DB reachable) |
| Public HTTPS live/ready | Same, with TLS validation on |
| Compose `ps` | gateway/api/web/delivery/postgres healthy; worker running; restarts=0 since rollout |
| Image revision labels | `org.opencontainers.image.revision` equals expected SHA |
| Capability contract | Job/API responses that expose capabilities match the SHA under test (no staging env bleed) |
| Inspection happy path | One safe public fixture URL through inspect (only if media flags enabled) |
| Download job | Enqueue + reach ready for an allowed fixture (only if downloads enabled) |
| Browser grant / content | Grant issue + content path reachable under HTTPS (only if browser delivery enabled) |
| Logs since rollout | No critical traceback/unhandled errors (ignore benign logger-name noise) |

### Provider fixtures

- Use only **safe public** media URLs agreed for operator smoke.
- Never commit secret or private URLs.
- Provider blips are not automatic NO-GO for process liveness; correlate
  with live/ready and logs ([chapter 13](13-healthchecks-and-smoke-tests.md)).

### Feature flags

If media/download/browser-delivery flags are still disabled on
production, record that in the smoke report and limit checks to web +
health + compose. Do not invent fake end-to-end success.

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
| Backups | [15](15-backups-and-restore.md) |
| Rollback decision table | [16](16-rollback.md) |
| Preflight / health | [24](24-release-preflight-health.md) |
| Prepare / verify | [25](25-release-materialization-build.md) |
| Rollout / recover | [26](26-deployment-transaction-rollback.md) |
| Deploy-plan | [27](27-migration-compatibility-deployment-planning.md) |
| Dual state | [28](28-dual-application-database-state.md) |
| Migrate / migration recover | [29](29-verified-migration-transaction.md) |

---

## 12. Future milestone: production parameterization

### Why

Release transaction machinery is real and staging-proven, but operator
entry points and the prepared-release source contract still assume
staging. Production cannot honestly reuse them until those assumptions
are parameterized. Shipping unverifiable “production” Make targets now
would create false confidence.

### Scope (implement later — not in this documentation PR)

1. Allowlist / accept Compose project `fetchnow-production` (today
   non-staging projects are rejected; `fetchnow-prod` is explicitly
   forbidden in test naming and is **not** an allowlisted env).
2. Introduce a production compose overlay that matches the release
   safety model: fail-closed secrets/revision interpolation, loopback
   gateway publish, `APP_ENV=production`. Replace or retire the current
   `deploy/compose/compose.prod.yaml` host-`:80` fragment for release
   use.
3. Parameterize Make defaults or add explicit production targets so
   `--project-name`, `--compose-file` list, `ENV_FILE`, `DEPLOY_ROOT`,
   and `BACKUP_ROOT` are not hard-coded to staging.
4. Extend the release source contract / runtime compose selection so
   prepared releases and rollout/migrate/recover use the production
   overlay when the project is production (today they always select
   `compose.staging.yaml` from the snapshot).
5. Add `.env.production.example` (placeholders only) documenting the
   production contract.
6. Keep gateway health URL loopback-only; document production public
   HTTPS as operator smoke, not CLI health authority.
7. Update chapters 06/21/24–29 cross-links once Make target names are
   real.

### Staging-specific assumptions to remove

| Assumption | Where today |
|---|---|
| `--project-name fetchnow-staging` in Make release recipes | `Makefile` |
| Default `ENV_FILE=.env.staging`, `DEPLOY_ROOT=/srv/fetchnow-staging` | `Makefile` |
| CLI default / allowlist `fetchnow-staging` only | `scripts/fetchnow_release/*` |
| Required source file `compose.staging.yaml` | release source contract (C2) |
| Runtime compose always `compose.yaml` + `compose.staging.yaml` | rollout / migrate / recover |
| Preflight password helper / messaging named “staging” | preflight helpers |
| Optional prod fragment publishes `:80` | `deploy/compose/compose.prod.yaml` |

### Out of scope for that milestone

- Buying or booting the production server.
- DNS/TLS cutover.
- Creating real `.env.production` secrets.
- Automatic off-host backup upload.
- Unified migrate→rollout orchestration (still PRD1C3B2C if pursued).
- Provider feature work.

### Acceptance criteria

- Operator can run the §5 sequence on a disposable integration project
  **and** on documentation-described production names without editing
  Python allowlists by hand.
- Prepared release for production contains the production overlay;
  rollout activates from that overlay + immutable image IDs.
- Staging path remains green and unchanged in behaviour.
- CI forbids targeting real `fetchnow-staging` / `fetchnow-production`
  from ephemeral tests (unique `fetchnow-*-test-*` only).
- This chapter’s command shapes match the shipped Make interface.
- No secrets in Git; `.env.production.example` uses placeholders only.

### Suggested label

`PRD1D-prod-param` (production parameterization of existing release
primitives) — prerequisite to real production bootstrap, not a
substitute for host/Nginx/TLS operator work.

---

## 13. Blockers before real production deployment

1. Production host does not exist yet.
2. DNS `fetchnow.online` not pointed at a production host.
3. TLS certificate not issued.
4. `.env.production` with real secrets not created (and must not be
   created in Git).
5. Release Make/CLI still staging-shaped (§12).
6. No production compose overlay accepted by the source contract.
7. Staging acceptance process for each SHA must remain the promotion
   gate once production exists.
8. Off-host backup copy still planned (same residual risk as staging
   PRD1B).

Until items 1–6 are cleared, treat any production deploy attempt as
**NO-GO**.
