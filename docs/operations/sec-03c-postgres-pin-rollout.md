# SEC-03C — PostgreSQL pin vs application rollout

Canonical Compose pin (this branch): `postgres:16.15-alpine3.24`
(`POSTGRES_IMAGE_PREFIX = "postgres:16.15"`).

## What app rollout does **not** do

Ordinary application rollout / activate:

- builds an images override for **api / worker / delivery / web / gateway /
  storage-init only**;
- refuses to include `postgres` in activate argv (`ActivateError`);
- uses `compose up --no-deps --no-build --pull never` for application services.

Therefore a **running** PostgreSQL container that still uses an older image
(for example `postgres:16.9-alpine`) is **not** recreated or retagged by a
normal app rollout, even when `compose.yaml` already names the newer canonical
pin.

Backup/restore helpers exec into the **running** postgres service; they do not
silently switch to a different image tag. After a future DB cutover, operators
must use tooling that matches the **running** server image.

## Blocking dependency before production DB cutover

Updating the Compose pin and CI image is **not** a production PostgreSQL
upgrade. A separate, approved window is required before changing the live
database container:

1. Logical backup + restore verification on a disposable project.
2. Explicit stop/recreate (or equivalent) of postgres onto
   `postgres:16.15-alpine3.24` with volume retention plan.
3. `pg_isready`, application migrations/heads check, smoke queries.
4. Documented rollback (restore from the verified backup).

Until that window completes, treat the Compose/`POSTGRES_IMAGE_PREFIX` bump as
**CI + future bootstrap** authority only. Do not assume production postgres
matches the new pin.

## Contract strength

Preflight / compose validation still require the **canonical** pin prefix
(`postgres:16.15-*`). The prefix is **not** widened to arbitrary `postgres:16*`.
Historical PR8 fixtures that still name `postgres:16.9-alpine` remain frozen
provenance artifacts and are not the production contract.

## Residual image findings (gosu) — not remediated in SEC-03C targeted fix

Official `postgres:16.15-alpine3.24` vendors **`/usr/local/bin/gosu`** (v1.19,
embedded Go **1.24.6**) for entrypoint privilege drop
(`exec gosu postgres …`). Image-audit reports fixable HIGH/CRITICAL against
Go `stdlib` on that gobinary.

**FetchNow does not** delete gosu, substitute the binary, or build a custom
PostgreSQL image in this workstream.

- Upstream wait for a newer official image / gosu rebuild is **tracking**, not
  risk elimination.
- Many advisories target APIs that gosu's upstream `SECURITY.md` argues are
  unused; that is an **applicability question for owner decision**, not an
  approved exception/VEX/ignore in this repository.
- No owner exception has been approved. Gate remains fail/needs-decision until
  upstream changes or an explicit owner decision is recorded separately.
