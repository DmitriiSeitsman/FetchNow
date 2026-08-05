"""PRD1C3B1 constants for migration compatibility and deploy planning."""

from __future__ import annotations

COMPATIBILITY_SCHEMA_VERSION = 1
COMPATIBILITY_REL_PATH = "deploy/migrations/compatibility.json"

DEPLOY_PLAN_SCHEMA_VERSION = 1

EXECUTION_MODE_ONLINE_EXPAND = "online_expand"
DATA_LOSS_NONE = "none"
DATABASE_DOWNGRADE_FORBIDDEN = "forbidden"

REVISION_ID_RE = r"^[a-zA-Z0-9_]+$"

MAX_RATIONALE_LENGTH = 2000
MIN_RATIONALE_LENGTH = 10

PLACEHOLDER_RATIONALE_MARKERS = frozenset(
    {
        "todo",
        "tbd",
        "fixme",
        "placeholder",
        "xxx",
        "lorem ipsum",
        "coming soon",
        "n/a",
    }
)

DEPLOY_PLAN_TEST_PROJECT_PREFIX = "fetchnow-deploy-plan-test-"
