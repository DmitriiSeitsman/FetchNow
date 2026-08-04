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
REQUIRED_SOURCE_FILES = (
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
