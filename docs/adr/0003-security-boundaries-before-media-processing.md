# ADR 0003: Security boundaries before media processing

- Status: Accepted
- Date: 2026-08-03

## Context

FetchNow will eventually fetch user-supplied URLs with tools such as yt-dlp and ffmpeg. Those tools are powerful, speak complex URL/HTTP semantics, and historically attract SSRF, argument-injection, and resource-exhaustion bugs. PR0A shipped process shape only. Before any media pipeline code, the project needs explicit security and product boundaries.

## Decision

### Fix security policy before yt-dlp

Implementing fetch before policy creates irreversible habits: logging full URLs, shelling out with string commands, trusting redirects, and treating “it works on VK” as “any host is fine.” PR0B records threat model, URL rules, logging redaction, capacity fail-closed behavior, and error codes so later PRs implement against a checklist instead of inventing controls under delivery pressure.

### User URLs are untrusted input

A pasted link is attacker-controlled data equal to a form field or HTTP header. It may encode credentials, target link-local metadata, abuse redirects, or carry shell metacharacters. No URL is trusted because the user “meant well.”

### Provider allowlist is mandatory

An open HTTP proxy is an SSRF and abuse engine. FetchNow only attempts hosts that are explicitly allowlisted with label-boundary-safe matching. Supporting a platform later means adding named hosts after review — not “allow the whole internet.”

### Clients must not control yt-dlp/ffmpeg arguments

Exposing flags, output templates, or config paths is remote code execution in practice (`--exec`, arbitrary files, network options). The product accepts a URL (and later simple quality choices). All tool argv are server-defined.

### Subprocess without shell

Media tools must be spawned with argument vectors, never `shell=True` or `/bin/sh -c` concatenating user data. This is a hard engineering rule in CONTRIBUTING.

### API and worker are different trust boundaries

The API authenticates/validates and enqueues; the worker performs dangerous I/O. Compromise of a worker’s tool stack should not imply unrestricted API secret access, and API request handlers should not run ffmpeg inline. Isolation limits blast radius and scaling coupling (see ADR 0001).

### Output storage is outside the public web root

Prepared files must not be served as static files from the Astro/Nginx document root. Delivery uses controlled, short-lived authorization separate from guessable paths. Public web roots are for the marketing/UI surface only.

## Consequences

- PR0B is mostly documentation, fixtures, and env placeholders — by design.
- Media PRs that violate these boundaries are out of policy and should be rejected in review.
- Residual risk remains (tool zero-days, TOCTOU DNS); mitigations are tracked in `docs/security/threat-model.md`.
