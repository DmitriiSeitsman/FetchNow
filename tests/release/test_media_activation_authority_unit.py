"""F3/F4: config-rollout refuse-on-media-ON + required active budget."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from fetchnow_release.health import (  # noqa: E402
    HealthMediaExpectation,
    validate_health_media_expectation,
)
from fetchnow_release.media_state import (  # noqa: E402
    MediaStateError,
    inactive_media_state,
    parse_media_activation_state,
    write_media_activation_state,
)
from fetchnow_release.media_storage import (  # noqa: E402
    disposable_test_storage_contract,
)
from fetchnow_release.resource_budget import disposable_test_budget  # noqa: E402

REV = "a" * 40


def _on_raw(**overrides: object) -> dict:
    budget = disposable_test_budget().to_dict()
    raw: dict = {
        "schema_version": 1,
        "activation": True,
        "release_mode": "media",
        "revision": REV,
        "media_image_ids": {
            "media-executor": "sha256:" + ("1" * 64),
            "media-net-executor": "sha256:" + ("2" * 64),
            "egress-proxy": "sha256:" + ("3" * 64),
        },
        "overlays": [
            "compose.media-executor.yaml",
            "compose.media-net-executor.yaml",
        ],
        "profiles": ["media-executor", "media-net-executor"],
        "executor_settings": {
            "MEDIA_EXECUTOR_ENABLED": "true",
            "MEDIA_NET_EXECUTOR_ENABLED": "true",
            "MEDIA_EXECUTOR_SOCKET": "/run/fetchnow-executor/ctrl/worker.sock",
            "MEDIA_NET_EXECUTOR_SOCKET": "/run/fetchnow-media-net/ctrl/worker.sock",
            "MEDIA_EXECUTOR_WORK_ROOT": "/var/lib/fetchnow/executor",
            "MEDIA_NET_EXECUTOR_WORK_ROOT": "/var/lib/fetchnow/media-net",
        },
        "updated_at_utc": "2026-01-01T00:00:00Z",
        "resource_budget": budget,
        "slice_unit": "fetchnow-media-executor.slice",
        "denied_host": "127.0.0.1",
        "storage_contract": disposable_test_storage_contract(
            host_bind_root="/var/lib/fetchnow-test-media-storage"
        ),
    }
    raw.update(overrides)
    return raw


def test_active_media_requires_valid_approved_budget() -> None:
    parsed = parse_media_activation_state(_on_raw())
    assert parsed.activation is True
    assert parsed.resource_budget is not None
    assert parsed.resource_budget["approved"] is True
    assert parsed.storage_contract is not None
    assert parsed.storage_contract["approved"] is True

    with pytest.raises(MediaStateError, match="resource_budget"):
        parse_media_activation_state(_on_raw(resource_budget=None))

    missing = _on_raw()
    del missing["resource_budget"]
    with pytest.raises(MediaStateError, match="resource_budget"):
        parse_media_activation_state(missing)

    bad = disposable_test_budget().to_dict()
    bad["approved"] = False
    with pytest.raises(MediaStateError, match="resource_budget"):
        parse_media_activation_state(_on_raw(resource_budget=bad))

    malformed = disposable_test_budget().to_dict()
    del malformed["aggregate_memory_max_bytes"]
    with pytest.raises(MediaStateError, match="resource_budget"):
        parse_media_activation_state(_on_raw(resource_budget=malformed))

    with pytest.raises(MediaStateError, match="storage_contract"):
        parse_media_activation_state(_on_raw(storage_contract=None))

    missing_storage = _on_raw()
    del missing_storage["storage_contract"]
    with pytest.raises(MediaStateError, match="storage_contract"):
        parse_media_activation_state(missing_storage)


def test_inactive_media_allows_missing_budget_roundtrip(tmp_path: Path) -> None:
    deploy = tmp_path / "deploy"
    (deploy / "state").mkdir(parents=True)
    state = inactive_media_state(
        revision=REV, updated_at_utc="2026-01-01T00:00:00Z"
    )
    assert state.resource_budget is None
    path = write_media_activation_state(deploy, state)
    loaded = parse_media_activation_state(json.loads(path.read_text(encoding="utf-8")))
    assert loaded.activation is False
    assert loaded.resource_budget is None


def test_on_health_expectation_requires_budget() -> None:
    ids = {
        "media-executor": "sha256:" + ("1" * 64),
        "media-net-executor": "sha256:" + ("2" * 64),
        "egress-proxy": "sha256:" + ("3" * 64),
    }
    with pytest.raises(Exception, match="resource_budget"):
        validate_health_media_expectation(
            HealthMediaExpectation(
                activation=True,
                media_image_ids=ids,
                denied_host="127.0.0.1",
                resource_budget=None,
            )
        )
    ok = validate_health_media_expectation(
        HealthMediaExpectation(
            activation=True,
            media_image_ids=ids,
            denied_host="127.0.0.1",
            resource_budget=disposable_test_budget().to_dict(),
            storage_contract=disposable_test_storage_contract(host_bind_root="/test/media"),
        )
    )
    assert ok.activation is True
