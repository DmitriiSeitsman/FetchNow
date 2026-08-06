"""PRD1C3B2A constants for dual application/database current state."""

from __future__ import annotations

CURRENT_SCHEMA_VERSION_V1 = 1
CURRENT_SCHEMA_VERSION_V2 = 2

# Successful writers always publish schema v2.
CURRENT_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION_V2

APPLICATION_STATE_KEYS = frozenset(
    {"revision", "release_manifest_sha256", "image_ids", "deployment_id"}
)
DATABASE_STATE_KEYS = frozenset(
    {
        "heads",
        "schema_release_revision",
        "last_migration_id",
        "compatibility_contract_sha256",
        "compatible_application_revisions",
    }
)
CURRENT_STATE_V2_TOP_KEYS = frozenset(
    {"schema_version", "application", "database", "updated_at_utc"}
)
CURRENT_STATE_V1_TOP_KEYS = frozenset(
    {
        "schema_version",
        "revision",
        "release_manifest_sha256",
        "image_ids",
        "updated_at_utc",
        "deployment_id",
    }
)
