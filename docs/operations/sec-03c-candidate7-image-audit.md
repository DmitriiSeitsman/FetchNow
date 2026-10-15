# SEC-03C/08/09 candidate-7 native image audit

Controlled one-shot Trivy **0.74.0** audit of **seven** distinct final runtime
images for the SEC-08/09 candidate tip.

## Scope

| Item | Value |
|---|---|
| Aggregate | `scripts/image_audit_candidate7.py` |
| History proof | `scripts/image_audit_candidate7_history.py` (`prove_successor_history`) |
| Compatibility smoke | `scripts/image_audit_candidate7_smoke.py` (literal dpkg argv; uid 10001 media) |
| Canonical four-target gate | `scripts/image_audit.py` (unchanged; still exactly four) |
| Workflow | `.github/workflows/sec03c-candidate7-image-audit.yml` (`fetch-depth: 0`) |
| Native PASS (unchanged baseline) | run `38627426197` @ `686369bd…` |
| Audit predecessor | run `38637415562` @ `48d9fc5…` |
| Prior technical FAIL | run `38631542113` (shallow checkout missing baseline) |
| Prior successor smoke FAIL | run `38641657351` (dpkg format expansion; scan NOT_RUN) |
| Scanner | Trivy 0.74.0 Linux-64bit `2ae6fe3ee734…` |
| Postgres pin | `postgres:16.15-alpine3.24` (Compose) |

Targets: `api` (also worker/delivery), `web`, `gateway`, `media-executor`,
`media-net-executor`, `egress-proxy`, `postgres`.

## Successor history contract

Native baseline is **not** retargeted. Successor HEAD must satisfy:

1. Native baseline and audit predecessor resolve as commit objects and are
   ancestors of HEAD.
2. Baseline → predecessor: audit-only paths; runtime/build inputs unchanged.
3. Predecessor → successor: only the three approved Dockerfiles, reviewed gosu
   evidence append, and listed audit harness/tests/docs/workflow paths.
4. Approved file **content** SHA-256 (not merely path names) verified from the
   commit under test.
5. Locks / Compose / sidecar Dockerfiles / app source unchanged.

`runtime_inputs_unchanged` is **false** when an approved Dockerfile delta is
present. Evidence records native PASS, approved delta, and `NEW_AUDIT_ARTIFACT`
separately. New builds do **not** inherit Image IDs from native PASS
`38627426197`.

## Non-claims

- Successful scan with findings is **not** image-security PASS.
- Candidate PostgreSQL scan does **not** prove live retained PostgreSQL identity
  (`live_retained_postgresql: NOT_VERIFIED`).
- Gosu applicability review is evidence only — **not** applied VEX.
- No VEX/exceptions applied in this pass.
- Residual acceptance: **NOT GRANTED**. Production readiness: **NO**.

## Policy (unchanged)

- Fixable HIGH/CRITICAL → fail
- Unfixed HIGH/CRITICAL → needs_owner_decision
- Technical/incomplete → fail
- No ignore-unfixed / severity downgrade / continue-on-error
