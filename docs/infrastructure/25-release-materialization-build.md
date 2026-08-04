# 25. Release materialization & image build (PRD1C2)

**INFO:** This chapter documents exact trusted-revision source materialization and application image build. It does **not** mutate the running Compose stack, start containers, run migrations, create backups, or deploy. Staging deployment is not claimed.

## Scope (PRD1C2)

**In scope**

- Exact `git archive` materialization of a full SHA already contained in `refs/remotes/origin/main`
- Safe extraction into an incomplete directory
- Exclusive release-root locking under an explicit deploy root
- Build of `api` / `web` / `gateway` images from the snapshot (worker shares API image)
- Immutable `release.json` (schema 1, status `prepared`)
- Atomic rename publication to `releases/<sha>/`
- Idempotent re-prepare and read-only `verify-release`

**Out of scope**

- `docker compose up/down/restart`, rollout, health-driven deploy
- Alembic, backup, rollback, image/release pruning
- Registry push/pull, SSH/Nginx/TLS/systemd

## Deployment root layout

Require an explicit absolute `--deploy-root` (never inferred from cwd; never defaulted to `/srv`):

```text
<deploy-root>/
  releases/<40-char-sha>/
    source/          # extracted git archive (non-writable after publish)
    release.json     # immutable manifest
  locks/.lock        # exclusive prepare lock
```

## Source model

Equivalent to `git archive --format=tar <full-sha>`:

1. Stream to an operation-owned temporary file and fsync
2. Reject empty/truncated archives
3. Verify embedded commit ID (pax `comment`) equals the expected SHA
4. Resolve and record Git tree OID
5. Pre-scan all tar members, then extract with Python 3.12 filters (no absolute/traversal/devices/FIFOs/sockets/unsafe links/duplicates/gitlinks)
6. Reject Git LFS pointers and `.gitmodules` (unsupported; fail closed)
7. Verify required Compose/Dockerfile/build-context paths stay inside the snapshot

Dirty/untracked worktree content cannot enter the snapshot. Operator `release prepare` always runs full PRD1C1 clean-worktree preflight against the operator repository — there is no `allow_dirty` / `skip_preflight` CLI bypass. Isolated integration uses a **separate clean temporary clone** as `repo_root` so a dirty developer worktree does not weaken that production contract.

No `.gitattributes` export-ignore/export-subst is currently present in the repository; if added later, archive contents must be re-audited.

## Image build

Build only from the materialized snapshot Compose files with explicit project name `fetchnow-release-build`. Process env for the build is a minimal Docker client allowlist plus `FETCHNOW_RELEASE_REVISION`; Compose secrets/interpolation use the explicit `--env-file` only. No container startup.

Final tags: `fetchnow-api|web|gateway:<full-sha>` with matching OCI `org.opencontainers.image.revision`. Image IDs are recorded in the manifest from post-build `docker image inspect`. Docker tag creation is **not** transactional: on failure the incomplete filesystem tree is removed and any created revision tags are reported as unmanaged (not deleted); retry rebuilds/verifies the exact revision.

**Tag collision policy:** tags used by running containers (matched by immutable image ID) are refused. Unused unmanaged tags are reported and may be retargeted; the published manifest becomes authority. A finalized matching release never rebuilds. Exclusive release-root locking serializes prepares for one deploy root; a container could still start using an inspected unused tag between check and retarget on a shared Docker host — prepare rechecks container usage immediately before build.

## Publication

Manifest and source identity are verified on the incomplete directory **before** atomic rename into `releases/<sha>/`. Second prepare validates and returns already prepared with no rebuild/retag/chmod/manifest rewrite. Drift fails closed without silent repair.

## Commands

```bash
make release-prepare \
  EXPECTED_REVISION=<40-char-sha> \
  ENV_FILE=.env.staging \
  DEPLOY_ROOT=/srv/fetchnow-staging

make release-verify \
  EXPECTED_REVISION=<40-char-sha> \
  DEPLOY_ROOT=/srv/fetchnow-staging
```

Second prepare for the same SHA verifies and returns already prepared (no rebuild). Drift (manifest/source/image ID/OCI mismatch) fails closed.

## Boundaries

| Milestone | Status |
|---|---|
| PRD1C1 preflight/health | done |
| **PRD1C2 materialize + image build** | this chapter |
| PRD1C3A application activation / rollback | [глава 26](26-deployment-transaction-rollback.md) |
| PRD1C3B migrations | not implemented |
| PRD1D host/public publishing | not implemented |
