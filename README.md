# FetchNow

Paste. Fetch. Done.

FetchNow is a portable media-fetch product: a FastAPI API + worker, an Astro static web client, PostgreSQL, and an Nginx gateway — all wired through Docker Compose so the same container shape runs locally and on a small server, then moves to a larger host without rewriting the application.

> **Current status:** the foundation stack (health checks, worker lifecycle, static landing page, Compose gateway) is running. **FetchNow does not yet process real media URLs.** There is no yt-dlp/ffmpeg pipeline, job queue, provider integration, or download delivery in the tree. Planned MVP providers (for example VK and Rutube) are **product scope**, not implemented features — do not treat them as live support.

## Architecture

| Service | Role |
|---|---|
| `gateway` | Nginx reverse proxy; only published entrypoint |
| `web` | Astro static site served by Nginx |
| `api` | FastAPI HTTP API |
| `worker` | Separate process from the same Python package |
| `postgres` | PostgreSQL (future job queue lives here; no Redis) |

Named volumes: `fetchnow_pgdata` (database), `fetchnow_tmp` (future temporary media files).

## Security posture

Security and product boundaries are documented **before** media processing ships:

- User URLs are untrusted input; only `http`/`https` with a provider hostname allowlist will be accepted (spec: [URL validation policy](docs/security/url-validation-policy.md)).
- Threat coverage for SSRF, redirects, tool abuse, and resource exhaustion: [Threat model](docs/security/threat-model.md).
- Production logs must not contain full source URLs, cookies, or secrets: [Logging and privacy](docs/security/logging-and-privacy.md).
- Capacity limits are environment-driven and fail closed: [Capacity policy](docs/operations/capacity-policy.md).
- Public error namespace: [Error codes](docs/api/error-codes.md).
- Why this order: [ADR 0003](docs/adr/0003-security-boundaries-before-media-processing.md).

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

- Frontend: http://localhost:8080/ (Compose override also maps host `8080`)
- Liveness: http://localhost:8080/api/v1/health/live
- Readiness: http://localhost:8080/api/v1/health/ready

With only `compose.yaml` (no override), the gateway publishes host port `80` by default (`GATEWAY_PORT`).

## Common commands

```bash
make logs           # follow Compose logs
make down           # stop containers (volumes preserved)
make lint           # Ruff + ESLint
make format         # Ruff format/fix + Prettier
make typecheck      # mypy + astro check
make test           # pytest
make build          # web build + Compose image build
make check          # lint + typecheck + test + web build
make migration m="add something"
```

## Configuration

All runtime configuration is environment-driven. Templates:

- `backend/.env.example`
- `web/.env.example`
- root `.env.example` (Compose-oriented knobs, including capacity and `ABUSE_CONTACT_EMAIL`)

Compose injects service environment in `compose.yaml`. Local overrides live in `compose.override.yaml` (auto-loaded). A production-shaped fragment is in `deploy/compose/compose.prod.yaml`.

## Repository layout

See the monorepo tree under `backend/`, `web/`, `deploy/`, and `docs/`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).
