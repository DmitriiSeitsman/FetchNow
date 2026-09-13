# ADR 0020: successful-delivery quota accounting

## Status

Accepted for PRD2-A4.2.

## Context

The original Free quota transition treated worker publication (`READY`) as a
successful download. Publication only proves that FetchNow prepared an artifact;
the user may never request it, a response may disconnect, or a resumed transfer
may need several HTTP ranges. Charging at READY therefore counted undelivered
work and made recoverable delivery retries ambiguous.

## Decision

PostgreSQL is the accounting authority. For one Free quota entry and the exact
`download_job + artifact_id + fence_token + artifact_bytes` generation, a unit
is consumed the first time persisted server-observed interval coverage equals
the half-open interval `[0, artifact_bytes)`. Coverage is the normalized union,
never a sum, so replayed and overlapping ranges cannot inflate it. A full 200,
a full-file 206, or several resume requests may complete the union. HEAD,
malformed ranges, 416 responses, and empty attempts contribute nothing.

A chunk becomes observed only after it has been yielded to ASGI and the async
generator is resumed. This is a practical server-delivery proxy. It is not
cryptographic proof that every byte reached the client, nor proof that the
browser successfully persisted or closed a local file. The browser never sends
an accounting acknowledgement.

The bounded ledger stores at most 16 active attempts and 64 normalized closed
fragments per quota entry. An active row has a 600-second renewable lease. A
closed row retains only the actually observed prefix, including after
disconnect, exception, early EOF, process restart, or lease expiry. Requests
that cannot be represented are rejected before response bytes are sent.

Begin, checkpoint, finalize, and reconcile are separate short transactions in
the lock order anonymous client → download job → quota entry → ledger rows. No
session or lock survives while media is streamed or paced. Checkpoints occur at
approximately 8 MiB or 30 seconds, whichever comes first. A required checkpoint
failure stops the stream, leaving the last durable prefix. Finalization uses a
fresh cancellation-shielded transaction with a 15-second bound. A crash can lose
only the bounded interval since the last checkpoint; reconciliation closes stale
attempts and consumes complete durable coverage.

## Compatibility and rollout

`FREE_DOWNLOAD_QUOTA_READY_COMPATIBILITY_MODE=true` is the fail-safe default.
It preserves READY-time consumption while old and new revisions coexist. The
ledger migration is additive and empty; consumed entries remain consumed and
reserved entries are not backfilled or refunded. A missing key uses `true`; an
invalid boolean fails application/config-contract validation instead of silently
activating the new accounting mode.

Activation order is: migrate to `0010_successful_delivery_quota`, roll out the
new application everywhere with compatibility `true`, verify delivery evidence,
then use the canonical config-rollout transaction to set the flag to `false` for
api, delivery, and worker together. No other quota or runtime value changes.

Rollback order is the reverse: first restore the flag to `true` with canonical
config rollout, verify health, then roll back the application if needed. The
database is not downgraded; revision 0010 is compatible with the previous
application. Never roll an old READY-consuming worker alongside a new worker
running with the flag disabled.

## Consequences

An active lease prevents expiry cleanup from removing the artifact or releasing
its reservation, while new requests after the original job TTL remain denied.
A crashed stream delays cleanup only until its bounded lease expires. Concurrent
completion serializes through the existing quota entry and consumes exactly
once. Premium jobs and quota-disabled legacy jobs do not require Free evidence.
