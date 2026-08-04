"""FetchNow PostgreSQL logical backup / restore-verify tooling (PRD1B)."""

from __future__ import annotations

__version__ = "1.0.0"

TOOL_NAME = "fetchnow_pg_backup"
MANIFEST_SCHEMA_VERSION = 1
VERIFICATION_SCHEMA_VERSION = 1
DUMP_FILENAME = "database.dump"
MANIFEST_FILENAME = "manifest.json"
VERIFICATIONS_DIRNAME = "verifications"
LOCK_FILENAME = ".lock"
INCOMPLETE_PREFIX = ".incomplete-"
RESTORE_DB_PREFIX = "fetchnow_restore_verify_"
DEFAULT_KEEP_COUNT = 7
DEFAULT_COMPRESSION = "gzip:6"
ARCHIVE_FORMAT = "custom"
# Alembic metadata table created by Alembic itself (not invented).
ALEMBIC_VERSION_TABLE = "alembic_version"
# Minimum required public tables after current head migrations.
REQUIRED_PUBLIC_TABLES = (ALEMBIC_VERSION_TABLE,)
FREE_SPACE_MARGIN_BYTES = 256 * 1024 * 1024
FREE_SPACE_SIZE_MULTIPLIER = 2
