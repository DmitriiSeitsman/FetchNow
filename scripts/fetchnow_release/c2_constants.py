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

# PRD1D-B environment-aware overlays (new prepares only).
SOURCE_CONTRACT_VERSION_V3 = 3
REQUIRED_SOURCE_FILES_V3 = REQUIRED_SOURCE_FILES_V2 + (
    "compose.production.yaml",
)

SUPPORTED_SOURCE_CONTRACT_VERSIONS = frozenset(
    {
        SOURCE_CONTRACT_VERSION_V1,
        SOURCE_CONTRACT_VERSION_V2,
        SOURCE_CONTRACT_VERSION_V3,
    }
)
CURRENT_PREPARE_SOURCE_CONTRACT_VERSION = SOURCE_CONTRACT_VERSION_V3
DEPLOY_PLAN_MIN_SOURCE_CONTRACT_VERSION = SOURCE_CONTRACT_VERSION_V2

# Pinned v1/v2 hash set. Do not treat this as "current prepare contract".
CONTRACT_HASH_FILES_V1 = (
    "compose.yaml",
    "compose.staging.yaml",
    "backend/Dockerfile",
    "web/Dockerfile",
    "deploy/nginx/Dockerfile",
)
CONTRACT_HASH_FILES_V2 = CONTRACT_HASH_FILES_V1
CONTRACT_HASH_FILES_V3 = CONTRACT_HASH_FILES_V2 + ("compose.production.yaml",)

# Backward-compatible aliases (legacy v2 prepare contract).
REQUIRED_SOURCE_FILES = REQUIRED_SOURCE_FILES_V2
CONTRACT_HASH_FILES = CONTRACT_HASH_FILES_V1
FORBIDDEN_SOURCE_PATHS = (
    ".git",
    ".env",
    ".env.staging",
    ".env.production",
    "backend/.env",
    "web/.env",
)
LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1"
BUILD_PROJECT_NAME = "fetchnow-release-build"
