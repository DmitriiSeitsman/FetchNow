"""Constants for the initial database bootstrap transaction."""

from __future__ import annotations

BOOTSTRAPS_DIRNAME = "bootstraps"
BOOTSTRAP_PLAN_NAME = "plan.json"
BOOTSTRAP_RESULT_NAME = "result.json"
BOOTSTRAP_EVENTS_DIRNAME = "events"
BOOTSTRAP_OVERRIDES_DIRNAME = "overrides"

BOOTSTRAP_PLAN_SCHEMA_VERSION = 1
BOOTSTRAP_EVENT_SCHEMA_VERSION = 1
BOOTSTRAP_RESULT_SCHEMA_VERSION = 1

BOOTSTRAP_TEST_PROJECT_PREFIX = "fetchnow-bootstrap-test-"

STATUS_BOOTSTRAP_PLANNED = "planned"
STATUS_BOOTSTRAP_POSTGRES_STARTED = "postgres_started"
STATUS_BOOTSTRAP_FRESHNESS_VERIFIED = "freshness_verified"
STATUS_BOOTSTRAP_SCHEMA_STARTED = "schema_started"
STATUS_BOOTSTRAP_SCHEMA_COMPLETED = "schema_completed"
STATUS_BOOTSTRAP_TARGET_HEADS_VERIFIED = "target_heads_verified"
STATUS_BOOTSTRAP_COMMITTED = "committed"
STATUS_BOOTSTRAP_FAILED = "failed"
STATUS_BOOTSTRAP_REQUIRES_RECOVERY = "requires_recovery"

BOOTSTRAP_LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_BOOTSTRAP_PLANNED: frozenset(
        {
            STATUS_BOOTSTRAP_POSTGRES_STARTED,
            STATUS_BOOTSTRAP_FRESHNESS_VERIFIED,
            STATUS_BOOTSTRAP_FAILED,
        }
    ),
    STATUS_BOOTSTRAP_POSTGRES_STARTED: frozenset(
        {
            STATUS_BOOTSTRAP_FRESHNESS_VERIFIED,
            STATUS_BOOTSTRAP_FAILED,
            STATUS_BOOTSTRAP_REQUIRES_RECOVERY,
        }
    ),
    STATUS_BOOTSTRAP_FRESHNESS_VERIFIED: frozenset(
        {
            STATUS_BOOTSTRAP_SCHEMA_STARTED,
            STATUS_BOOTSTRAP_COMMITTED,
            STATUS_BOOTSTRAP_FAILED,
        }
    ),
    STATUS_BOOTSTRAP_SCHEMA_STARTED: frozenset(
        {
            STATUS_BOOTSTRAP_SCHEMA_COMPLETED,
            STATUS_BOOTSTRAP_REQUIRES_RECOVERY,
        }
    ),
    STATUS_BOOTSTRAP_SCHEMA_COMPLETED: frozenset(
        {
            STATUS_BOOTSTRAP_TARGET_HEADS_VERIFIED,
            STATUS_BOOTSTRAP_REQUIRES_RECOVERY,
        }
    ),
    STATUS_BOOTSTRAP_TARGET_HEADS_VERIFIED: frozenset(
        {
            STATUS_BOOTSTRAP_COMMITTED,
            STATUS_BOOTSTRAP_REQUIRES_RECOVERY,
        }
    ),
    STATUS_BOOTSTRAP_COMMITTED: frozenset(),
    STATUS_BOOTSTRAP_FAILED: frozenset(),
    STATUS_BOOTSTRAP_REQUIRES_RECOVERY: frozenset(),
}

BOOTSTRAP_TERMINAL_STATUSES = frozenset(
    {
        STATUS_BOOTSTRAP_COMMITTED,
        STATUS_BOOTSTRAP_FAILED,
    }
)

BOOTSTRAP_UNRESOLVED_STATUSES = frozenset(
    {
        STATUS_BOOTSTRAP_REQUIRES_RECOVERY,
    }
)

PGDATA_VOLUME_KEY = "pgdata"
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_VOLUME_LABEL = "com.docker.compose.volume"

# Explicit bootstrap-owned catalog allowlist. Empty: Alembic is the first
# writer of application relations. Do not replace this with a heuristic.
# Format: "{schema}.{relname}:{relkind}" e.g. "public.foo:r".
BOOTSTRAP_ALLOWED_USER_RELATIONS: frozenset[str] = frozenset()
BOOTSTRAP_ALLOWED_EXTRA_SCHEMAS: frozenset[str] = frozenset()

# pg_class.relkind values treated as user/application relations.
BOOTSTRAP_USER_RELKINDS = frozenset({"r", "p", "v", "m", "S", "f"})

LABEL_BOOTSTRAP_ID = "com.fetchnow.bootstrap-id"
LABEL_BOOTSTRAP_REVISION = "com.fetchnow.bootstrap-revision"

PRE_SCHEMA_MUTATION_STATUSES = frozenset(
    {
        STATUS_BOOTSTRAP_PLANNED,
        STATUS_BOOTSTRAP_POSTGRES_STARTED,
        STATUS_BOOTSTRAP_FRESHNESS_VERIFIED,
    }
)
