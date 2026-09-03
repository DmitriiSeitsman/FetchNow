# 31. Canonical runtime config rollout (PRD1E-B2.1)

## Source rollout versus config rollout

An application rollout changes the immutable source/image revision and records
that identity in `state/current.json`. A config rollout keeps the accepted
revision, image IDs, deployment ID, and database heads unchanged. It may only
recreate services whose rendered runtime environment changed through the
reviewed allowlist.

The canonical production entry point is:

```bash
make production-release-config-rollout \
  EXPECTED_REVISION=<active-40-char-sha>
```

Never replace this command with a manual `docker compose up`, `recreate`, or a
direct call to the Python activation helper.

## Active config authority

`state/current.json` remains schema v2 and continues to describe application
and database release identity. Config rollout adds the separate
`state/runtime-config.json` authority. Schema 2 is bound to the active revision and
deployment ID and contains only:

- normalized values from the reviewed non-secret runtime allowlist;
- normalized, non-secret build-time values used to detect forbidden changes;
- domain-separated SHA-256 fingerprints;
- the latest committed config rollout ID and timestamp.

The runtime fingerprint is SHA-256 over
`fetchnow:runtime-config:v1\0 || canonical-json`. Keys are sorted and JSON is
compact, so env-file ordering cannot change the result. Secrets, credentials,
storage paths, bindings, and arbitrary env values are neither fingerprinted nor
persisted.

Legacy deployments need one explicit initialization while the host env still
matches the running containers:

```bash
make production-release-config-rollout \
  EXPECTED_REVISION=<active-40-char-sha> \
  INIT_CONFIG=1
```

Initialization is health-gated and performs no recreation. It is refused after
an allowlisted runtime value has already been edited. Repeat initialization
after every normal source rollout because the deployment binding changes.
Schema-1 state containing only `FREE_DOWNLOAD_QUOTA_ENABLED` remains readable
and validates against its original exact key set and fingerprint domain. It is
never silently padded with B3 values. After the B3 source rollout, explicit
no-delta initialization writes schema 2 with the live delivery values.

## Config classification

The initial mutation allowlist is intentionally narrow:

| Key | Classification | Runtime receiver | Config rollout |
|---|---|---|---|
| `FREE_DOWNLOAD_QUOTA_ENABLED` | runtime-only | `api` | allowed |
| `FREE_DELIVERY_RATE_LIMIT_ENABLED` | runtime-only | `api`, `delivery` | allowed |
| `FREE_DELIVERY_RATE_BYTES_PER_SECOND` | runtime-only bounded integer | `api`, `delivery` | allowed |
| `FREE_DOWNLOAD_LIMIT` | runtime-wired | `api`, `worker` | classified, not allowlisted |
| `FREE_DOWNLOAD_WINDOW_SECONDS` | runtime-wired | `api`, `worker` | classified, not allowlisted |
| `FREE_DOWNLOAD_QUOTA_RETENTION_SECONDS` | runtime-wired | `api`, `worker` | classified, not allowlisted |
| `MEDIA_INSPECTION_ENABLED` | runtime-wired | `api`, `worker` | classified, not allowlisted |
| `MEDIA_JOBS_ENABLED` | runtime-wired | `api`, `worker` | classified, not allowlisted |
| `MEDIA_DOWNLOADS_ENABLED` | runtime-wired | `api`, `worker` | classified, not allowlisted |
| `MEDIA_DELIVERY_ENABLED` | runtime-wired | `delivery` | classified, not allowlisted |
| `MEDIA_BROWSER_DELIVERY_ENABLED` | runtime-wired | `api`, `worker`, `delivery` | classified, not allowlisted |
| `MEDIA_MUXING_ENABLED` | runtime-wired | `worker` | classified, not allowlisted |
| `PUBLIC_MEDIA_FLOW_ENABLED` | build-time | `web` build arg | rejected |
| `PUBLIC_SEARCH_INDEXING_ENABLED` | build-time | `web` build arg | rejected |
| `PUBLIC_SITE_URL` | build-time | `web` build arg | rejected |
| `FETCHNOW_RELEASE_REVISION` | image/release identity | all image references/builds | rejected |
| `GATEWAY_PORT` | host/network binding | `gateway` publish | rejected |
| PostgreSQL credentials / `DATABASE_URL` | secret/identity | database consumers | rejected |

The production env feeds runtime values to `api`, `worker`, and `delivery`;
PostgreSQL receives its database identity/credentials. `web` public flags are
build args, not live runtime toggles. `gateway` consumes release identity and
the loopback publish binding, neither of which belongs to config rollout.

The transaction renders Compose from the verified prepared release, reads the
live container environments, and compares every Compose-declared environment
key in memory. A changed key outside the allowlist aborts before recreation.
Only key/service names may appear in diagnostics; values are not printed.

## Transaction and journal

Each mutation creates `config-rollouts/<config-rollout-id>/` with an immutable
plan, monotonic events, an exact-image override, and a terminal result. The plan
records previous/target fingerprints, changed key names, affected services,
the previous deployment/config identities, revision, and start time. The result
records commit/rollback and health outcomes. No plaintext config values or
secrets are journaled.

Before mutation the command requires:

1. active revision equals `EXPECTED_REVISION`;
2. prepared release and manifest verify;
3. immutable image IDs equal `current.json`;
4. live database heads equal saved heads and compatibility passes;
5. no unresolved deployment, migration, bootstrap, or config journal;
6. live config equals the persisted active fingerprint;
7. build-time config is unchanged;
8. every effective service env delta is allowlisted.

For quota activation the affected set is exactly `api`. The existing activation
helper runs Compose with `--no-build --no-deps --pull never` and the current
immutable image-ID override. Worker, delivery, web, gateway, and PostgreSQL
container IDs must remain unchanged. After replacement, scoped health and the
global official health gate run, followed by revision and DB-head revalidation.
Only then is `runtime-config.json` committed.

A same-fingerprint request returns `already-active-config` and does not recreate
containers.

For either B3 key the affected set is `api` and `delivery`. Changing the flag,
the rate, or both recreates those two services once with the accepted immutable
API image ID; worker, web, gateway, and PostgreSQL remain unchanged. The rate
uses canonical unsigned decimal syntax and is bounded to 262144..67108864
bytes/s. API and delivery must receive identical product-policy values so
admission snapshots match live delivery fallback.

## Rollback

If activation, verification, or health fails after mutation starts, the journal
already contains the previous sanitized runtime values. The tool creates a
short-lived mode-600 copy of the host env, restores only those allowlisted
values, recreates the same affected services with the same immutable image IDs,
and repeats scoped and global health. The temporary file is deleted afterward.

- Successful automatic rollback produces terminal `rolled_back`; active config
  state remains on the previous fingerprint. Live containers are restored from
  the persisted sanitized snapshot via a temporary env copy; the operator host
  env file is not rewritten and may still contain the requested target. Retry
  the same canonical command after the failure is understood.
- Failed automatic rollback produces terminal `rollback_failed` with failure
  classes and durable evidence. Stop and investigate; do not edit
  `current.json`, quota rows, or container state manually.
- A post-commit operator rollback is another reviewed env delta followed by the
  same canonical config-rollout command.

Config rollback never changes source revision or database schema.

## Production quota example

With the baseline still set to `false`, initialize once as shown above. Then
atomically edit only:

```env
FREE_DOWNLOAD_QUOTA_ENABLED=true
```

Keep `FREE_DOWNLOAD_LIMIT=3`, `FREE_DOWNLOAD_WINDOW_SECONDS=86400`,
`PUBLIC_SEARCH_INDEXING_ENABLED=false`, and `MEDIA_MUXING_ENABLED=true`. Run:

```bash
make production-release-config-rollout \
  EXPECTED_REVISION=<active-40-char-sha>

make production-release-health \
  EXPECTED_REVISION=<active-40-char-sha>
```

The config command performs its own global health gate; the second command is
the explicit operator acceptance check. Quota semantic smoke remains a separate
Stage 2 acceptance step.
