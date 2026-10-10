# SEC-09 — Network isolation plan (targeted discovery)

Status: **CORRECTIVE PASS COMPLETE / LOCAL REGRESSION PASS / READY FOR CODEX REVIEW**
Date: 2026-10-10. Worktree `/Users/dina/projects/FetchNow-sec09`, branch
`codex/security-sec-09-network-isolation`, based on SEC-08 HEAD
`022d83f901d22ffa8a0132c35e7128ba1d0f9b31` (uncommitted local stage).

Discovery sections below remain the boundary design. §11 is the
implementation contract for this local stage. It is **not** residual
acceptance, production approval, SEC-08 reopen, SEC-11, or native PASS.

## 0. Baseline preserved

| Item | Value |
| --- | --- |
| Expected HEAD | `022d83f901d22ffa8a0132c35e7128ba1d0f9b31` (confirmed) |
| Staging | empty at discovery start |
| Local uncommitted SEC-08 reports | preserved (`sec-08-media-executor.md`, `sec-08-executor-image-audit.md`) |
| SEC-08 same-artifact run | [37268708428](https://github.com/DmitriiSeitsman/FetchNow/actions/runs/37268708428) |
| Proven Image ID | `sha256:3969a416cacb3db87f9f03f514a6343c3c6e625bb117609d2aedc465fdaece21` |
| Native | PASS |
| Fixable HIGH/CRITICAL | 0 |
| Unfixed HC | 213 instances / 47 IDs — **decision NOT granted** |
| Workflow | non-green via `unfixed_residual` (exit 2) — honest policy result |

SEC-08 offline mux/ffprobe isolation stands. This plan does not re-run native
or Trivy. Old failed/skipped runs are not rewritten.

Pinned tool version in tree: `yt-dlp==2026.7.4` (`backend/pyproject.toml`,
`backend/uv.lock`). Official option text below is taken from the
[yt-dlp 2026.07.04 README](https://raw.githubusercontent.com/yt-dlp/yt-dlp/2026.07.04/README.md).

## 1. What SEC-09 is (from repo instructions, not the name)

Remediation (`security-reviews/2026-09-18/remediation-prompt.md`):

- Depends on SEC-08; closes the remaining part of FN-04.
- Apply network policy to the **isolated media process**.
- No reachability to Postgres, controlling worker, Docker daemon,
  loopback/private/link-local/cloud metadata.
- Controlled egress proxy or equivalent; verify IPv4/IPv6, DNS, redirects,
  re-resolution. Pre-launch HTTP checks do not replace connection control.
- CONNECT must protect the actual dial target, not only the initial name.
- No TLS verification off; no temporary allow-all. Fail closed if the control
  layer is down (no silent direct internet).

Feasibility plan (`docs/operations/sec-08-09-isolation-plan.md` §2.3):

| Attachment | Role |
| --- | --- |
| `media-offline` / `network_mode: none` | ffmpeg/ffprobe (SEC-08 delivered as `media-executor`) |
| `media-net` (internal; only peer = egress proxy) | yt-dlp |
| `egress-proxy` | `media-net` + external path; destination policy |
| `fetchnow` | worker / Postgres — not joined to media nets |

Synthetic netns harness rows are **not** Compose policy proof (same doc).

## 2. Confirmed execution / network map

### 2.1 Paths still on the trusted worker (today)

| Path | Caller | Executable | Network today |
| --- | --- | --- | --- |
| Inspection / metadata | `MediaWorkerLoop` → `MediaInspectionService` → `YtDlpMetadataExtractor` | `MEDIA_INSPECTION_YTDLP_PATH` (default `/opt/venv/bin/yt-dlp`) | Worker Compose network `fetchnow` (shared with Postgres). **No** egress proxy. |
| Progressive download | `DownloadExecutor._run_direct_download` | same yt-dlp | same |
| Video then audio for mux | `DownloadExecutor` two `_run_exact_ytdlp` stages | same yt-dlp | same |
| SafeHTTP wrapper/document/probe | resolution / optional probe | httpx via `SafeHTTPClient` | Validator + DNS publicness + redirect revalidation; `trust_env=False`. **Not** used for yt-dlp I/O. |

Publication authority for all of the above remains the worker with DB
lease+fence (`complete` / `complete_ready`). Tools never mark READY.

### 2.2 Paths already offline (SEC-08)

| Path | When | Network |
| --- | --- | --- |
| `mux_copy` / `ffprobe_validate` | `MEDIA_EXECUTOR_ENABLED=true` | Executor `network_mode: none` |
| Same ops in-process | flag false (default) | Worker network; argv uses `-protocol_whitelist file` only |

Protocol rejects `inspect_metadata` and `download_progressive` with
`network_not_in_sec08` (`media_executor/constants.py`, `protocol.py`).

### 2.3 Per-path facts (yt-dlp on worker)

**Inspection**

- Fixed argv: `--ignore-config`, `--no-plugin-dirs`, `--no-js-runtimes`,
  `--no-update`, `--skip-download`, `--dump-single-json`, `--no-cookies`,
  `--use-extractors <allowlist>`, URL after `--` =
  query/fragment-free canonical `tool_url`
  (`media_inspection/ytdlp_argv.py`).
- Extractors (immutable defaults): `vk`, `rutube`, `odnoklassniki`,
  `dzen.ru` — generic forbidden (`media_inspection/registry.py`).
- Env: sanitized `HOME`/`TMP*`/`PATH=/usr/bin:/bin`/`NETRC` nonexistent;
  rejects `DATABASE_URL` and `HTTP(S)_PROXY` / `ALL_PROXY`
  (`media_inspection/process.py`).
- Limits (defaults): process 30s (≤120), socket 10s, stdout ≤1 MiB,
  stderr ≤256 KiB; JSON depth/node caps.
- Cancel: process-group kill; lost lease discards result.
- Secondary CDN/API URLs after the canonical page: **UNCONFIRMED** in-repo
  (parse strips `url` / `manifest_url` / `http_headers`).

**Progressive / mux-input download**

- Same hardening flags; adds `--max-filesize`, exact `-f <token>`, `-o`
  template; **no** `--external-downloader`, `--ffmpeg-location`, merge flags
  (`downloads/ytdlp_download_argv.py`; unit tests forbid those flags).
- Defaults: timeout 300s (≤600), socket 30s, max bytes ~3 GiB,
  min free disk 2 GiB, concurrency 1.
- Env secret/proxy-key denial identical to inspection.
- Result file under attempt workspace → `ArtifactStore.publish` only under
  live fence.

**Mux after downloads**

- Network stages are yt-dlp (above). Offline mux/probe is SEC-08 (or
  in-process file-only argv when flag off).

### 2.4 Official yt-dlp facts (2026.07.04) vs repo assumptions

| Topic | Official | Repo posture |
| --- | --- | --- |
| Proxy | `--proxy URL`; `--proxy ""` = **direct** connection | Tool env currently **forbids** proxy keys; argv does **not** pass `--proxy` today |
| External downloader | `--downloader` / `--external-downloader` supports native, aria2c, curl, ffmpeg, wget, … | Not passed; tests forbid. Default remains **native** unless configured |
| ffmpeg location | `--ffmpeg-location PATH` | Not passed. Worker tool `PATH` still includes `/usr/bin:/bin` where ffmpeg may exist |
| file:// | Disabled by default; `--enable-file-urls` opts in | Not passed |

**Confirmed:** FetchNow does not enable external downloaders or ffmpeg via argv.
**UNCONFIRMED at runtime:** whether any extractor path still spawns a helper
despite omission (mitigate in SEC-09 image/PATH; do not treat argv omission as
sole network control).

### 2.5 Credentials

- yt-dlp invocations: **no** app/DB/payment cookies or secrets in argv/env;
  `--no-cookies`; isolated nonexistent `NETRC`.
- Worker retains `DATABASE_URL` for lease/publish (must not enter the network
  tool container).
- SafeHTTP is a separate, already-bounded surface; SEC-09 does not replace it.

### 2.6 What is already constrained vs not

| Control | Constrains yt-dlp egress? |
| --- | --- |
| URLValidator / blocked destinations / provider allowlist | Initial target only |
| SafeHTTP | No — yt-dlp does its own I/O |
| Fixed argv / sanitized env | Reduces foot-guns; not destination policy |
| SEC-08 executor `network_mode: none` | Yes for mux/ffprobe only |
| Compose `fetchnow` worker attachment | **No** egress proxy; worker can reach Postgres and whatever the bridge/NAT allows |
| Production host firewall | **UNKNOWN** here; not assumed |

## 3. Recommended minimal architecture (one variant)

Reuse the **proven SEC-08 mechanism** (trusted supervisor uid 0 with
SETUID|SETGID|SETPCAP only; tool uid 10003; Landlock ABI ≥6; private cgroup
v2; UDS `SO_PEERCRED` uid 10001; server-built argv; sticky work root; no
silent fallback). Do **not** invent a second sandbox platform.

### 3.1 Service split

| Service | Network | Runs | Image |
| --- | --- | --- | --- |
| `media-executor` (existing) | `network_mode: none` | offline `mux_copy` / `ffprobe_validate` | current `Dockerfile.media-executor` (ffmpeg/ffprobe; no change to offline contract) |
| `media-net-executor` (new) | Compose network `media-net` with `internal: true`; **only** other peer = `egress-proxy` | yt-dlp `inspect_metadata` / download ops | new slim image: pinned `yt-dlp==2026.7.4`, launcher/supervisor code, **no** Docker socket, **no** DB URL, **no** compiler in runtime; prefer **no ffmpeg/aria2c/curl** on `PATH` |
| `egress-proxy` (new) | `media-net` + external egress path | destination-validating forward/CONNECT proxy | minimal userspace proxy image (not a general platform) |
| `worker` | `fetchnow` only | DB, lease/fence, quotas, publication, handoff client | unchanged app image |

Worker never joins `media-net`. Network tool never joins `fetchnow`.
Offline executor stays without any external interface.

### 3.2 Why the existing mechanism is enough (and what is missing)

**Sufficient already:** process/uid/Landlock/cgroup/UDS/protocol shape,
fail-closed flag wiring pattern, handoff copy rules, cancel/reap, restart
refusal of stale dirs, peer-uid auth.

**Missing contracts (must be added; not privilege expansion):**

1. Compose `media-net` (`internal: true`) + proxy peer topology.
2. Userspace egress proxy with dial-time destination checks.
3. Protocol activation for network ops + server-built yt-dlp argv including
   mandatory `--proxy <proxy-url>` (never `--proxy ""`).
4. Worker client path that sends inspection/download to `media-net-executor`
   under a new default-off flag (or staged extension of the executor flag
   with explicit offline vs network sockets).
5. Native Compose acceptance (not synthetic netns alone).

**STOP before implementation if** any design requires `CAP_SYS_ADMIN`,
`privileged`, Docker socket, host networking, binding the host cgroup
hierarchy, or disabling seccomp/AppArmor. Those are out of scope; choose
Compose topology + userspace proxy instead.

### 3.3 Trusted computing base

| Component | Trust role |
| --- | --- |
| Worker (uid 10001) | Validates URL/provider/token; holds lease/fence; copies bytes; publishes |
| Offline supervisor | As SEC-08; no network |
| Network supervisor | Same privilege model as SEC-08; may pass proxy URL only as **server-built** argv; still no DB/payment secrets |
| Egress proxy | DNS resolution for CONNECT/forward targets; deny/allow dial; only process with external route from media path |
| DNS used by proxy | Part of TCB; must not be attacker-controlled from `media-net` |
| Narrow privilege transitions | SETUID/SETGID/SETPCAP into tool uid only — unchanged |

### 3.4 Control that works even if the tool ignores proxy env

1. **Compose:** `media-net` is internal; no default route to the internet; the
   only L3 peer is the proxy. Direct provider/metadata/Postgres dials fail
   closed at the network attachment.
2. **Proxy:** every new destination (CONNECT host or forward URL host) is
   resolved and checked before dial; dial uses the checked address set.
3. **Argv:** server adds `--proxy http://egress-proxy:<port>` so cooperative
   yt-dlp uses the proxy; this is **not** the sole control.
4. **Env:** do not rely on `HTTP_PROXY` in the tool environment (today those
   keys are denied; keep denial unless an owner-approved exception is
   documented). Prefer argv `--proxy` only.

If the proxy is down or unreachable: operation fails; **no** worker-side
direct yt-dlp fallback.

## 4. Network / SSRF contract

### 4.1 Always forbidden (dial-time, every hop)

Reuse and extend the spirit of `url/destination.py` on the **proxy dial
path** (not only the pre-tool validator):

- Loopback, unspecified, private, link-local, multicast/reserved as blocked
  by current helpers.
- Cloud metadata `169.254.169.254` and IPv6 analogues / mapped forms.
- Docker bridge/gateway and host addresses observable in the Compose project
  (enumerate in acceptance from the disposable project; do not hardcode a
  single lab IP as universal truth).
- Postgres / worker / API / delivery addresses on `fetchnow`.
- Direct dial from `media-net` to any non-proxy peer.

Apply to **IPv4 and IPv6**. Prefer disabling IPv6 on `media-net` **or**
enforcing the same deny rules on v6 — acceptance must prove v6 is not a
bypass.

### 4.2 Allowed external destinations (MVP)

**MVP dial policy:** after deny-list, allow only globally routable unicast
destinations the proxy itself resolves, on permitted ports:

- HTTPS `CONNECT` to port **443** (required).
- Plain HTTP forward to port **80** only if a provider still needs it;
  default posture HTTPS-only unless acceptance shows otherwise.
- No arbitrary port range; no SOCKS unless later justified.
- TLS verification toward origins remains on (no MITM requirement for MVP).

This is **not** “allow all”: private/metadata/link-local/Docker/host remain
denied. It **is** weaker than a CDN hostname allowlist.

**Provider/hostname restrictions**

- App layer already restricts initial page hostnames (VK/Rutube/OK/Dzen).
- Exact secondary CDN hostnames: **UNCONFIRMED** — do not freeze a wide
  allowlist from one observed URL.
- Optional proxy hostname/suffix allowlist = **deferred owner decision**
  after measured non-prod capture with mocks/fixtures, not live scraping in
  this discovery.

### 4.3 DNS, rebinding, redirects

| Risk | Proxy contract |
| --- | --- |
| DNS rebinding | Resolve at decision time; reject if any address is forbidden; dial only checked addresses; re-check on redirect to a new host |
| Redirect to forbidden IP/host | Re-validate each new target before dial |
| Tool bypasses proxy | Blocked by internal `media-net` (no external route) |
| `--proxy ""` / alternate proxy | Not present in server argv; alternate proxy address not routed on `media-net` |
| Custom DNS from tool | Tool’s resolver cannot create an external route; proxy DNS is authoritative for allowed egress |
| Proxy/DNS down | Fail closed |

### 4.4 HTTPS CONNECT residual (explicit)

Without TLS interception, CONNECT hides HTTP method/path/headers inside the
tunnel. The proxy **can** enforce:

- CONNECT permission (host[:port] syntax, port allowlist),
- DNS + dial IP policy,
- connection/byte/time limits at the proxy.

The proxy **cannot** see or restrict HTTPS paths inside the tunnel. That
residual is accepted and documented; it is not called “solved.” Mitigations:
initial URL allowlist, fixed yt-dlp extractors, byte/time caps, deny-list on
dial IP for every new CONNECT.

### 4.5 Compose vs proxy responsibility

| Guarantee | Owner |
| --- | --- |
| No route from network tool to Postgres/worker/internet except via proxy | Compose `internal` network + service attachments |
| Offline mux has no proxy and no external interface | `network_mode: none` (existing) |
| Forbidden destinations even when hostname looks public | Proxy dial checks |
| Provider business allowlist (first URL) | Worker / URLValidator (existing) |
| Fail closed when proxy absent | Compose + client timeouts; no direct fallback |

Synthetic netns proofs remain non-authoritative for Compose.

## 5. RPC and filesystem handoff

### 5.1 Protocol additions (minimal)

Activate deferred ops on **`media-net-executor` only** (offline executor keeps
rejecting them):

| Op | Purpose | Client-supplied fields (only) |
| --- | --- | --- |
| `inspect_metadata` | yt-dlp metadata JSON | `v,op,job_id,attempt,fence` + bounded `url` (canonical) + `provider_id` (or server-resolved extractors derived from it) |
| `download_progressive` | exact `-f` progressive file | above + bounded `format_token` + `output_kind` enum (`progressive` / `video` / `audio`) |
| existing `reserve` / `cancel` / `release` | lifecycle | unchanged identifiers |

Rejected from client forever: executable, argv, env, host paths, PIDs, cgroup
paths, proxy URL/config, credential paths, extractor generics, arbitrary
ports.

Server builds argv from the same builders as today, plus mandatory
`--proxy <fixed proxy URL>` and output paths under the job directory
(`output-artifact` / fixed names analogous to `output-mux`).

Size bounds (starting point; reuse constants where possible):

- request ≤ 4096 bytes;
- metadata stdout ≤ current inspection stdout cap (1 MiB class);
- response frame ≤ 400000 for metadata; download success returns code +
  checksum/size, **not** file bytes on the UDS;
- download max bytes = server settings (`MEDIA_DOWNLOAD_MAX_BYTES`), enforced
  by argv `--max-filesize` and handoff/`stat` checks.

### 5.2 Trust boundary for results

- Worker must not treat tool success as publish authority.
- Re-check lease/fence after completion; copy via existing handoff rules
  (regular file only, no symlink follow, size ≤ max).
- Restart/reclaim: refuse stale reservations/results (SEC-08 behavior).
- Flag on + network executor error: **fail closed**; no in-worker yt-dlp
  fallback.

### 5.3 Disk / memory / process budget (not production-signed)

| Concern | Discovery note |
| --- | --- |
| Handoff copy | Up to ~2× peak bytes (executor work + worker attempt) before publish; owner must accept disk headroom |
| Concurrent tools | Keep `MAX_JOBS=2` unless measured otherwise |
| cgroup memory | New disposable test ceiling for network+download; **not** a production budget |
| yt-dlp + proxy CPU | Acceptance uses local mock origins only |

Do not assert production MemoryMax/TasksMax without measurement and owner
decision (same rule as SEC-08 slice template).

## 6. Acceptance matrix (no production, no real metadata)

Native Linux AMD64, real Compose project (`compose.yaml` + overlays), local
mock origin / mock DNS / disposable proxy. One candidate **network** image
built once; native acceptance and Trivy share the same Image ID (same-artifact
pattern from SEC-08). No live provider load tests; no real cloud metadata
probes — use local canaries.

| # | Check | Pass criterion |
| --- | --- | --- |
| N1 | Allowed inspection via proxy | Mock origin metadata returned; bytes/time within caps |
| N2 | Allowed progressive download via proxy | File lands; size ≤ max; publish still worker/fence-owned |
| N3 | Direct egress denied | Tool without usable proxy still cannot reach mock “public” or canary outside `media-net` |
| N4 | Internal service denied | Canary on `fetchnow` (fake Postgres/API) unreachable from tool |
| N5 | Host / gateway / metadata canaries denied | Local canary addresses refused at proxy and unreachable directly |
| N6 | IPv6 non-bypass | Forbidden v6/mapped forms denied; or v6 disabled with proof |
| N7 | Redirect / rebinding | Public→private redirect and DNS flip denied on dial |
| N8 | Proxy down | No direct fallback; op fails |
| N9 | Byte/time/output limits | Oversize/time kill; bounded stdout |
| N10 | Cancel + reap; neighbor continues | Same spirit as SEC-08 lifecycle |
| N11 | Lease/fence loss / restart | No publish of stale output |
| N12 | Offline mux stays netless | `media-executor` still `network_mode: none`; cannot reach proxy/origin |
| N13 | Same-artifact image audit | One Image ID; Trivy 0.74.0 + SEC-03C policy; no ignore-unfixed; no VEX for this image unless owner later says so |

## 7. Implementation scope and order

### 7.1 Stacking (do not create yet)

- Base: current SEC-08 tip `022d83f…` (or later SEC-08 tip if more fixes land).
- Proposed branch name when approved: `codex/security-sec-09-network-isolation`.
- Do **not** merge by bypassing SEC-03C / unfixed residual gates.
- Residual acceptance for executor CVEs: **NOT GRANTED** and not required to
  start SEC-09 design/implementation on a stacked branch, but blocks any claim
  of full media-toolchain security closure / rollout.

### 7.2 One bounded implementation iteration (after approval)

1. `egress-proxy` minimal service + dial policy module (deny-list, CONNECT
   443, fail closed).
2. Compose: `media-net` (`internal: true`), attach proxy + `media-net-executor`.
3. `Dockerfile.media-net-executor` (or equivalent) with pinned yt-dlp; supervisor
   reuse; no compiler; no secrets.
4. Protocol: enable `inspect_metadata` / `download_progressive` on network
   server only; server argv + mandatory `--proxy`.
5. Worker client + default-off flag; fail closed; no silent fallback.
6. Offline `media-executor` left on `network_mode: none`.
7. Native Compose acceptance workflow (exact-commit / owner-triggered) +
   same-artifact audit for the **network** image.
8. Docs: contract + evidence; update SEC-08 “until SEC-09” pointers.

### 7.3 Likely files (expected; not created now)

**New**

- `docs/operations/sec-09-network-isolation-plan.md` (this file)
- `backend/Dockerfile.media-net-executor` (name flexible)
- `compose.media-net-executor.yaml` / proxy compose fragment
- `backend/src/fetchnow/media_egress_proxy/` (or `deploy/egress-proxy/`)
- `backend/tests/media_executor/network_acceptance.py` (or sibling)
- `.github/workflows/sec09-media-net-executor.yml`
- `docs/operations/sec-09-media-net-executor.md` (later evidence)

**Touched**

- `backend/src/fetchnow/media_executor/protocol.py`, `constants.py`, `server.py`,
  `argv.py`, `client.py`
- `backend/src/fetchnow/media_inspection/*`, `downloads/executor.py`,
  `downloads/ytdlp_download_argv.py`, `core/config.py`
- `scripts/compose_contract_check.py`
- `compose.yaml` / staging overlays / `.env.*.example` (opt-in only)
- Offline unit/security tests for protocol and proxy policy

**Not in iteration**

- Main merge, production host apply, VEX/exceptions, SEC-03C policy weakening,
  live provider soak, hostname CDN allowlist platform, CAP_SYS_ADMIN firewall
  inside tool netns, TLS interception.

### 7.4 Deferred improvements

- Proxy hostname/suffix allowlist from measured CDN set.
- Optional split of download vs inspect resource budgets.
- Production MemoryMax/TasksMax sign-off.
- Combining offline+network into one image **if** size/attack-surface review
  favors it (default recommendation: separate network image without ffmpeg).
- Any reopen of SEC-08 Landlock/cgroup questions without a new defect.

### 7.5 Rollback / default-off

- New network flag (or explicit network socket enable) defaults **false**.
- Rollback = flag off → today’s worker yt-dlp path (still not SEC-09-safe, but
  behavior-preserving) **or** fail closed if a stricter “network required”
  mode is later chosen — choose at implementation kickoff; discovery
  recommends **default-off with preserve-today** for the first iteration.
- Offline SEC-08 flag remains independently default-off unless already enabled
  by operator.

### 7.6 Owner approvals required before/with implementation

1. Scope approval of this plan (this document).
2. MVP dial policy = public-unicast + deny-list (vs delay for CDN hostname
   allowlist).
3. Disk headroom acceptance for handoff copies on large downloads.
4. Whether network mode default-off may fall back to in-worker yt-dlp or must
   fail closed when operators enable a “require network executor” switch.
5. Unfixed residual decision remains separate; not granted by SEC-09.

## 8. Open questions and blockers

| ID | Item | Severity |
| --- | --- | --- |
| Q1 | Exact secondary CDN/API hostnames per provider | Open — blocks proxy hostname allowlist, not MVP deny-list |
| Q2 | Runtime proof that yt-dlp never spawns ffmpeg/curl despite PATH | Open — mitigate with network image PATH contents |
| Q3 | Production host egress/firewall beyond Compose | Unknown — not assumed; acceptance is disposable Compose |
| Q4 | SEC-08 unfixed residual (213/47) | Residual acceptance **NOT GRANTED**; stacks but does not clear rollout |
| Q5 | SEC-08 not merged to main | Stacked branch required; no gate bypass to merge |

No discovery blocker prevents writing an implementation scope: MVP can proceed
with deny-list + internal `media-net` + CONNECT policy while Q1 stays deferred.

## 9. Acceptance criteria for calling SEC-09 done (later)

- Network yt-dlp inspection + progressive (and mux-input) downloads run only
  in `media-net-executor`.
- Offline mux/ffprobe remain on netless executor.
- N1–N13 pass on native AMD64 with same-artifact image identity.
- No silent direct fallback; proxy-down fails closed.
- Worker retains publication authority; no tool secrets/DB URL in network
  image.
- Docs record CONNECT residual and unfixed image findings without claiming
  SECURITY PASS / READY FOR ROLLOUT.

## 10. Staging / files touched by this discovery

| Path | Change |
| --- | --- |
| `docs/operations/sec-09-network-isolation-plan.md` | **created** (this file) |
| `docs/operations/sec-08-media-executor.md` | left as pre-existing local modifications (preserved; not rewritten) |
| `docs/operations/sec-08-executor-image-audit.md` | left as pre-existing local modifications (preserved; not rewritten) |

No implementation, dependency bumps, Docker/CI runs, commit, push, PR, merge,
SSH, host/network/production changes, or VEX/exceptions.

## 11. Implementation contract (local stage, 2026-10-10)

This section supersedes §10 “NOT STARTED” for the local worktree only.

### 11.1 Delivered locally (default-off)

| Piece | Path / flag |
| --- | --- |
| Network executor image | `backend/Dockerfile.media-net-executor` (yt-dlp 2026.7.4, no ffmpeg) |
| Egress CONNECT proxy | `backend/src/fetchnow/media_egress_proxy/`, `Dockerfile.media-egress-proxy` |
| Opt-in Compose | `compose.media-net-executor.yaml` (`media-net` internal + proxy on `fetchnow`) |
| Worker flag | `MEDIA_NET_EXECUTOR_ENABLED` default **false**; fail-closed when true |
| Protocol profile | `PROFILE_NETWORK`: inspect/download ops; offline ops rejected |
| Offline image | still `network_mode: none`; copies `format_token` for shared grammar |
| Prepared workflow | `.github/workflows/sec09-media-net-executor.yml` (dispatch / exact native-once subject; attempt 1) |
| Acceptance entrypoint | `backend/tests/media_executor/network_acceptance.py` (N1–N13 + optional Trivy; not executed on Mac) |

### 11.2 Local acceptance (this stage)

Ran offline:

- `ruff` clean on touched SEC-09 modules/tests
- `mypy` clean on egress proxy + net client/protocol/argv/client
- pytest: egress proxy, net protocol, compose overlay, workflow gate, plus
  SEC-08 protocol/worker regressions — **PASS**
- `docker compose … config` renders overlay with `internal: true`, default-off
  flag, no host ports on net/proxy services

**Not** evidence:

- native AMD64 Compose N1–N13
- same-artifact Trivy for network/proxy images
- live provider soak / CDN hostname allowlist
- commit, push, PR, remote CI run, merge, production apply

### 11.3 Explicit residuals

- CONNECT residual and dial-time re-resolution limits remain as discovery §5–6
- SEC-08 unfixed HC residual **213/47 NOT GRANTED** (unchanged)
- Native harness is implemented; authorized Linux AMD64 run still required for
  native PASS / image audit evidence

### 11.4 Rollback

Flag off → worker in-process yt-dlp (behavior-preserving, not SEC-09-safe).
Flag on + proxy/executor down → fail closed (no silent direct internet).

## Verdict

**SEC-09 — CORRECTIVE PASS COMPLETE / LOCAL REGRESSION PASS / READY FOR CODEX REVIEW**

SEC-08 native PASS preserved on base `022d83f…`.
Residual acceptance: **NOT GRANTED**.
Production / TEST / LIVE / SEO: **unchanged**.
Commit / push / PR / remote CI: **NOT DONE** (await authorization).

STOP before commit/push/native run unless owner authorizes the next exact step.
Evidence detail: `docs/operations/sec-09-media-net-executor.md`.
