# ADR 0002: Deployment portability

- Status: Accepted
- Date: 2026-08-03

## Context

FetchNow will launch on a limited host and later move to a dedicated server with more CPU/RAM and a larger SSD. Rewriting application code for that move is unacceptable.

## Decision

1. **No hostname or IP coupling.** Services discover each other through Compose DNS names (`postgres`, `api`, `web`). Public URLs come from environment (`PUBLIC_SITE_URL`, gateway port). Application code never hardcodes host addresses.

2. **Environment owns paths, limits, and concurrency.** Database URL, temp storage path, temp byte budget, API concurrency, and worker concurrency are settings. Changing capacity is an ops change.

3. **Persistent state lives only in PostgreSQL and the storage volume/provider.** Containers are disposable. Project-scoped named volumes (`{project}_pgdata`, `{project}_tmp`) carry durable/ephemeral data across recreations. Explicit global volume `name:` fields are forbidden so distinct Compose projects cannot silently share storage.

4. **Local disk is the first storage implementation, not a business-logic dependency.** Future PRs introduce a storage boundary so media code talks to an interface. This PR does not invent that interface early.

5. **Production migration is infrastructure, not application code.** Moving hosts means copying/restoring volumes (or pointing at a new provider), adjusting Compose/env resource limits, and pointing DNS at the new gateway. The same images and package versions run.

## Consequences

- Secrets and environment-specific values stay out of the image and out of git.
- API startup does not run migrations; operators apply them explicitly (`make migrate`).
- Scaling knobs are documented beside Compose, not buried in source constants.
