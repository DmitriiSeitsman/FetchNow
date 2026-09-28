# SEC-01 — Bounded release probes and process cleanup

Date: 2026-09-28 (extended short-probe coverage).
Implements remediation item SEC-01 / FN-05 for FetchNow release tooling.

**Acceptance note:** An earlier pre-commit acceptance understated remaining short
read-only Docker CLIs on rollout/recover/migrate/deploy-plan/bootstrap paths.
Those probes are now on the same bounded runner. This does **not** mean every
Docker call in the tree is bounded.

## Covered read-only probes

Default probe cap: **20s** (`DOCKER_PROBE_TIMEOUT_SECONDS`), nested under a parent
monotonic deadline when one exists. Nested timeout is
`min(per-probe cap, remaining budget)`; no new probe below
`DOCKER_PROBE_MIN_REMAINING_SECONDS` (0.25s).

| Area | Commands |
|---|---|
| `docker_checks` / preflight | `compose version`, `docker info`, `compose config` |
| stabilize / health | `compose ps`, `docker inspect` |
| bootstrap_cleanup discovery | `compose ps`, `docker inspect` |
| `db_heads` | `compose exec … psql` Alembic heads (timeout → `query_failed`, never empty heads) |
| `bootstrap_freshness` | `docker volume ls`; catalog `compose exec … psql` (timeout → error / `query_failed`, never fresh) |
| `migrate._snapshot_container_ids` | `compose ps -q` (timeout ≠ missing container) |
| `image_build` metadata | `docker version`, `compose version`, image/container inspect & `docker ps` filters |

Stdout/stderr caps are **2 MiB each** (independent). Truncated output is not a
trusted complete list — callers fail closed.

Stopping the Docker CLI for `compose exec` does **not** prove the in-container
`psql` stopped. Such outcomes are treated as failed reads, not success.

## Mutating cleanup (separate semantics)

| Call | Class | Failure |
|---|---|---|
| `bootstrap_cleanup` `docker rm -f` | mutating | `BootstrapCleanupError`; timeout ≠ successful remove; no auto-retry; unresolved journal if cleanup fails |

## Not on the 20s probe cap (remaining unbounded)

Long / mutating operations retain prior contracts and **may still hang**:

- `activate` compose up / storage-init run
- `postgres_bootstrap` compose up postgres
- `image_build` compose **build** (metadata probes above are bounded)
- Alembic migrate `compose run`
- archive create/extract
- config_rollout mutations
- integration harness scripts under `scripts/release_*_integration_test.py`

SEC-01 does **not** claim “all Docker calls are bounded.”

## Timeout / cancellation outcomes

| Outcome | Meaning |
|---|---|
| Success | process exited without forced group cleanup; callers interpret exit code |
| `BoundedTimeoutError` / domain mapping | CLI exceeded budget; **not** success / not empty discovery |
| `BoundedCancelledError` | cancel / wrapper SIGTERM·SIGINT; **not** success |
| `BudgetExhaustedError` | parent deadline left no time for another probe |
| `OutputLimitExceededError` | stream cap exceeded; **not** a complete result |
| Forced group cleanup error | stuck readers after leader exit; **not** clean success |

Stabilization/health/bootstrap failures continue to drive existing journal
failure / rollback / recovery. Probe timeout must not be recorded as `committed`.

## Process cleanup guarantees and limits

`scripts/fetchnow_release/bounded_subprocess.py`:

- argv list, no shell; new session/process group
- SIGTERM → always wait grace → SIGKILL to spawn-time pgid; reap direct child
- **Not guaranteed:** detached/out-of-group descendants; absolute freedom from
  theoretical pgid reuse after full group exit
- No `pkill` by name; does not signal the agent session

## Local test wall / CI

- `scripts/run_pytest_bounded.py` wraps **release / pg_backup** Make pytest
  entrypoints (`FETCHNOW_PYTEST_WALL_SECONDS`, default 600). Backend pytest is
  **not** wrapped.
- CI jobs set `timeout-minutes`. Forced runner destruction may skip cleanup;
  disposable project naming + journal recovery remain the contract.
- Integration harness Docker calls are limited by CI job timeout, not this
  helper.

## FN-05 status

Bounded coverage now includes the short diagnostic and short `compose exec`
read probes used by canonical production gates listed above. Long mutates and
test harness subprocesses remain residual risk. Treat FN-05 as **substantially
addressed for short release probes**, not as a claim that every Docker CLI in
the repository is bounded.

## Safe re-run

1. Inspect deployment/migration journals under the deploy root.
2. Non-terminal / recovery-required → matching recover command; do not remutate
   solely because a CLI exited.
3. Read-only probes may be retried after the daemon is healthy.
