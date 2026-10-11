# SEC-09 / SEC-08 / SEC-03C — Pre-rollout readiness inventory

Status: **NOT READY FOR ACTIVATION** (SEC-08/09 flags ON).
Release-tooling: see `sec-09-release-integration-record.md` (native acceptance
via `.github/workflows/sec09-release-media-activation.yml`; not production).
Source rollout with executor flags OFF: see §5.1 (conditional).
Date: 2026-10-11. Worktree `/Users/dina/projects/FetchNow-sec09`, branch
`codex/security-sec-09-network-isolation`. Residual acceptance, VEX, merge,
SSH/deploy: **not** granted.

---

## 1. Baseline and evidence references

| Item | Value |
| --- | --- |
| Branch | `codex/security-sec-09-network-isolation` |
| HEAD | `e69b5d3a9ff4c779bacbf22f125d21eb90c362c6` |
| Preserved dirty / untracked | `docs/operations/sec-09-media-net-executor.md` (local results); this file |
| Staging | empty |
| Native CI run | https://github.com/DmitriiSeitsman/FetchNow/actions/runs/38124838023 |
| Run conclusion | `failure` (residual policy, not technical crash) |
| Artifact | `sec09-media-net-38124838023-1` |
| `result.json` sha256 | `2ea664aa58306be2d0b5b240c3af5ee5b120171ccdfe5c5e4825cd70eb07f26a` |

### Residual evidence (unchanged; not re-scanned)

| Check | Result |
| --- | --- |
| N1–N12 | **PASS** |
| Fixable HIGH/CRITICAL | **0** |
| network / proxy unfixed HC | **44 instances / 8 unique IDs** each |
| offline unfixed HC | **216 / 50** |
| Per-image verdict | `needs_owner_decision` |
| Owner residual acceptance | **not** granted |
| Overall gate | **FAIL** |

New vs historical SEC-08 “47 unfixed” set (still requiring review, not accepted):
`CVE-2026-77214`, `CVE-2026-88647`, `CVE-2026-88648`.
Isolation does not clear CVEs or imply NOT_AFFECTED.

---

## 2. Production inventory

### 2.1 Historical SSH failure (prior session)

| When | Observation |
| --- | --- |
| 2026-10-11 earlier inventory | Host key OK; server accepted `fetchnow-prod` ED25519; agent had **no identities** (passphrase-locked) → `Permission denied (publickey)`. STOP without key/user enumeration. |

### 2.2 Successful inventory (this session)

| Field | Value |
| --- | --- |
| Inventory UTC start | `2026-10-11T08:56:58Z` |
| Remote UTC samples | `2026-10-11T08:57:00Z` … `2026-10-11T08:57:55Z` |
| Target | `ubuntuuser@195.209.215.87` (`fetchnow-prod`) |
| Auth | Unlocked agent identity `fetchnow-prod` / comment `fetchnow-production`; `BatchMode=yes`; `StrictHostKeyChecking=yes` |
| Mode | read-only; no containers/units/networks/packages/sysctl/env mutations |

### 2.3 Confirmed production facts

#### Release authority

| Fact | Value |
| --- | --- |
| Deploy root | `/srv/fetchnow-production` |
| Project | `fetchnow-production` |
| Active revision | `2d24140434ccd53388e73744dfd276f313e76113` |
| Deployment ID | `eff53f36-ae69-42f0-99fb-8fe03b739ff0` (`status=committed`) |
| Release working_dir | `/srv/fetchnow-production/releases/2d24140434ccd53388e73744dfd276f313e76113/source` |
| Compose files in use | `compose.yaml` + `compose.production.yaml` + `deployments/.../overrides/images.yaml` |
| `state/current.json` updated_at_utc | `2026-09-22T21:01:43Z` |
| Media overlays in active release source | **absent** (`compose.media-executor.yaml` / `compose.media-net-executor.yaml` not present) |

Note: `state/runtime-config.json` still records an older `revision=b6114796…` /
`updated_at_utc=2026-09-15T17:37:24Z` (config-rollout metadata). **Live**
application authority is the container OCI revision label and compose working_dir
above (`2d241404…`).

#### Running images (linux/amd64)

| Service | Image ID (sha256 prefix) | OCI revision label | Memory limit | NanoCPUs |
| --- | --- | --- | --- | --- |
| api / worker / delivery | `6d149ce0b0fa…920403ed` (same ID) | `2d241404…76113` | api/delivery 384MiB; worker 1GiB | 0.5 CPU |
| web | `253f3746df71…b3fb4cd2` | `2d241404…76113` | 64MiB | 0.25 CPU |
| gateway | `dc236a481d25…73ad698a` | `2d241404…76113` | 64MiB | 0.25 CPU |
| postgres | `postgres:16.9-alpine` → `7c688148e5e1…04b1fb7` | none | 512MiB | 0.5 CPU |

#### Worker / product flags

| Flag | Observed |
| --- | --- |
| `MEDIA_MUXING_ENABLED` | **`true`** (worker env + `/srv/fetchnow-production/env/.env.production`) — **product behavior; do not turn off for SEC-08/09** |
| `MEDIA_EXECUTOR_ENABLED` | **absent** from worker env and env file → Settings default **`false`** (executor OFF) |
| `MEDIA_NET_EXECUTOR_ENABLED` | **absent** → Settings default **`false`** (net executor OFF) |
| api | neither executor flag present |

In-process muxing/ffmpeg paths remain active via muxing flag; SEC-08/09 sidecars
are not started.

#### Host / Docker / cgroup

| Fact | Value |
| --- | --- |
| Kernel | `Linux 7.0.0-31-generic x86_64` (`#31-Ubuntu … 2026`) |
| Docker Engine | **29.7.2** (client=server) |
| Docker root | `/var/lib/docker` — **rootful** daemon (not rootless); context `default` |
| Cgroup | **v2**, driver **systemd** |
| Host RAM | ~16 GiB total; ~14.9 GiB available at inventory time |
| Root / docker fs | ~160 GiB disk; ~36% used (~98 GiB available) |
| Host public IPv4 | `195.209.215.87/24` on `enp3s0` |

#### Landlock

| Signal | Value |
| --- | --- |
| `/boot/config-$(uname -r)` | `CONFIG_SECURITY_LANDLOCK=y` |
| `CONFIG_LSM` | includes `landlock` (with lockdown, yama, integrity, apparmor) |
| `/sys/kernel/security/landlock` | **absent** |
| Runtime Landlock **ABI ≥ 6** | **UNKNOWN** — not proven without a dedicated acceptance probe binary/container (not run) |

Kernel build enables Landlock; that is **not** proof the executor’s
`MIN_LANDLOCK_ABI = 6` check will pass. Keep a separate disposable gate.

#### Executor slice / sidecars / sockets

| Item | Value |
| --- | --- |
| `fetchnow-media-executor.slice` unit files | **not installed** (no fragment under `/etc` or `/lib/systemd`) |
| `systemctl show` LoadState | `loaded` but `FragmentPath=` empty, `ActiveState=inactive`, MemoryMax/TasksMax infinity — **not** an installed production ceiling |
| media-executor / media-net-executor / egress-proxy containers | **none** |
| Sockets / work roots (`/run/fetchnow-executor`, `/run/fetchnow-media-net`, `/var/lib/fetchnow/executor`, `/var/lib/fetchnow/media-net`) | **absent** |

#### Networks (deny-list candidates for future SEC-09 proxy)

| Network | Subnet / gateway | Role |
| --- | --- | --- |
| `bridge` (docker0) | `172.17.0.0/16` / `172.17.0.1` | default bridge |
| `fetchnow-production_fetchnow` | `172.18.0.0/16` / `172.18.0.1` | FetchNow app network (6 containers) |
| `xogame-backend_default` | `172.19.0.0/16` / `172.19.0.1` | **neighbor** project |
| `amhelper_default` | `172.20.0.0/16` / `172.20.0.1` | **neighbor** project |
| Host | `195.209.215.87`, gateway `195.209.215.1` | must be denied from tool egress |
| Link-local metadata path | route to `169.254.169.254` via `195.209.215.253` | must remain denied |

Future `EGRESS_DENY_CIDRS` / `EGRESS_DENY_IPS` must cover at least: all docker
bridge subnets above, host address(es), and neighbor project subnets — without
disrupting neighbor stacks (do not modify amhelper/xogame networks).

#### Shared-host neighbors (impact scope only)

| Project | Containers (summary) |
| --- | --- |
| `fetchnow-production` | 6 (api, worker, delivery, web, gateway, postgres) |
| `amhelper` | 4 (api, backup, postgrest, postgres) — healthy/up |
| `xogame-backend` | 3 (app up; migrate exited 0; db healthy) |

Any activation must stay inside FetchNow compose project + new opt-in profiles;
no changes to neighbor projects.

### 2.4 UNKNOWN / separate gates

| Topic | Status |
| --- | --- |
| Landlock ABI integer ≥ 6 on this host/runtime | **UNKNOWN** → required disposable acceptance gate before activation |
| Exact memory budget shared with worker (1 GiB) + future executor slice (template 1 GiB) under concurrent load | **UNKNOWN** until budget decision |
| Residual owner acceptance for 50 offline / 8 shared IDs | **not granted** (policy FAIL remains) |

---

## 3. Corrections to prior readiness claims

1. **Executor flags OFF** means only `MEDIA_EXECUTOR_ENABLED=false` and
   `MEDIA_NET_EXECUTOR_ENABLED=false` (defaults when unset).
   **`MEDIA_MUXING_ENABLED` is independent product behavior** and is currently
   **`true` on production** — do **not** require disabling it for SEC-08/09 work.
2. Split rollout modes:
   - **Source rollout, executor flags OFF** — ship application code via canonical
     release (api/web/gateway/worker image) without starting media sidecars.
   - **Activation, flags ON** — new images, overlays, slice, deny-lists, health,
     rollback — blocked until implementation scope (§4) lands and residuals are
     handled by owner process.

Absence of sidecar release support is a **blocker for activation**, not an
automatic blocker for every source-only deploy of the existing trio, provided
executor flags remain OFF and overlays/profiles are not applied.

---

## 4. Minimal implementation scope for SEC-08/09 **activation**

Local release-integration code for this scope is implemented in-tree (see
`sec-09-release-integration-record.md` + planned manifest). Production
activation remains blocked by host/residual gates in §5.2.

Goal: enable opt-in activation through the **existing release transaction**,
not ad-hoc `compose up`.

### 4.1 Build / manifest / identity / contract hashes

**Candidates:**
`scripts/fetchnow_release/c2_constants.py`, `image_build.py`, `image_identity.py`,
`override.py`, `manifest.py`,
`backend/Dockerfile.media-executor`, `Dockerfile.media-net-executor`,
`Dockerfile.media-egress-proxy`,
`compose.media-executor.yaml`, `compose.media-net-executor.yaml`

**Acceptance:**
- Build tags `fetchnow-media-executor|media-net-executor|media-egress-proxy:<fullsha>`
- OCI revision label + Image ID recorded in release manifest
- Contract hash set includes the three Dockerfiles + two media compose overlays
- Old releases without overlays remain buildable (backward compatible)

### 4.2 Opt-in profiles and backward compatibility

**Candidates:** same compose overlays; `compose_validate.py`; deploy-plan / rollout
path that optionally passes `-f compose.media-*.yaml --profile media-executor
--profile media-net-executor` only when activating.

**Acceptance:**
- Default production plan does **not** start sidecars
- Activation plan is explicit and transactional
- Releases predating overlays still deploy api/web/gateway unchanged

### 4.3 Host slice + shared resource budget

**Candidates:**
`deploy/systemd/fetchnow-media-executor.slice` (today: uninstalled template),
ops runbook; align with worker `mem_limit=1GiB` and host ~16 GiB.

**Acceptance:**
- Unit installed/enabled **before** first executor container
- `cgroup_parent` resolves to a real ControlGroup ancestor
- Documented MemoryMax/CPUQuota/TasksMax that cannot starve neighbors
- Verified with `systemctl show … -p ControlGroup,MemoryMax`

### 4.4 Preflight Docker / cgroup / Landlock

**Candidates:** release preflight module (extend `docker_checks.py` / new helper),
native harness patterns.

**Acceptance:**
- Rootful Docker ≥28, cgroup v2, systemd driver
- Disposable Landlock ABI probe returns ≥6 **without** leaving residue
- Fail closed if ABI unknown/low

### 4.5 Settings classification and controlled activation

**Candidates:**
`scripts/fetchnow_release/config_contract.py`,
`backend/src/fetchnow/core/config.py` (already has fields), env/schema docs.

**Acceptance:**
- Classify `MEDIA_EXECUTOR_ENABLED`, `MEDIA_EXECUTOR_SOCKET`,
  `MEDIA_EXECUTOR_WORK_ROOT`, `MEDIA_NET_EXECUTOR_ENABLED`,
  `MEDIA_NET_EXECUTOR_SOCKET`, `MEDIA_NET_EXECUTOR_WORK_ROOT`
- Activation requires absolute socket/work roots
- **Do not** require changes to `MEDIA_MUXING_ENABLED`
- Flags remain false unless activation step sets them after sidecars healthy

### 4.6 Infrastructure deny-list

**Candidates:**
`compose.media-net-executor.yaml` (`EGRESS_DENY_CIDRS` / `EGRESS_DENY_IPS`),
config validation, production env controlled keys.

**Acceptance:**
- Non-empty deny covering at least production facts in §2.3 networks
- Refuse SEC-09 enable when deny lists empty
- No modification of neighbor compose projects

### 4.7 Health, journal, rollback / recovery

**Candidates:**
`health.py`, deploy journal/events, rollout/rollback commands,
override `images.yaml` extension.

**Acceptance:**
- Health: protocol UDS + deny-proven proxy (not bare port-open); worker flags;
  fail-closed when proxy/socket unavailable
- Journal records images, release_mode, overlays/profiles, executor settings
  (no secrets) + previous_* for restore
- Rollback restores **previous** state (OFF→ON → OFF; ON→ON → previous ON
  images/config); leaves muxing unchanged; does not uninstall host slice or
  global-prune

### 4.8 Tests / disposable gates

**Candidates:**
`tests/release/…` unit extensions; disposable integration under
`fetchnow-*-test-*` project prefixes only; reuse SEC-09 native matrix patterns.

**Acceptance:**
- Unit: contract hashes, flag classification, deny-list validation, compose
  profile wiring
- Disposable: slice → sidecars → sockets → flag ON → health → rollback
- No production project names in tests

---

## 5. Independent rollout verdicts

### 5.1 Source rollout with executor flags OFF

**CONDITIONAL — not blocked solely by missing sidecar release support.**

May proceed **only if** all of the following hold:

1. Canonical release path (api/web/gateway) only; **no** media profiles/overlays
   applied on production.
2. `MEDIA_EXECUTOR_ENABLED` and `MEDIA_NET_EXECUTOR_ENABLED` remain unset/false.
3. **`MEDIA_MUXING_ENABLED` left as-is** (currently `true`).
4. Branch merge + CI + applicable security gates for that revision pass (including
   residual policy handling as required by gate rules — residual acceptance is
   still **not** granted by this inventory).
5. No host slice/sidecar installation as part of that source deploy.

This inventory does **not** authorize a specific merge or deploy.

### 5.2 Activation with SEC-08/09 flags ON

**NOT READY**

Blockers:

1. ~~Implementation scope §4 not done~~ — **local release integration landed**;
   still requires Codex review + media-mode prepare of the exact candidate.
2. Host slice not installed; no sidecars/sockets.
3. Landlock ABI ≥6 **UNKNOWN** (needs disposable probe gate).
4. `EGRESS_DENY_*` not populated for production topology.
5. Residual owner acceptance for **50 / 8** unfixed HC IDs **not** granted;
   overall image gate remains FAIL.
6. Shared-host resource budget with worker + neighbors **undecided** —
   aggregate vs per-container must be explicitly approved (1 GiB template is
   **not** auto-approved; conflicts with stated executor mem_limits).

### 5.3 Future activation order (after external gates)

1. Approve resource budget (aggregate ceiling, per-container, concurrency).  
2. Install/enable host slice; prove ControlGroup via `systemctl show`.  
3. Disposable Landlock ABI ≥6 proof on the runtime host.  
4. Populate controlled `EGRESS_DENY_CIDRS` / `EGRESS_DENY_IPS` covering §2.3.  
5. `prepare --release-mode media` for the exact revision (new artifact; do not
   reuse older native/Trivy PASS).  
6. Residual/security gate on **that** candidate.  
7. `rollout --media-activation` with budget JSON + required deny targets +
   `--media-denied-host` (sidecars healthy **before** worker flag flip).  
8. On failure: previous-state rollback (not unconditional flags OFF).

---

## 6. Changed-file manifest

Inventory finalize + release-integration local implementation. Exact list and
hashes: `sec-09-release-integration-record.md` /
`sec-09-release-integration-planned-manifest.md`.

Preserved dirty docs:

| Path | Note |
| --- | --- |
| `docs/operations/sec-09-media-net-executor.md` | pre-existing local results record |
| This file | readiness + activation order |

Staging: **none**.
Commit / push / PR / merge / deploy / VEX / residual acceptance: **not performed**.

---

## STOP

**SEC-08/09 RELEASE INTEGRATION — LOCAL IMPLEMENTATION COMPLETE / READY FOR CODEX REVIEW**  
Not READY FOR PRODUCTION.
