# Pre-LIVE status reconciliation (evidence inventory)

Date (local authoring): 2026-09-24.
Reconciliation refresh: 2026-09-27 (docs worktree completion).
Final review (this commit readiness): 2026-09-27.
Object: repository `origin/main` at
`2d24140434ccd53388e73744dfd276f313e76113` (worktree
`codex/docker-access-docs-live-readiness`).

**Scope:** read-only inventory of SEC-00…SEC-12 progress and pre-LIVE gaps.
**Not in scope:** security implementation, payment LIVE enablement, production
SSH, npm audit, deploy, permission changes.

**Evidence classes used below**

| Class | Meaning |
|---|---|
| code-confirmed | present in tree at this SHA |
| merge/CI-confirmed | GitHub merged PR / commit on `main`, and/or CI evidence for that change |
| production-confirmed | verified on production in a dated report **and** still treated as current only with matching date/source |
| production-reported | stated in a dated production/acceptance report; **not** re-verified in this stage (no SSH) |
| owner-reported | stated by the owner; not independently re-probed here |
| UNKNOWN / NO EVIDENCE | insufficient proof in the materials reviewed; **do not** treat as established “not implemented” |

Known prior production claims (production-reported only; not re-probed here):
revision `2d24140…`, deployment `eff53f36…`, DB head
`0010_successful_delivery_quota`, runtime-config schema 4, TEST on / LIVE off /
SEO off, offer DRAFT, runtime-config metadata drift unrepaired.

---

## 1. Canonical SEC plan location

Tracked plan:
[`security-reviews/2026-09-18/remediation-prompt.md`](../../security-reviews/2026-09-18/remediation-prompt.md)
(SEC-00…SEC-12). Supporting dated evidence package under
`security-reviews/2026-09-18/` (assessment, dependencies, SEC-00 matrix).
Those historical capture files are **not** rewritten by the Docker-access
docs update.

---

## 2. SEC-00 … SEC-12 evidence matrix

### SEC-00 — Production baseline / evidence

| Field | Status |
|---|---|
| Task | Capture real server state, discrepancies, budgets; no behaviour change |
| Implementation | Evidence docs + sanitized prod-baseline notes |
| PR/commit | **merge-confirmed** [PR #139](https://github.com/DmitriiSeitsman/FetchNow/pull/139) → merge `0b33e37…` (2026-09-19); content commit `2a926fe…` |
| Test acceptance | Documentation/evidence checklist in `sec-00-evidence-matrix.md` |
| Merge | MERGED to `main` |
| Production rollout | N/A as code change; capture itself was production-verified **as of 2026-09-19** |
| Residual risks | Runtime-config authority drift; restore-verify gap at head 0010; HSTS absent (FN-06); co-tenancy; FN-02 retained |
| Evidence gap | Capture is dated; this stage did **not** re-SSH. Docker ACL “ps denied” was true at capture time; host model later changed (group membership) — historical report left intact |

### SEC-01 — Bounded deploy checks / process cleanup (FN-05)

| Field | Status |
|---|---|
| Task | Time-budgeted Docker checks; kill process groups on cancel/timeout |
| Implementation | Partial pre-existing stabilize deadlines in `scripts/fetchnow_release/stabilize.py` (**code-confirmed**). Full FN-05 remediation per plan: **выполнение не подтверждено доступными доказательствами** (no SEC-01-labelled PR found; unlabelled mapping incomplete) |
| PR/commit | No `SEC-01`-labelled merged work found |
| Tests / merge / prod | UNKNOWN |
| Residual | Local pytest/Docker hang risk may remain; targeted SEC-01 verification is the next agreed step after this docs PR |

### SEC-02 — Astro / production frontend dependency advisories

| Field | Status |
|---|---|
| Task | Update Astro/Sharp and related prod build deps against advisories |
| Implementation | Tree has `astro@5.12.8` (**code-confirmed**). Dedicated SEC-02 remediation: **выполнение не подтверждено доступными доказательствами** |
| PR/commit | No SEC-02-labelled PR found; unlabelled dependency PRs not fully mapped |
| Tests / merge / prod | UNKNOWN whether advisories from 2026-09-18 assessment are closed; **npm audit not re-run** (forbidden this stage) |
| Residual | Dependency risk status UNKNOWN relative to original assessment |

### SEC-03 — Vitest + reproducible dependency checks

| Field | Status |
|---|---|
| Task | Update Vitest/tooling; reproducible npm/Python/image checks in CI |
| Implementation | `vitest@3.2.4` present (**code-confirmed**). SEC-03 program: **выполнение не подтверждено доступными доказательствами** |
| PR/commit | No SEC-03-labelled PR found |
| Residual | CI advisory-source failure modes / image digest pinning UNKNOWN |

### SEC-04 — Trusted proxy chain + inbound budgets (part FN-01/FN-03)

| Field | Status |
|---|---|
| Task | Trusted forwarded headers; per-class request budgets |
| Implementation | Process-local `API_CONCURRENCY_LIMIT` exists (**code-confirmed**). Host Nginx `limit_req` / `real_ip` were **absent** in SEC-00 capture (**production-reported 2026-09-19**). Dedicated SEC-04: **выполнение не подтверждено доступными доказательствами** |
| PR/commit | No SEC-04-labelled PR found |
| Related non-SEC | Free delivery rate limit (PRD1E-B3 / follow-ups) is product shaping; not accepted here as full SEC-04 closure |
| Residual | Client IP spoof / inbound abuse budgets remain open relative to the remediation plan unless later mapped |

### SEC-05 — Atomic queue capacity (main FN-01)

| Field | Status |
|---|---|
| Task | Atomic cross-API queue/active caps |
| Implementation | `CAPACITY_UNAVAILABLE` and job/download machinery exist (**code-confirmed**). Whether limits match SEC-00 budgets and are atomic across API replicas: **выполнение не подтверждено доступными доказательствами** |
| PR/commit | No SEC-05-labelled PR found |
| Residual | FN-01 remains open relative to the remediation plan pending dedicated proof |

### SEC-06 — Anonymous identity abuse budgets (part FN-03)

| Field | Status |
|---|---|
| Task | Bootstrap / infra budgets beyond cookie delete |
| Implementation | Anonymous cookie + Free quota exist (prior product work). SEC-06 identity issuance budgets: **выполнение не подтверждено доступными доказательствами** |
| PR/commit | No SEC-06-labelled PR found |
| Residual | FN-03 has partial product mitigations; SEC-06 acceptance not confirmed |

### SEC-07 — Bounded job/storage cleanup

| Field | Status |
|---|---|
| Task | Batched cleanup; storage caps that actually bind |
| Implementation | Ephemeral artifact hygiene / expiry paths exist from earlier security PR #87 and media work (**merge/CI-confirmed** historically). Mapping to full SEC-07 acceptance: **выполнение не подтверждено доступными доказательствами** |
| PR/commit | No SEC-07-labelled PR found; content mapping to SEC-07 incomplete |
| Residual | TEMP_STORAGE_MAX_BYTES effectiveness / payment-record retention guarantees not re-proven here |

### SEC-08 — Media tool isolation from controlling worker (prep FN-04)

| Field | Status |
|---|---|
| Task | Separate untrusted media executor privileges from worker |
| Implementation | Tool runners / env minimization exist historically; compose shows `network_mode: "none"` for at least one auxiliary service (**code-confirmed** fragment). Full SEC-08 process/privilege boundary: **выполнение не подтверждено доступными доказательствами** |
| PR/commit | No SEC-08-labelled PR found |
| Residual | FN-04 isolation “prepared” status not demonstrated against SEC-08 acceptance criteria |

### SEC-09 — Sandbox egress control (remainder FN-04)

| Field | Status |
|---|---|
| Task | Block sandbox → DB/Docker/metadata/private |
| Implementation | **выполнение не подтверждено доступными доказательствами** |
| PR/commit | No SEC-09-labelled PR found |
| Residual | FN-04 egress control not confirmed closed |

### SEC-10 — HSTS on real TLS terminator (FN-06)

| Field | Status |
|---|---|
| Task | HSTS on host Nginx HTTPS |
| Implementation | No HSTS template hit in tracked `deploy/nginx` / TLS chapters (**code-confirmed** absence). SEC-00: HSTS absent on production HTTPS (**production-reported 2026-09-19**) |
| PR/commit | No SEC-10-labelled PR found |
| Residual | FN-06 not confirmed closed; production header state not re-checked this stage (no SSH) |

### SEC-11 — Re-audit assembled system

| Field | Status |
|---|---|
| Task | End-to-end security acceptance with TEST still on |
| Implementation | **выполнение не подтверждено доступными доказательствами** |
| PR/commit | No SEC-11-labelled PR found |
| Residual | Owner cannot treat the security audit as finished on available evidence |

### SEC-12 — Remove public TEST checkout after owner command (FN-02)

| Field | Status |
|---|---|
| Task | Server-side remove TEST order creation; inventory test entitlements |
| Implementation | Deferred by plan until owner command. TEST path still required (`ROBOKASSA_MODE` ∈ {disabled,test}; live rejected) (**code-confirmed**). Public TEST checkout retained (**owner-reported** decision; still present in code defaults/flags) |
| PR/commit | Not started (correct) |
| Residual | FN-02 remains “temporarily retained by owner”, not fixed |

**Summary:** Only **SEC-00** has merge/CI-confirmed dedicated completion against the remediation plan. For SEC-01…SEC-11, **выполнение не подтверждено доступными доказательствами**: no SEC-labelled merged PRs were found, and unlabelled PR content mapping was not completed in this inventory. Adjacent product/reliability PRs (#87, #110, #115, #140–#142, payments A1–A4) must **not** be counted as closing entire SEC items without explicit mapped acceptance evidence.

---

## 3. Pre-LIVE gap list (code + existing materials)

### 1) LIVE support

| Check | Evidence |
|---|---|
| Mode validation | `Settings`: `ROBOKASSA_MODE` must be `disabled` or `test`; **`live is unavailable`** (**code-confirmed**, `config.py`) |
| Catalog | `get_product` only when `robokassa_mode == "test"` (**code-confirmed**, `catalog.py`) |
| TEST/LIVE credential split | TEST password fields only; no LIVE password fields in settings (**code-confirmed**) |
| Commercial entitlement from TEST | LIVE path absent; TEST entitlements are real Premium capabilities by design until SEC-12 — FN-02 retained |

**Gap:** LIVE mode not implemented. Enabling via env alone is impossible (and must stay so until intentional implementation).

### 2) Commercial catalog (100 ₽)

| Required | Evidence |
|---|---|
| 100 ₽ = `amountMinor` 10000 | Owner-approved commercial terms in `docs/legal/premium-offer-review-notes.md`. Server catalog currently uses `ROBOKASSA_TEST_AMOUNT_MINOR` (assessment sample showed **100** = 1 ₽ TEST) — **no live commercial amount field** |
| RUB / 24h / no autorenew | Product currency `RUB`, duration `86400` (**code-confirmed**). Autorenew absent in code paths reviewed |

**Gap:** commercial `amountMinor=10000` catalog path not implemented.

### 3) Callback / activation

| Check | Evidence |
|---|---|
| Signature / amount / order | `accept_callback` + `signature_matches` + amount compare (**code-confirmed**); postgres tests for idempotency/concurrency (**code-confirmed**) |
| Premium after ResultURL, not browser return | Payment service / API design + tests (**code-confirmed**) |

**Gap for LIVE:** must be re-proven with LIVE credentials/signing; not a blocker to keep TEST.

### 4) Offer

| Check | Evidence |
|---|---|
| Owner-approved draft content | PR #148 merge-confirmed; review notes owner acceptance 2026-09-22 |
| DRAFT / not in force | `/offer/` + `premium-offer.draft.ru.md` (**code-confirmed**) |
| Revision/date/acceptance in order | **NO EVIDENCE** in payment order schema (`offer_revision` etc. absent) |
| Legal review | Explicitly still required (review notes) |

### 5) Repurchase while Premium active

| Check | Evidence |
|---|---|
| UI hides TEST CTA when `premiumHeld` | `render.ts` (**code-confirmed**) |
| Server forbids second purchase | `create_order` does **not** check active entitlement before create (**code-confirmed**) — only checkout visibility + idempotency |
| No stacking promise without impl | Offer/review notes warn; stacking not implemented as sum-of-periods |

**Gap:** UI mitigation only; server-side repurchase policy for commercial LIVE still needs explicit design/impl.

### 6) Lost browser identity

| Check | Evidence |
|---|---|
| Recovery | Session recovery for media jobs exists; Premium bound to anonymous cookie. Cross-device restore **not** confirmed; offer does not promise it (review notes) |

### 7) Receipts / refunds

| Check | Evidence |
|---|---|
| «Мой налог» via Robokassa | **owner-reported**; independently unverified |
| Real receipt proof | NO EVIDENCE in this stage |
| Money refund / receipt correction / Premium revoke | Operational process open (review notes); no backend refund workflow found |

### 8) Operational

| Check | Evidence |
|---|---|
| Runtime-config authority drift | SEC-00 production-reported; **not repaired** (prior reports); not re-SSH’d |
| Restore-verify at DB head 0010 | SEC-00: last verify against **0009**; 0010 UNKNOWN |
| Deploy/rollback | Canonical `make production-release-*` documented; last publication production-reported for `2d24140…` |

### 9) TEST → moderation → LIVE

Still needed (non-exhaustive): owner legal approval of offer in force; commercial catalog + LIVE credentials implementation; SEC-11 completion decision; SEC-12 owner command; Robokassa moderation rules with public TEST; receipt/refund runbook; SEO decision separate.

---

## 4. Priority buckets (Phase 5)

### A. Done with evidence

- SEC-00 documentation baseline (PR #139).
- TEST Robokassa path with server-authoritative callback/idempotency (product PRs; tests in tree).
- Public readiness pages / draft offer / merchant footer (PR #148).
- Owner decision to keep trusted-admin Docker group model (documented in runbook this branch; host not mutated here).

### B. Mandatory blockers under previously agreed gates

- SEC-11 completion (and confirmed closure of required SEC-01…SEC-10 gates) before treating the security audit as finished — **do not unilaterally waive**. Absence of labelled PRs is an evidence gap, not an automatic proof of non-implementation.
- SEC-12 only after owner command (TEST must remain until then).
- LIVE enablement blocked by **code**: no `live` mode, no commercial catalog amount path.
- Offer still DRAFT / not in force; legal review outstanding before real money.
- Commercial catalog `amountMinor=10000` not in server LIVE path.

### C. Owner / Robokassa decisions

- When security audit is “complete enough” to allow SEC-12.
- Offer lawyer sign-off and in-force date.
- Robokassa moderation with public TEST vs intermediate mode.
- Receipt («Мой налог») independent verification.
- Refund + Premium revocation policy.
- Whether repurchase while active should be hard-blocked server-side.
- SEO enablement (separate from LIVE).

### D. Recommendations (not auto-blockers)

- Repair runtime-config authority drift on a dedicated ops window.
- Restore-verify drill at current DB head 0010.
- Stop operator habit of temporary ACL on this host (docs updated).
- Map Reliability / capacity PRs explicitly if owner wants partial SEC credit later.

### E. UNKNOWN

- Current production socket/group/HSTS/drift/restore state (no SSH this stage).
- Whether any unlabelled PR fully satisfies a SEC item.
- Present npm/image advisory posture (audit not re-run).
- External WAF / off-host backup copy (SEC-00 unknowns).

---

## 5. Next scopes (DO NOT START IN THIS DOCS PR)

### Nearest agreed step after this docs-only PR

**SEC-01 targeted verification / implementation** — separate prompt.

Goal: compare SEC-01 requirements to current release tooling (`stabilize.py`, runners, Makefile/CI timeouts) and implement **only** the missing pieces. Do not expand into SEC-02…SEC-12 or LIVE work in that stage.

### Possible later separate scope (not approved / not started)

**LIVE payment foundation** (candidate only): commercial catalog `amountMinor=10000` + fail-closed `ROBOKASSA_MODE=live` plumbing, still default-off; TEST retained until SEC-12 owner command. This is **not** an approved implementation order for the immediate next stage.

Likely files if later approved: `backend/src/fetchnow/core/config.py`, `payments/catalog.py`, `payments/service.py`, `payments/robokassa.py`, focused payment tests.

Rollout impact if later approved: none until an explicit production config change; rollback = keep `ROBOKASSA_MODE=test`.

---

## 6. Docker docs change note (this branch)

Operator docs updated to record permanent `docker` group access on the shared
production host and to stop treating temporary ACL / `docker ps: denied` as the
default completion criterion. Historical SEC-00 ACL checkboxes left unchanged
as point-in-time evidence.

---

**Statuses**

- DOCKER ACCESS DOCS — ready for review (local branch only; final review 2026-09-27).
- PRE-LIVE STATUS — reconciled with explicit evidence gaps (**PARTIAL** on live production re-verification because SSH was forbidden).
- Accepted operational model ≠ completed SEC hardening; retained TEST ≠ closed FN-02.
