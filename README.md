# FetchNow

Paste. Fetch. Done.

FetchNow is a portable media-fetch product: a FastAPI API + worker, an Astro static web client, PostgreSQL, and an Nginx gateway — all wired through Docker Compose so the same container shape runs locally and on a small server, then moves to a larger host without rewriting the application.

> **Current status:** foundation stack + URL validation (`POST /api/v1/media/validate`) + **safe outbound probe** (`POST /api/v1/media/probe`) + **wrapper resolve** (`POST /api/v1/media/resolve`) including the Yandex Video Preview → VK/Rutube resolver + **internal media inspection** (PR4) + **durable media-inspection jobs** (PR5) + **durable download execution foundation** (PR6: private artifacts, create/status only; **disabled by default**) + **authenticated private artifact delivery** (PR7: dedicated `delivery` service; **disabled by default**) + **browser download flow** (PR8: static UI orchestration; **`PUBLIC_MEDIA_FLOW_ENABLED=false`**) + **bounded server-side muxing** (PR9: stream-copy ffmpeg for split video/audio; **`MEDIA_MUXING_ENABLED=false`**). There is no transcoding and no permanent storage.

## Architecture

| Service | Role |
|---|---|
| `gateway` | Nginx reverse proxy; only published entrypoint |
| `web` | Astro static site served by Nginx |
| `api` | FastAPI HTTP API (validate/probe/resolve/jobs create+status) |
| `delivery` | Authenticated artifact streaming; read-only artifact mount; shared image (no configured yt-dlp path) |
| `storage-init` | One-shot non-root mkdir/validate of the shared download root (PR9) |
| `worker` | Claims inspection/download jobs when enabled; same Python package |
| `postgres` | PostgreSQL (durable media/download job queues; no Redis) |

Named volumes (project-scoped): `{project}_pgdata` (database), `{project}_tmp` (worker/delivery private artifacts). With local `COMPOSE_PROJECT_NAME=fetchnow` these resolve to `fetchnow_pgdata` / `fetchnow_tmp`. The `api` and `gateway` services do **not** mount the artifact volume.

## Security posture

Security and product boundaries are documented **before** media processing ships:

- User URLs are untrusted input; only `http`/`https` with a provider hostname allowlist are accepted (`POST /api/v1/media/validate`; policy: [URL validation](docs/security/url-validation-policy.md), [ADR 0004](docs/adr/0004-provider-registry-and-dns-validation.md)).
- Outbound HTTP uses a safe client with manual redirects and per-hop re-validation (`POST /api/v1/media/probe`; [ADR 0005](docs/adr/0005-safe-outbound-http-and-redirects.md)).
- Wrapper URL resolution: domain foundation ([ADR 0006](docs/adr/0006-wrapper-resolution-foundation.md)) and Yandex Video Preview resolver + `POST /api/v1/media/resolve` ([ADR 0007](docs/adr/0007-yandex-video-preview-resolver.md)). Validate/probe behavior is unchanged.
- Media inspection foundation: metadata-only VK/Rutube adapter behind a hardened yt-dlp subprocess boundary ([ADR 0008](docs/adr/0008-media-inspection-and-tool-boundary.md)). Disabled by default; no media bytes downloaded; direct media URLs stay internal.
- Durable media-inspection jobs: PostgreSQL queue with client-generated access tokens, `SKIP LOCKED` claims, and lease fencing ([ADR 0009](docs/adr/0009-postgresql-media-job-orchestration.md)). Defaults off; API does not run yt-dlp.
- Durable download execution foundation: progressive download jobs, exact option rebinding, private artifact store ([ADR 0010](docs/adr/0010-durable-download-execution-and-private-artifact-boundary.md)). Defaults off; no client file delivery in PR6.
- Authenticated private artifact delivery: dedicated `delivery` process, parent Bearer auth, FD-based streaming ([ADR 0011](docs/adr/0011-authenticated-private-artifact-delivery.md)). Defaults off; API does not mount artifact storage; delivery never invokes yt-dlp and receives no tool path (shared image still contains the binary; DB role is not read-only).
- Browser download flow: static Astro orchestration of inspection → download → File System Access streaming save ([ADR 0012](docs/adr/0012-browser-download-flow.md), [browser flow](docs/api/browser-download-flow.md)). UI flag `PUBLIC_MEDIA_FLOW_ENABLED` defaults false and does not enable server flags.
- Bounded muxing: worker-only stream-copy of compatible video-only + audio-only streams ([ADR 0013](docs/adr/0013-bounded-media-muxing.md)). `MEDIA_MUXING_ENABLED` defaults false; ffmpeg/ffprobe paths never reach API, delivery, or web.
- Threat coverage for SSRF, redirects, tool abuse, and resource exhaustion: [Threat model](docs/security/threat-model.md).
- Production logs must not contain full source URLs, cookies, or secrets: [Logging and privacy](docs/security/logging-and-privacy.md).
- Capacity limits are environment-driven and fail closed: [Capacity policy](docs/operations/capacity-policy.md).
- Public error namespace: [Error codes](docs/api/error-codes.md).
- Why security docs preceded media tools: [ADR 0003](docs/adr/0003-security-boundaries-before-media-processing.md).

The web UI uses a **system font stack only** (no Google Fonts CDN).

## Documentation map

| Doc | Topic |
|---|---|
| [MVP scope](docs/product/mvp-scope.md) | Planned product boundaries |
| [Product policy](docs/product/product-policy.md) | What FetchNow will and will not do with content |
| [Abuse and copyright process](docs/product/abuse-and-copyright-process.md) | Intake process and `ABUSE_CONTACT_EMAIL` |
| [File lifecycle policy](docs/product/file-lifecycle-policy.md) | Temp file states and TTL |
| [ADR 0001](docs/adr/0001-monorepo-and-stack.md) | Monorepo and stack |
| [ADR 0002](docs/adr/0002-deployment-portability.md) | Portability |
| [ADR 0003](docs/adr/0003-security-boundaries-before-media-processing.md) | Security before media tools |
| [ADR 0004](docs/adr/0004-provider-registry-and-dns-validation.md) | Provider registry + DNS order |
| [ADR 0005](docs/adr/0005-safe-outbound-http-and-redirects.md) | Safe outbound HTTP + redirects |
| [ADR 0006](docs/adr/0006-wrapper-resolution-foundation.md) | Wrapper resolution foundation |
| [ADR 0007](docs/adr/0007-yandex-video-preview-resolver.md) | Yandex Video Preview resolver |
| [ADR 0008](docs/adr/0008-media-inspection-and-tool-boundary.md) | Media inspection + yt-dlp tool boundary |
| [ADR 0009](docs/adr/0009-postgresql-media-job-orchestration.md) | PostgreSQL media-job orchestration |
| [ADR 0010](docs/adr/0010-durable-download-execution-and-private-artifact-boundary.md) | Durable download execution + private artifacts |
| [ADR 0011](docs/adr/0011-authenticated-private-artifact-delivery.md) | Authenticated private artifact delivery |
| [ADR 0012](docs/adr/0012-browser-download-flow.md) | Browser orchestration + streaming save |
| [Browser download flow](docs/api/browser-download-flow.md) | Client sequence and constraints |
| [Browser support](docs/product/browser-support.md) | File System Access API boundary |

## Validate API (PR1)

```bash
curl -sS -X POST http://localhost:8080/api/v1/media/validate \
  -H 'content-type: application/json' \
  -d '{"url":"<PUBLIC_VK_URL>"}'
```

Success returns provider id/displayName and a query-stripped canonical URL. Errors use the stable error envelope (`INVALID_URL`, `UNSUPPORTED_PROVIDER`, `BLOCKED_DESTINATION`, …).

## Probe API (PR2)

```bash
curl -sS -X POST http://localhost:8080/api/v1/media/probe \
  -H 'content-type: application/json' \
  -d '{"url":"<PUBLIC_VK_URL>"}'
```

Diagnostic only: provider, final URL without query, and limited HTTP metadata (`status`, `contentType`, `contentLength`, `redirectCount`, `methodUsed`). Does not extract media metadata or download files. Some providers may fall back from HEAD to a bounded GET per provider policy.

## Resolve API (PR3B)

```bash
curl -sS -X POST http://localhost:8080/api/v1/media/resolve \
  -H 'content-type: application/json' \
  -d '{"url":"<PUBLIC_VK_OR_YANDEX_PREVIEW_URL>"}'
```

Resolves a direct supported provider URL or a registered wrapper (currently
exact `https://yandex.ru/video/preview/<digits>`) to a query-stripped canonical
provider URL. Returns `provenance`, optional `wrapperType`, and a sanitized
resolution chain. Does not download media.

## Media inspection (PR4, internal)

Internal `MediaInspectionService` turns a `ResolutionResult` into bounded
`MediaMetadata` / quality options via a metadata-only yt-dlp adapter. No public
HTTP route in this PR. The tool is disabled by default
(`MEDIA_INSPECTION_ENABLED=false`). Manual live smoke:

```bash
FETCHNOW_LIVE_MEDIA_INSPECTION=1 \
  MEDIA_INSPECTION_ENABLED=true \
  MEDIA_INSPECTION_YTDLP_PATH=/absolute/path/to/yt-dlp \
  backend/.venv/bin/python backend/scripts/media_inspection_live_smoke.py \
  '<PUBLIC_VK_OR_RUTUBE_OR_YANDEX_PREVIEW_URL>'
```

Does not download media. Staging deployment of this path is not performed in PR4.

## Media inspection jobs (PR5)

Durable PostgreSQL-backed jobs enqueue metadata inspection after URL resolution.
Feature flags default **off** (`MEDIA_JOBS_ENABLED=false`,
`MEDIA_INSPECTION_ENABLED=false`).

```bash
# Client generates a 32-byte token (base64url, 43 chars) and keeps it.
curl -sS -X POST http://localhost:8080/api/v1/media/jobs \
  -H 'content-type: application/json' \
  -H 'authorization: Bearer <TOKEN>' \
  -d '{"url":"<PUBLIC_VK_OR_RUTUBE_OR_YANDEX_PREVIEW_URL>"}'

curl -sS http://localhost:8080/api/v1/media/jobs/<JOB_ID> \
  -H 'authorization: Bearer <TOKEN>'
```

API resolves/validates and persists a trusted terminal target only; it does **not**
run yt-dlp. The worker claims jobs with `SKIP LOCKED` and may run PR4 inspection
when both flags are enabled. See [ADR 0009](docs/adr/0009-postgresql-media-job-orchestration.md).

## Infrastructure Handbook

Подробный русскоязычный учебник и staging runbook находится в
[Infrastructure Handbook](docs/infrastructure/README.md). Он объясняет Linux,
Docker/Compose, Nginx, TLS, PostgreSQL, deployment, диагностику, backup,
rollback и перенос на новый сервер; фактическое состояние сервера всегда
подтверждается preflight-проверками.

## Prerequisites

- Docker Engine + Docker Compose v2
- Python 3.12 (for local backend tooling)
- Node.js 22+ (for local web tooling)

## Quick start

```bash
make setup          # local Python venv + npm install
make up             # build and start the full Compose stack
make migrate        # apply Alembic migrations (explicit; not on API boot)
```

Open the gateway:

- Frontend: http://localhost:8080/ (from `compose.override.yaml`)
- Liveness: http://localhost:8080/api/v1/health/live
- Readiness: http://localhost:8080/api/v1/health/ready

Base `compose.yaml` does not publish host ports. Staging uses `compose.staging.yaml` with loopback `127.0.0.1:8091` (see Infrastructure Handbook ch. 06).

## Common commands

```bash
make logs           # follow Compose logs
make down           # stop containers (volumes preserved; never use -v routinely)
make lint           # Ruff + ESLint
make format         # Ruff format/fix + Prettier
make typecheck      # mypy + astro check
make test           # pytest
make compose-check  # Compose base/dev/staging isolation contract
make pg-backup-test # PostgreSQL backup unit tests (PRD1B)
make release-test   # release preflight/health unit tests (PRD1C1)
make build          # web build + Compose image build
make check          # lint + typecheck + test + compose-check + web build
make migration m="add something"
```

`make compose-check` (and therefore full `make check`) requires Docker Compose v2 (`docker compose config`). It does not start containers or mutate volumes, but the Compose CLI normally needs a reachable Docker daemon.

Staging PostgreSQL backup/restore-verify tooling: `make pg-backup-create|verify|list|prune-dry` (see Infrastructure Handbook ch. 15). Isolated integration: `make pg-backup-integration` (unique `fetchnow-backup-test-<suffix>` project only).

Release identity / read-only preflight & health (PRD1C1): `make release-preflight|release-health EXPECTED_REVISION=<40-char-sha> ENV_FILE=... DEPLOY_ROOT=...`. Preflight requires the revision to already be contained in local `origin/main` and does not fetch. Protected staging health reads schema-v2 `current.json` and validates exact immutable image IDs. Omitting `DEPLOY_ROOT` is allowed only for validated `fetchnow-health-test-*` projects; protected names (`fetchnow-staging`, `fetchnow-prod`, …) fail closed without managed mode. External env files are consumed in place—no repository-local secret copy or symlink is required. A newer tooling checkout may recheck an older deployed revision without rebuild/restart. Exact-revision materialization & image build (PRD1C2): `make release-prepare EXPECTED_REVISION=<sha> ENV_FILE=... DEPLOY_ROOT=...` and `make release-verify ...` (no compose up). PRD1C3A adds application-only `make release-rollout ... [BOOTSTRAP=1]` and explicit `make release-recover ...`; it uses immutable image IDs and never performs a DB migration. Isolated transaction coverage: `make release-rollout-integration`. See Infrastructure Handbook ch. 24–26; repository tooling does not claim that this hotfix is deployed to staging.

## Configuration

All runtime configuration is environment-driven. Templates:

- `backend/.env.example`
- `web/.env.example`
- root `.env.example` (local Compose knobs; set `COMPOSE_PROJECT_NAME=fetchnow`)
- `.env.staging.example` → gitignored `.env.staging` for staging

Compose injects service environment in `compose.yaml`. Local overrides live in `compose.override.yaml` (auto-loaded). Staging uses explicit `-f compose.yaml -f compose.staging.yaml` with `--project-name fetchnow-staging`. An optional production-shaped fragment remains in `deploy/compose/compose.prod.yaml`.

## Repository layout

See the monorepo tree under `backend/`, `web/`, `deploy/`, and `docs/`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).
