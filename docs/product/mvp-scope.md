# FetchNow MVP scope

## In scope

- **Public links only.** Users paste publicly reachable media URLs.
- **First providers:** VK and Rutube.
- **Free quality:** up to 720p without payment.
- **Turbo:** time-limited boost (24 hours) for faster/higher-priority processing — details in a later PR.
- **No registration.** No accounts, no passwords, no OAuth in the MVP.
- **Download modes:** direct URL handoff when possible; server-processed fetch when the provider requires it.
- **No permanent media storage.** Temporary files exist only for processing/delivery and are deleted afterward.

## Out of MVP

- Arbitrary private or authenticated media sources
- Long-term media library / cloud locker
- User profiles, history accounts, or social login
- Mobile native apps
- Multi-region active-active deployment
- Kubernetes / Terraform automation
- Redis-backed queues
- Adult-content specialized workflows beyond baseline abuse controls (later)

## Foundation note

- **PR0A:** monorepo, health endpoints, worker lifecycle, static landing page, Compose gateway.
- **PR0B:** product/security/capacity policies, URL security fixtures, error-code registry.
- **PR1:** executable URL validation + VK/Rutube provider registry + `POST /api/v1/media/validate` (no media download).
- ffmpeg/yt-dlp pipelines, payments, Turbo entitlements, and job tables arrive in subsequent PRs.
