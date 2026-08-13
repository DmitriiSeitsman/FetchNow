"""FetchNow release identity, preflight, health, materialization & rollout."""

from __future__ import annotations

__version__ = "1.2.0"

STAGING_PROJECT = "fetchnow-staging"
EXPECTED_SERVICES = ("gateway", "api", "worker", "delivery", "postgres", "web")
POSTGRES_IMAGE_PREFIX = "postgres:16.9"
OCI_REVISION_LABEL = "org.opencontainers.image.revision"
DEFAULT_GATEWAY_LOOPBACK = "127.0.0.1"
DEFAULT_GATEWAY_PORT = 8091
MIN_PASSWORD_LENGTH = 32
PASSWORD_ALLOWED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~-"
)
PLACEHOLDER_PASSWORD_MARKERS = (
    "replace-with",
    "changeme",
    "password",
    "example",
    "fetchnow",
)
TRUSTED_MAIN_REF = "refs/remotes/origin/main"
PLACEHOLDER_REVISION = "0000000000000000000000000000000000000000"
MIN_FREE_BYTES = 1 * 1024 * 1024 * 1024  # 1 GiB documented margin
HEALTH_HTTP_TIMEOUT_SECONDS = 3.0
HEALTH_RETRY_DELAY_SECONDS = 1.0
HEALTH_OVERALL_DEADLINE_SECONDS = 60.0
HEALTH_MAX_BODY_BYTES = 4096
