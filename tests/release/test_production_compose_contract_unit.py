"""Text-level production Compose overlay contract (no Docker)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_FORBIDDEN_SHORT_NAME = re.compile(r"(?<![A-Za-z0-9-])fetchnow-prod(?![A-Za-z0-9-])")


def test_retired_compose_prod_yaml_is_gone() -> None:
    assert not (ROOT / "deploy" / "compose" / "compose.prod.yaml").exists()


def test_production_overlay_is_loopback_fail_closed() -> None:
    text = (ROOT / "compose.production.yaml").read_text(encoding="utf-8")
    assert ":-80" not in text
    assert "${GATEWAY_PORT:-80}" not in text
    assert "127.0.0.1" in text
    assert '"${GATEWAY_PORT:-127.0.0.1:8091}:8080"' in text
    assert "APP_ENV: production" in text
    assert _FORBIDDEN_SHORT_NAME.search(text) is None
    assert "fetchnow-production" in text
    assert "compose.override.yaml" in text
    assert "PUBLIC_SEARCH_INDEXING_ENABLED: ${PUBLIC_SEARCH_INDEXING_ENABLED:-false}" in text
    assert "POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set in .env.production" in text
    assert (
        "FETCHNOW_RELEASE_REVISION:?FETCHNOW_RELEASE_REVISION must be a full "
        "40-char lowercase Git SHA"
    ) in text


def test_production_env_example_is_placeholder_only() -> None:
    text = (ROOT / ".env.production.example").read_text(encoding="utf-8")
    assert "COMPOSE_PROJECT_NAME=fetchnow-production" in text
    assert "APP_ENV=production" in text
    assert "PUBLIC_SITE_URL=https://fetchnow.online" in text
    assert "FETCHNOW_DEPLOY_ROOT=/srv/fetchnow-production" in text
    assert "FETCHNOW_BACKUP_ROOT=/srv/fetchnow-production/backups" in text
    assert "GATEWAY_PORT=127.0.0.1:8091" in text
    assert "FETCHNOW_RELEASE_REVISION=0000000000000000000000000000000000000000" in text
    assert "POSTGRES_PASSWORD=replace-with-32plus-url-safe-production-password" in text
    assert "PUBLIC_SEARCH_INDEXING_ENABLED=false" in text
    assert "PUBLIC_MEDIA_FLOW_ENABLED=false" in text
    assert _FORBIDDEN_SHORT_NAME.search(text) is None
    staging = (ROOT / ".env.staging.example").read_text(encoding="utf-8")
    staging_keys = {
        line.split("=", 1)[0]
        for line in staging.splitlines()
        if line and not line.startswith("#") and "=" in line
    }
    production_keys = {
        line.split("=", 1)[0]
        for line in text.splitlines()
        if line and not line.startswith("#") and "=" in line
    }
    assert staging_keys == production_keys
