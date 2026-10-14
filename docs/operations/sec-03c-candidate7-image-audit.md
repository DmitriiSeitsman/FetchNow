# SEC-03C/08/09 candidate-7 native image audit

Controlled one-shot Trivy **0.74.0** audit of **seven** distinct final runtime
images for the SEC-08/09 candidate tip.

## Scope

| Item | Value |
|---|---|
| Aggregate | `scripts/image_audit_candidate7.py` |
| History proof | `scripts/image_audit_candidate7_history.py` |
| Canonical four-target gate | `scripts/image_audit.py` (unchanged; still exactly four) |
| Workflow | `.github/workflows/sec03c-candidate7-image-audit.yml` (`fetch-depth: 0`) |
| Baseline native PASS | run `38627426197` @ `686369bd…` |
| Prior technical FAIL | run `38631542113` (shallow checkout missing baseline) |
| Scanner | Trivy 0.74.0 Linux-64bit `2ae6fe3ee734…` |
| Postgres pin | `postgres:16.15-alpine3.24` (Compose) |

Targets: `api` (also worker/delivery), `web`, `gateway`, `media-executor`,
`media-net-executor`, `egress-proxy`, `postgres`.

## Non-claims

- Successful scan with findings is **not** image-security PASS.
- Candidate PostgreSQL scan does **not** prove live retained PostgreSQL identity
  (`live_retained_postgresql: NOT_VERIFIED`).
- No VEX/exceptions applied in this pass.
- Residual acceptance: **NOT GRANTED**. Production readiness: **NO**.

## Policy (unchanged)

- Fixable HIGH/CRITICAL → fail
- Unfixed HIGH/CRITICAL → needs_owner_decision
- Technical/incomplete → fail
- No ignore-unfixed / severity downgrade / continue-on-error
