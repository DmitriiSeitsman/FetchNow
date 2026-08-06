"""PRD1C2 constants for release materialization & image build."""

from __future__ import annotations

RELEASE_SCHEMA_VERSION = 1
RELEASE_MANIFEST_NAME = "release.json"
RELEASE_STATUS_PREPARED = "prepared"
RELEASES_DIRNAME = "releases"
LOCKS_DIRNAME = "locks"
LOCK_FILENAME = ".lock"
SOURCE_DIRNAME = "source"

APPLICATION_BUILD_SERVICES = ("api", "web", "gateway")

# Pinned PRD1C2/PRD1C3A legacy source contract (immutable — do not extend).
SOURCE_CONTRACT_VERSION_V1 = 1
REQUIRED_SOURCE_FILES_V1 = (
    "compose.yaml",
    "compose.staging.yaml",
    "backend/Dockerfile",
    "web/Dockerfile",
    "deploy/nginx/Dockerfile",
    "backend/pyproject.toml",
    "backend/src",
    "web/package.json",
    "web/package-lock.json",
    "deploy/nginx/nginx.conf",
)

# PRD1C3B1 migration compatibility policy (new prepares only).
SOURCE_CONTRACT_VERSION_V2 = 2
REQUIRED_SOURCE_FILES_V2 = REQUIRED_SOURCE_FILES_V1 + (
    "deploy/migrations/compatibility.json",
)

SUPPORTED_SOURCE_CONTRACT_VERSIONS = frozenset(
    {SOURCE_CONTRACT_VERSION_V1, SOURCE_CONTRACT_VERSION_V2}
)
CURRENT_PREPARE_SOURCE_CONTRACT_VERSION = SOURCE_CONTRACT_VERSION_V2
DEPLOY_PLAN_MIN_SOURCE_CONTRACT_VERSION = SOURCE_CONTRACT_VERSION_V2

# Backward-compatible alias for callers that mean "current prepare contract".
REQUIRED_SOURCE_FILES = REQUIRED_SOURCE_FILES_V2
CONTRACT_HASH_FILES = (
    "compose.yaml",
    "compose.staging.yaml",
    "backend/Dockerfile",
    "web/Dockerfile",
    "deploy/nginx/Dockerfile",
)
FORBIDDEN_SOURCE_PATHS = (
    ".git",
    ".env",
    ".env.staging",
    "backend/.env",
    "web/.env",
)
LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1"
BUILD_PROJECT_NAME = "fetchnow-release-build"
