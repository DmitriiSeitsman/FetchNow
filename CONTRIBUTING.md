# Contributing to FetchNow

## Principles

- Keep PRs small and production-shaped: ship real structure, not speculative abstractions.
- Configuration belongs in environment variables, not hardcoded hostnames or paths.
- Do not auto-run migrations on API startup; use `make migrate`.
- Persistent state belongs in PostgreSQL and the storage volume/provider — not in application containers.
- Security policy docs and fixtures land **before** media-processing code (see `docs/adr/0003-security-boundaries-before-media-processing.md`).

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
- Do not add remote font CDNs (`fonts.googleapis.com`, `fonts.gstatic.com`, or equivalents). Use the system font stack.

## Security requirements for contributors

- **URL handling:** any change to URL parsing, normalization, DNS/redirect checks, or provider host matching **must** update `backend/tests/fixtures/url_security_cases.json` (and keep loader tests green). Do not “fix” a case by deleting it without review.
- **New providers:** require an **explicit** hostname allowlist entry with label-boundary-safe matching. Naive `endswith("example.com")` is forbidden.
- **Subprocess:** never invoke a shell with user-controlled data (`shell=True`, `/bin/sh -c`, string-built command lines). Media tools must use argv arrays defined server-side.
- **Logging:** never log full source URLs (including query tokens), cookies, authorization headers, payment secrets, or download URLs. Follow `docs/security/logging-and-privacy.md`.
- **Client input:** clients must not supply yt-dlp/ffmpeg arguments — only URLs and future product-level options.

## Docker

- Prefer Compose for integration verification.
- Images must run as non-root where practical (PostgreSQL official image is the known exception for its entrypoint model).
- Never commit secrets or production credentials. Keep `ABUSE_CONTACT_EMAIL` empty in examples until a real mailbox exists.

## Documentation

- Architecture decisions go in `docs/adr/`.
- Product scope and policy updates go in `docs/product/`.
- Security specs go in `docs/security/`.
- Operational capacity limits go in `docs/operations/`.
- Public API error codes go in `docs/api/error-codes.md` before clients depend on new codes.
