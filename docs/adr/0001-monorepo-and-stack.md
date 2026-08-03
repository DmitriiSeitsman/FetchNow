# ADR 0001: Monorepo and stack

- Status: Accepted
- Date: 2026-08-03

## Context

FetchNow starts on a constrained server and must later move to a larger host with more SSD without rewriting the application. The first deliverable needs a production-shaped foundation for an API, a background worker, a static web client, PostgreSQL, and an Nginx gateway.

## Decision

### Why a monorepo

API, worker, web client, gateway config, and Compose definitions ship together. Shared versioning and a single CI graph keep the portable deployment unit coherent. Split repositories would force cross-repo version pinning before any product value exists.

### Why FastAPI

FastAPI gives typed request/response models, async I/O for future outbound provider calls, and a clear application-factory pattern. It pairs cleanly with SQLAlchemy 2 async, Pydantic Settings, and uvicorn workers.

### Why Astro

The public surface is mostly static marketing/UI with progressive enhancement later. Astro’s static output needs no Node runtime in production, keeps the client thin, and avoids committing to React/Vue/Svelte before interaction complexity requires it.

### Why Docker from PR0

Containers are the portability boundary. Local Compose uses the same process split (api, worker, postgres, web, gateway) expected on a small VPS and later on a larger server. Host differences become Compose/env/volume changes, not application rewrites.

### Why API and worker are separate processes

HTTP latency and background media work have different failure and scaling profiles. One Python package, two commands (`fetchnow-api`, `fetchnow-worker`) lets us scale or restart workers independently without coupling request threads to long jobs.

### Why PostgreSQL as the future job queue

A second broker (Redis) adds another stateful dependency on a small server. PostgreSQL is already required for durable state; a later SKIP LOCKED / `FOR UPDATE` job table keeps operations simple. Throughput limits on day one are acceptable; the queue can move only if evidence demands it.

### Why temporary storage will hide behind a StorageProvider later

Local disk is the first implementation on a single host. Business logic must not import filesystem paths directly so a later SSD volume or remote object store is an infrastructure swap. The interface is intentionally deferred until media processing lands — inventing unused abstractions now is out of scope.

### Portability to a stronger server

Scale-up means larger Compose resource limits, a bigger PostgreSQL volume, and a larger temp volume (or storage provider). Application code stays the same: env-driven concurrency/limits, no hostname coupling, state only in PostgreSQL + storage.

## Consequences

- Compose is the primary local and small-server runtime.
- Redis and Kubernetes are explicitly deferred.
- Media pipeline code is absent in this PR; only health, logging, config, and worker lifecycle exist.
