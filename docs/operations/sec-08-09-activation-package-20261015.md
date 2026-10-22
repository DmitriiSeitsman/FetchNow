# SEC-08/09 — activation package (local preparation only)

**Status: PREPARED FOR CODEX REVIEW / NOT INSTALLED / NOT APPROVED FOR PRODUCTION.**

HEAD `de54d9265af3febb8826f4ca127cb76aa91e4e88`. No commit/push/SSH/deploy in this
pass. Owner residual decision remains **APPROVED / PENDING_TECHNICAL_PREREQUISITES**
(expires `2026-10-29T20:18:03Z`). Gosu VEX remains saved-audit-only.

## Consumer map (Workstream A)

| Consumer | Role | Consumes approvals? |
| --- | --- | --- |
| `scripts/image_audit.py` | Canonical four-target CI gate | **No** |
| `scripts/image_audit_candidate7.py` | Seven-target audit snapshot | **No** (raw FAIL preserved) |
| `.github/workflows/ci.yml` image-audit | Canonical four-target | **No** |
| `.github/workflows/sec03c-candidate7-image-audit.yml` | Candidate-7 native audit | **No** (explicitly does not load approvals) |
| `scripts/image_audit_gosu_vex_apply.py` | Offline gosu layer on saved audit | Yes, saved-audit only |
| `scripts/image_audit_candidate7_residual_apply.py` | Offline residual layer | Yes, fail-closed pending prerequisites |
| `scripts/image_audit_candidate7_policy_layers.py` | Offline three-layer composition | Yes, never CI auto-green |

Layers stay named separately: raw scanner → scoped gosu `not_affected` →
owner-accepted residual risk. Accepted risk is never “0 vulnerabilities”.
Scanner/identity/source failures remain non-zero. New Image IDs produce
`approval_scope_mismatch`, not inheritance.

## Recommended resource budget (NOT APPROVED)

File: `docs/operations/sec-08-09-resource-budget-proposal.json` (`approved: false`).
Supersedes the 2026-10-15 overcommit draft.

| Field | Proposed value |
| --- | --- |
| Aggregate slice | `fetchnow-media-executor.slice` **2176M** MemoryMax, **100%** CPUQuota, TasksMax **512**, MemorySwapMax 0 |
| Per-container | media-executor **768M**, media-net-executor **1152M**, egress-proxy **256M** |
| Concurrency | **1** per executor (`MEDIA_EXECUTOR_MAX_JOBS`); offline and network may still run concurrently |
| Shared ceiling | **false** (aggregate == sum of the three limits; no silent overcommit) |
| Disk | dedicated FS **39728447488** bytes (~37 GiB); see storage contract |

`egress-proxy` must share `cgroup_parent` with both executors (static overlay + generated settings + post-start proof).
Existing app-facing deploy limits remain ~2.375 GiB outside the media slice.

## Deny-set proposal (from 2026-10-15 inventory — NOT INSTALLED)

Host public: `195.209.215.87/32`. Neighbor/docker bridges observed:

- `172.17.0.0/16` (docker0)
- `172.18.0.0/16` (`fetchnow-production_fetchnow`)
- `172.19.0.0/16` (`xogame-backend_default`)
- `172.20.0.0/16` (`amhelper_default`)

Proposed `EGRESS_DENY_*` seeds (exact wiring remains the existing proxy policy
path; values must be re-validated at install time): host public /32, the four
bridge subnets above, link-local, and loopback. Public `:443` CONNECT opacity
remains explicitly accepted, not “blocked”. **Not installed.**

## Disk consequence boundary (Workstream C)

**Not a hard quota today:** periodic size sampling (~0.2s) + final post-reap
check detect overshoot and can fail the attempt; they do **not** prevent
writes already committed to the shared root ext4 (~88.69 GiB free at snapshot).
Free capacity, protocol `max_bytes`, and final reject are **not** hard
shared-disk damage bounds for neighbors.

**Recommended single approach (separate operational/implementation scope — not
done here):** place media work roots on a dedicated filesystem or project
quota (ext4 project quota / separate mount) owned by the media path, sized to
the approved peak reservation, so malicious/overshooting writers cannot fill
the root volume used by PostgreSQL and neighbors. Until that exists, shared-disk
outage remains an **unaccepted** residual per owner limits.

## UNKNOWN callers — libxml2 / libacl (limited)

| Package | Audited HC locations | Confirmed | Still UNKNOWN |
| --- | --- | --- | --- |
| `libxml2` | api + media-executor (8 IDs / 16 instances) | No direct `lxml`/libxml Python dep in `pyproject`/`uv.lock`; no product source hits for xmlParse/etree/lxml | Whether ffmpeg/`libavformat` or other native deps in those **exact** Image IDs link and feed attacker bytes into vulnerable symbols; whether worker/API/root supervisor/proxy invoke them outside tool sandbox |
| `libacl1` | all four residual images (CVE-2026-54369) | No product source hits for `acl_*` / setfacl | Whether any privileged native pathname-based ACL caller exists in those images |

Exact audited Image IDs were **absent** from the local Docker store and from
production inventory; no disposable rebuild was substituted for their identity.
Absence of a Python string is **not** call-graph proof.

## Argv gap fix (runtime input change)

Both mux builders now emit `-protocol_whitelist file` immediately before **each**
`-i`. Worker (`downloads/ffmpeg_argv.py`) and executor (`media_executor/argv.py`)
stay byte-aligned. This changes runtime argv relative to audited images → final
candidate needs identity reconciliation; **no CVE auto-cleared**.

## Prerequisites checklist

`docs/operations/sec-03c-candidate7-residual-prerequisites-checklist.json` —
`overall_status: NOT_PROVEN` for all eight §6 conditions.

The offline validator now requires each `PROVEN` entry to reference an actual
JSON artifact (`evidence.artifact_path`, relative to the checklist or absolute),
checks its bytes against `artifact_sha256`, and checks its schema, kind, status
and embedded condition/revision/summary binding. The envelope contract and
synthetic regressions are documented in
`tests/image_audit/fixtures/residual_policy/README.md`. File integrity and declared
binding are not independent proof of the underlying operational claim. Activation
remains forbidden in this package even if all envelopes validate. No current
checklist entry or owner approval has been changed to PROVEN/activated.

The compositor protects all referenced inputs (including gosu proposal/validator,
evidence files and implicit identity/smoke reports) before any intermediate
cleanup, child helper or final write; aliases are rejected, not overwritten.

## Activation / rollback outline (NOT EXECUTED)

Use existing release transaction only (no ad-hoc compose up). Rollback must follow
`restore_previous_media` / the release transaction — not an ad-hoc “flags OFF,
stop sidecars” recipe.

1. Owner approves numeric budget (`approved=true` record) — separate act.
2. Install/enable slice; prove `systemctl show … -p ControlGroup,MemoryMax`.
3. Materialize release with media overlays via prepare/verify/deploy-plan/rollout.
4. Install deny-set; prove fail-closed proxy routing.
5. Prove container Landlock ruleset + private writable cgroup acceptance.
6. Flip executor flags ON only after health; keep `MEDIA_MUXING_ENABLED` unchanged.

### Rollback (transactional — restores previous media authority)

`restore_previous_media` restores the **previous** media authority, settings, and
image identities recorded for the failed transaction. It never deletes host
slices as part of rollback.

- **OFF→ON failure:** restore previous **OFF** authority (source-only plan).
  Sidecars introduced by the failed attempt may be removed only under that
  transaction’s ownership labels after the worker is back on the previous OFF
  compose path. Restoring OFF means the previous **in-process** tool path is
  active again — that is the recorded prior state, not “keep isolation while
  mux stays on.”
- **ON→ON failure:** restore previous **ON** authority — previous executor
  settings, previous media image IDs, and re-activate the **previous** sidecars
  for that authority. Do **not** unconditionally stop sidecars or force flags
  OFF: that would drop isolation while `MEDIA_MUXING_ENABLED` can remain true.
- Do not stop sidecars that the restored worker still requires before that
  worker/authority is restored.
- Emergency isolation kill-switch is **not** a substitute for transactional
  rollback and must not be documented as the safe default recovery path.
- Never silently invent a new fallback; retain slice unless a separate
  operational authorization removes it.

## Remaining blockers (concrete)

1. Production numeric budget **not** owner-approved; slice **not** installed.
2. Audited candidate images **absent** on production; live PG **16.9** ≠ candidate 16.15.
3. Shared-disk hard boundary **not** implemented/proven.
4. libxml2/libacl caller attribution on **exact** artifacts **incomplete**.
5. Residual policy **not activated** (`PENDING_TECHNICAL_PREREQUISITES`).
6. Mux argv change ⇒ new candidate identity required before reuse of scan approvals.
