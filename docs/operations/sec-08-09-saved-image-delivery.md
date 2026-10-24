# SEC-08/09 — saved-image delivery (local implementation, 2026-10-24)

## Purpose and boundary

Deliver the exact image bytes checked on the RC builder instead of rebuilding
them on the destination. This adds transport, **not** risk acceptance or a new
deployment authority. Native Docker save/load acceptance is still required.
Production installation and rollout have not been performed.

The existing `prepare` build path remains the default. Explicit import uses
`prepare --image-bundle ... --image-bundle-sha256 ...`; it never falls back to
build, pull or a different platform. Existing preflight, clean-worktree,
trusted-origin/main, source materialization, release-root lock, immutable source
checks and atomic release publication remain in place. No source-contract
version, runtime image, dependency pin or activation policy was changed.

## Commands (future operator workflow; not executed on production)

Run from the repository's `scripts` import path. Required values below refer
to an authorized future release, **not** the deleted images of run 40070427618.

After prepare, compatibility, acceptance and scanning on the RC builder:

```sh
python -m fetchnow_release export-images \
  --expected-revision "$RELEASE_REVISION" \
  --deploy-root "$BUILDER_DEPLOY_ROOT" \
  --output "$NEW_BUNDLE_DIRECTORY" \
  --postgres-identity-json "$AUDITED_POSTGRES_IDENTITY_JSON"
```

The candidate PostgreSQL JSON uses the existing audit identity fields:
`candidate_only:true`, `platform:linux/amd64`, `config_image_id`,
`registry_index_digest` and `platform_manifest_digest_linux_amd64`. The three
digests must be full, valid and distinct. They are preserved separately, not
inferred from a mutable tag or invented from `RepoDigests`. This metadata is
supplied by the trusted audit pipeline; transport does not independently query
the registry or prove applicability of any advisory.

Export saves six application images in media mode (three in canonical mode)
plus the explicit candidate PostgreSQL image. The tool prints the SHA-256 of
`image-bundle.json`. Publish this checksum through the trusted RC evidence
channel and preserve the complete bundle as a private CI artifact **before**
image cleanup. Do not commit binary archives or environment files to Git.

After authorized transfer to the destination, with the checked source revision
already available in trusted origin/main:

```sh
python -m fetchnow_release prepare \
  --project-name "$TARGET_PROJECT" \
  --env-file "$TARGET_ENV_FILE" \
  --compose-file compose.yaml \
  --compose-file "$TARGET_COMPOSE_OVERLAY" \
  --expected-revision "$RELEASE_REVISION" \
  --deploy-root "$TARGET_DEPLOY_ROOT" \
  --release-mode media \
  --image-bundle "$RECEIVED_BUNDLE_DIRECTORY" \
  --image-bundle-sha256 "$TRUSTED_BUNDLE_SHA256"
```

Both import arguments are required together. Obtain the expected checksum from
the trusted builder record, **not** by calculating and trusting a checksum from
an untrusted received copy. A checksum is integrity/binding, not a signature,
owner approval or proof that the build itself is safe.

The bundle's recorded overlay must match the destination. In particular,
staging-built frontend/public configuration is not silently promoted to
production by changing an overlay. The final RC must be built for its intended
environment; this task does not add a cross-environment promotion shortcut.

## Format and checks

Flat directory: `image-bundle.json`, original `release.json`, and exactly one
`<service>.tar` for each required image including postgres. Extra files,
directories, symlinks, incomplete sets and duplicate services are rejected.
Keep external checksum/evidence files outside this flat directory.

`fetchnow-image-bundle/v1` binds source revision/tree, release mode, overlay,
original release-manifest checksum, archive sizes/checksums, platforms and
Image IDs. App IDs must equal the existing release manifest; worker, delivery
and storage-init continue to share the API image through existing mappings.

Docker archives are saved **by immutable ID without tags**, one linux/amd64
image per archive. Before loading, the tool validates tar metadata without
extracting it: safe unique regular entries, one manifest/config, no RepoTags,
config SHA-256 == expected Image ID, platform and OCI revision. OCI indexes, if
present, must not hide another config/platform or named tags. Layer consistency
is ultimately checked by Docker when loading the authenticated archive; this
validator does not claim to reimplement Docker's layer verification.

Limits: 8 GiB per uncompressed archive, 20,000 outer members, 2 MiB per JSON
metadata member, 900 seconds per save/load/tag command and bounded diagnostic
output. Unsupported format/platform is an error, never a fallback.
The platform flags require Docker API 1.48+; see the official
[save](https://docs.docker.com/reference/cli/docker/image/save/) and
[load](https://docs.docker.com/reference/cli/docker/image/load/) contracts.

All bundle files validate before the first load. The importer stages private
copies with bounded byte counts and verifies their hashes before loading, so
it does not load directly from mutable received files. Staging requires archive
sizes plus the existing 1 GiB release margin on the staging filesystem. This
does **not** prove Docker data-root capacity or approve a host resource budget;
those remain host-preflight requirements.

Loaded IDs, platforms and app OCI revision labels are inspected, not inferred
from stdout. Existing revision tags pointing at another ID cause refusal even
if unused. Only the expected application tags may be created. An import receipt
records the bundle checksum and all loaded IDs; repeated prepare requires the
same release metadata/receipt and present exact images. No current/media
authority, containers, sockets, networks, volumes or activation flags are
modified by transport.

## PostgreSQL and failure behavior

The PostgreSQL archive is loaded without tags and retained only as a candidate.
The importer never retags `postgres:*`, starts/replaces PostgreSQL, migrates the
database, or changes the live retained PostgreSQL identity. Registry index and
platform-manifest metadata are preserved in the receipt but are **not** claimed
to reappear as loaded `RepoDigests`. Database replacement remains a separate
explicitly authorized operation.

An image archive checksum is not an Image ID; an Image ID/config digest is not
a registry index or platform-manifest digest. If an engine does not preserve
the expected config/Image ID, import fails rather than substituting another
identity.

There is no automatic retry or deletion of daemon resources. A Docker timeout
or interrupted load/tag is an uncertain daemon outcome: no release authority is
published, but loaded images or newly created revision tags may remain. Inspect
before a separately authorized retry. Export failures retain only their own
partial output directory for diagnosis; the bundle metadata is not usable until
all required files/checks pass. Global prune is never used.

The release-root lock serializes writers for one deploy root; unrelated/root
Docker clients can still race tag checks. Operate on an administratively
exclusive release window/engine. This is not an engine-wide CAS mechanism.

## Evidence and next acceptance

Changed/new scope: `image_archive.py`, `image_bundle.py`, `prepare.py`, `cli.py`,
`tests/release/test_image_bundle_unit.py`, and this document. Existing dirty
SEC-03C/VEX/residual/validator and storage corrective files are preserved.

Unit tests use real temporary archive/JSON files with stubbed Docker,
Git/preflight/source materialization where indicated. They test delivery and
release orchestration; they are **not** native Docker-format or production
acceptance. Full release/backup/image-audit regression counts are recorded in
the implementation handoff.

Local verification: **1,337 passed in aggregate** (release 800, pg_backup 67,
image_audit 470), including **48 new image-bundle cases**. Of these, 1,329 ran
in the sandbox; eight existing syntax/loopback checks ran once separately in
a hash-verified temporary copy with loopback access. Source and test copies
were identical; the actual worktree was not used for those temporary writes.
Ruff check/format, strict mypy on the four touched production Python modules
and `python -S` CLI import/help passed. No workflow changed.

The first combined run was not clean: it exposed an early-overlay regression
for V1 idempotency (fixed by retaining the legacy order), an invalid new
failure-injection fixture (fixed without relaxing the source hash guard), and
the eight sandbox restrictions above. It remains a failed historical local
run, not a native failure or security finding. Pytest also reported garbage
collection warnings for old readonly temporary release fixtures. The new
fixture teardown restores only its own directory modes; no global temporary
directory cleanup/prune was performed.

Next authorized native acceptance should export a prepared production-intent
candidate after its gates, preserve the artifact, import onto a disposable
native linux/amd64 Docker engine, and prove:

- Same archive hashes and loaded Image IDs for all seven roles;
- no build/pull, no unrelated tag/retained PostgreSQL changes;
- valid source/manifest/receipt and no activation during import;
- refusal for corrupt archives, wrong checksums/IDs and conflicting tags;
- ordinary release health/rollback still works with imported images;
- artifact upload is complete and verified before scoped cleanup.

No real images were built, saved, loaded or scanned in this local task.
No CI/SSH/host installation/commit/push/merge/deploy or new approvals.
**READY FOR ROLLOUT: NO.**

## Native acceptance harness (one-shot)

`scripts/native_saved_image_delivery_acceptance.py` and
`.github/workflows/sec08-09-saved-image-delivery.yml` run one native proof of
this mechanism. The workflow is push-only, attempt 1 only, and gated on the
exact first-line commit subject
`test(security): SEC-08/09 native saved-image delivery acceptance [native-once-20261024-imgbndl]`.
Without `FETCHNOW_NATIVE_SAVED_IMAGE_DELIVERY_EXECUTE=1` on native Linux AMD64
the harness returns `NOT_RUN`.

Identities recorded separately: tooling commit (the workflow commit, parent
`97b3964160fb6757dd6e86d497f1cd3d838754be`), product revision
`3792c377ecde1218ad9da9f3d5b91d620ed63b46` / tree
`e9dbbc4588c2e968fd32e0201dac99b075924134`, release mode `media`, overlay
`compose.staging.yaml`, host arch and Docker version/daemon ID per job.
Trusted-main authority is a **test fixture**: a disposable local clone whose
`refs/remotes/origin/main` is set to the product revision; the product tree is
checked against the pin. Production `origin/main` is neither changed nor
claimed.

- **Builder job**: standard `prepare` build (new artifacts; nothing historical
  transfers), `verify-release`, manifest/tree/contract checks, seven full IDs,
  platforms and OCI revision labels; candidate PostgreSQL pulled only by the
  pinned index digest after registry bytes hash to the pinned index and
  linux/amd64 manifest digests (no tag, never started, live PostgreSQL not
  consulted); `export-images`; `validate_bundle`. The bundle SHA-256 is a job
  output; the bundle is a private artifact (3-day retention) and publication
  is confirmed before scoped cleanup. Upload failure means import `NOT_RUN`.
- **Importer job**: a different runner and Docker daemon (daemon IDs
  compared) that must start without any of the seven IDs. Refusals run first
  on per-case hardlinked bundle copies (wrong checksum, corrupted tar,
  missing/extra file, wrong revision/mode/overlay) with no Docker events, no
  inventory change and no published release/current/media authority. Then the
  standard `prepare --image-bundle --image-bundle-sha256 --release-mode media`
  import is checked by Docker events (load/tag only), exact inventory delta,
  unchanged build cache, atomic publish, receipt bound to the checksum and all
  IDs, unchanged env/current/media authority/containers. Repeated prepare must
  be idempotent with zero Docker events. Conflicting app tag (own disposable
  reference, restored) and missing/mismatched receipt on copied release roots
  must be refused. `verify-release` plus the health/rollout image-identity
  primitives must resolve the imported IDs; no containers are started, so this
  is **not** the full native rollout/health matrix.
- **Cleanup**: only images/tags new since each job's baseline inventory and
  run-created directories; refused if containers exist; no prune; build cache
  is left on the disposable runner. Cleanup errors are reported separately
  from the primary error.

This harness is delivery acceptance only: **not** image-security acceptance
(no Trivy/advisory calls), not production installation, not a rollout.
