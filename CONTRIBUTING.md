# Contributing to FetchNow

## Principles

- Keep PRs small and production-shaped: ship real structure, not speculative abstractions.
- Configuration belongs in environment variables, not hardcoded hostnames or paths.
- Do not auto-run migrations on API startup; use `make migrate`.
- Persistent state belongs in PostgreSQL and the storage volume/provider — not in application containers.

## Local workflow

1. `make setup`
2. `make up`
3. `make migrate`
4. `make check` before opening a PR

## Backend

- Package lives in `backend/src/fetchnow`.
- Use the application factory in `fetchnow.api.main`.
- Lint with Ruff, typecheck with mypy (strict), test with pytest + pytest-asyncio.

## Web

- Astro static output only for now (no React/Vue/Svelte in this foundation).
- Lint with ESLint, format with Prettier, typecheck with `astro check`.

## Docker

- Prefer Compose for integration verification.
- Images must run as non-root where practical (PostgreSQL official image is the known exception for its entrypoint model).
- Never commit secrets or production credentials.

## Documentation

- Architecture decisions go in `docs/adr/`.
- Product scope updates go in `docs/product/`.
