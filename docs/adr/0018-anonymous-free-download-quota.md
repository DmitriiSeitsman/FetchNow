# ADR 0018: anonymous Free download quota

Status: Accepted for PRD1E-B2

## Context

FetchNow has no accounts, but Free download work needs a best-effort daily
entitlement that survives tab reloads and cannot be exceeded by ordinary
parallel requests. A successful prepared file should count; provider failure,
cancellation, and pre-ready expiry should not. Existing per-flow Bearer tokens
and short-lived browser delivery grants are intentionally unsuitable as a
stable entitlement identity.

## Decision

Migration `0007_free_download_quota` additively creates `anonymous_clients` and
`free_download_quota_entries`. The browser first calls
`GET /api/v1/media/quota`. The server creates 32 random bytes, returns them as a
43-character unpadded base64url `__Host-fetchnow_client` cookie, and persists
only `SHA-256("fetchnow:v1:anonymous-client\\0" || raw_bytes)`. Cookie flags are
Path=/, Secure, HttpOnly, and SameSite=Lax.

Identity expiry is an absolute one year from creation. It is not extended by
activity. Database expiry and cookie Max-Age/Expires agree. Bounded worker
maintenance removes terminal accounting after the configured retention (48
hours by default) and then removes expired identities without entries.

Effective Free policy is three consumed successes in a rolling 86,400-second
window. All correctness timestamps come from PostgreSQL. Admission locks the
anonymous-client row, counts consumed events in the window plus live
reservations, and inserts one reservation in the same transaction as a newly
inserted download job. The existing unique `(media_job_id, format_option_id)`
key resolves idempotent concurrency; a unique quota `download_job_id` gives an
additional one-reservation invariant.

The lock order is: parent media job if a path explicitly locks it, anonymous
client, media download job, quota entry. Unlocked reads may discover the client
id needed to acquire this prefix. API admission, worker terminal transitions,
cancellation, expiry, cleanup, and reconciliation never acquire these row locks
in reverse order. Advisory locks are not used.

A fenced `ready` job transition and reservation `consumed` transition commit in
one PostgreSQL transaction. Permanent failure, stored cancellation, exhausted
lease failure, and pre-ready expiry atomically release the reservation. Retry
keeps it reserved. Reconciliation repairs reserved entries from authoritative
job state and runs regardless of the current admission flag.

`FREE_DOWNLOAD_QUOTA_ENABLED` belongs to API admission only. It defaults false.
Disabling it after reservations exist must not prevent worker finalization or
repair. The safe rollout is code/schema with false, canonical migration and
verification, then a separate operator activation with limit 3 and window
86,400. Search indexing remains disabled.

Status exposes only tier, limit, used, reserved, remaining, and nullable reset.
Reservations reduce remaining but do not create a reset promise. `resetAt` is
published only when consumed entries alone exhaust the limit and equals the
oldest currently counted success plus the rolling window.

## Consequences

Parallel distinct admission is serialized per anonymous identity without a
global bottleneck. Job/quota atomicity prevents successful files from being
released or terminal failures from burning quota. Rows add modest PostgreSQL
write and retention load, bounded by admission and cleanup.

This is not an anti-fraud identity. Cookie deletion, private browsing, and a
different browser or device can obtain another allowance. Stolen cookies remain
usable until absolute expiry. FetchNow accepts those residual bypasses instead
of IP entitlements or invasive fingerprinting. B3 delivery rate limiting,
Premium, payments, and separate audio/video products remain out of scope.

## Related

- [Free-tier download policy](../product/free-tier-download-policy.md)
- [Browser download flow](../api/browser-download-flow.md)
- [Logging and privacy](../security/logging-and-privacy.md)
- [Threat model](../security/threat-model.md)
