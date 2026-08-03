# FetchNow

Paste. Fetch. Done.

FetchNow is a portable media-fetch product: a FastAPI API + worker, an Astro static web client, PostgreSQL, and an Nginx gateway — all wired through Docker Compose so the same container shape runs locally and on a small server, then moves to a larger host without rewriting the application.

## Architecture

| Service | Role |
|---|---|
| `gateway` | Nginx reverse proxy; only published entrypoint |
| `web` | Astro static site served by Nginx |
| `api` | FastAPI HTTP API |
| `worker` | Separate process from the same Python package |
| `postgres` | PostgreSQL (future job queue lives here; no Redis) |

Named volumes: `fetchnow_pgdata` (database), `fetchnow_tmp` (future temporary media files).

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

Compose injects service environment in `compose.yaml`. Local overrides live in `compose.override.yaml` (auto-loaded). A production-shaped fragment is in `deploy/compose/compose.prod.yaml`.

## Repository layout

See the monorepo tree under `backend/`, `web/`, `deploy/`, and `docs/`. Design decisions are recorded in `docs/adr/`; MVP product boundaries are in `docs/product/mvp-scope.md`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).
