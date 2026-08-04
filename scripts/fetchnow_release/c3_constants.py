"""PRD1C3A constants for application rollout transactions."""

from __future__ import annotations

STATE_DIRNAME = "state"
CURRENT_STATE_NAME = "current.json"
DEPLOYMENTS_DIRNAME = "deployments"
ROLLOUT_LOCK_FILENAME = "rollout.lock"

PLAN_NAME = "plan.json"
RESULT_NAME = "result.json"
EVENTS_DIRNAME = "events"
OVERRIDES_DIRNAME = "overrides"
IMAGES_OVERRIDE_NAME = "images.yaml"

CURRENT_SCHEMA_VERSION = 1
PLAN_SCHEMA_VERSION = 1
RESULT_SCHEMA_VERSION = 1
EVENT_SCHEMA_VERSION = 1

APPLICATION_SERVICES = ("api", "worker", "web", "gateway")
APP_SERVICES_BEFORE_GATEWAY = ("api", "worker", "web")

# Journal / result statuses
STATUS_PLANNED = "planned"
STATUS_ACTIVATING_APP = "activating_app"
STATUS_APP_HEALTHY = "app_healthy"
STATUS_ACTIVATING_GATEWAY = "activating_gateway"
STATUS_STABILIZING = "stabilizing"
STATUS_COMMITTED = "committed"
STATUS_FAILED = "failed"
STATUS_ROLLBACK_STARTED = "rollback_started"
STATUS_ROLLED_BACK = "rolled_back"
STATUS_ROLLBACK_FAILED = "rollback_failed"
STATUS_BOOTSTRAP_CLEANUP_STARTED = "bootstrap_cleanup_started"
STATUS_BOOTSTRAP_CLEANED = "bootstrap_cleaned"

BOOTSTRAP_CLEANUP_AUDIT_PREFIX = "OK: bootstrap cleanup audit="

TERMINAL_RESULT_STATUSES = frozenset(
    {
        STATUS_COMMITTED,
        STATUS_FAILED,
        STATUS_ROLLED_BACK,
        STATUS_ROLLBACK_FAILED,
    }
)

# Legal transitions for the in-memory / event phase marker.
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_PLANNED: frozenset({STATUS_ACTIVATING_APP, STATUS_FAILED}),
    STATUS_ACTIVATING_APP: frozenset(
        {STATUS_APP_HEALTHY, STATUS_ROLLBACK_STARTED}
    ),
    STATUS_APP_HEALTHY: frozenset(
        {STATUS_ACTIVATING_GATEWAY, STATUS_ROLLBACK_STARTED}
    ),
    STATUS_ACTIVATING_GATEWAY: frozenset(
        {STATUS_STABILIZING, STATUS_ROLLBACK_STARTED}
    ),
    STATUS_STABILIZING: frozenset({STATUS_COMMITTED, STATUS_ROLLBACK_STARTED}),
    STATUS_ROLLBACK_STARTED: frozenset(
        {STATUS_ROLLED_BACK, STATUS_ROLLBACK_FAILED}
    ),
    STATUS_FAILED: frozenset(),
    STATUS_COMMITTED: frozenset(),
    STATUS_ROLLED_BACK: frozenset(),
    STATUS_ROLLBACK_FAILED: frozenset(),
}

# Production stabilization defaults (tests may inject a policy object).
STABILIZE_CONSECUTIVE_SUCCESSES = 3
STABILIZE_INTERVAL_SECONDS = 2.0
STABILIZE_WINDOW_SECONDS = 30.0
SERVICE_HEALTH_DEADLINE_SECONDS = 120.0
SERVICE_HEALTH_POLL_SECONDS = 2.0

RECOVERIES_DIRNAME = "recoveries"
RESOLUTION_NAME = "resolution.json"

RECOVERY_PLAN_SCHEMA_VERSION = 1
RESOLUTION_SCHEMA_VERSION = 1

RESOLUTION_OUTCOME_ACCEPT_TARGET = "accept_target"
RESOLUTION_OUTCOME_ROLLBACK = "rollback"

LABEL_DEPLOYMENT_ID = "com.fetchnow.deployment-id"
LABEL_RELEASE_REVISION = "com.fetchnow.release-revision"

RECOVERY_LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_PLANNED: frozenset(
        {STATUS_STABILIZING, STATUS_COMMITTED, STATUS_ROLLED_BACK, STATUS_FAILED}
    ),
    STATUS_STABILIZING: frozenset(
        {STATUS_COMMITTED, STATUS_ROLLED_BACK, STATUS_FAILED}
    ),
    STATUS_FAILED: frozenset(),
    STATUS_COMMITTED: frozenset(),
    STATUS_ROLLED_BACK: frozenset(),
}

RECOVERY_SUCCESS_STATUSES = frozenset({STATUS_COMMITTED, STATUS_ROLLED_BACK})

# Bootstrap-only post-mutation failure path (after ownership-verified cleanup).
BOOTSTRAP_FAILURE_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_ACTIVATING_APP: frozenset({STATUS_BOOTSTRAP_CLEANUP_STARTED}),
    STATUS_APP_HEALTHY: frozenset({STATUS_BOOTSTRAP_CLEANUP_STARTED}),
    STATUS_ACTIVATING_GATEWAY: frozenset({STATUS_BOOTSTRAP_CLEANUP_STARTED}),
    STATUS_STABILIZING: frozenset({STATUS_BOOTSTRAP_CLEANUP_STARTED}),
    STATUS_BOOTSTRAP_CLEANUP_STARTED: frozenset({STATUS_BOOTSTRAP_CLEANED}),
    STATUS_BOOTSTRAP_CLEANED: frozenset({STATUS_FAILED}),
    STATUS_FAILED: frozenset(),
}

ROLLOUT_PROJECT_PROTECTED = "fetchnow-staging"
ROLLOUT_TEST_PROJECT_PREFIX = "fetchnow-rollout-test-"
