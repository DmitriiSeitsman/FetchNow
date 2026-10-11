"""Offline unit regressions for SEC-08/09 release integration."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fetchnow_release.c2_constants import (  # noqa: E402
    CURRENT_PREPARE_SOURCE_CONTRACT_VERSION,
    SOURCE_CONTRACT_VERSION_V3,
    SOURCE_CONTRACT_VERSION_V4,
)
from fetchnow_release.config_contract import (  # noqa: E402
    INFRASTRUCTURE_ACTIVATION_WIRING,
    RUNTIME_CONFIG_ALLOWLIST,
    assert_infrastructure_activation_not_config_rollout,
)
from fetchnow_release.egress_deny_coverage import (  # noqa: E402
    EgressDenyCoverageError,
    assert_deny_coverage,
)
from fetchnow_release.journal import PlanDocument, parse_plan  # noqa: E402
from fetchnow_release.manifest import (  # noqa: E402
    ImageRecord,
    ManifestError,
    ReleaseManifest,
    parse_manifest,
    validate_manifest,
)
from fetchnow_release.media_activation import (  # noqa: E402
    MediaActivationError,
    assert_executor_flags_off,
    build_activation_plan,
    build_source_only_plan,
)
from fetchnow_release.media_state import (  # noqa: E402
    inactive_media_state,
    parse_media_activation_state,
    write_media_activation_state,
)
from fetchnow_release.release_mode import (  # noqa: E402
    RELEASE_MODE_CANONICAL,
    RELEASE_MODE_MEDIA,
    build_services_for_mode,
    expected_image_services,
)
from fetchnow_release.resource_budget import (  # noqa: E402
    ResourceBudgetError,
    disposable_test_budget,
    parse_resource_budget,
)


def _img(service: str, n: str, rev: str) -> ImageRecord:
    from fetchnow_release.release_mode import image_reference_for_service

    return ImageRecord(
        service=service,
        reference=image_reference_for_service(service, rev),
        image_id="sha256:" + (n * 64),
        oci_revision=rev,
        platform="linux/amd64",
    )


def _canonical_manifest(*, rev: str = "a" * 40, version: int = 3) -> dict:
    images = [
        _img("api", "1", rev),
        _img("web", "2", rev),
        _img("gateway", "3", rev),
    ]
    raw = {
        "schema_version": 1,
        "source_contract_version": version,
        "revision": rev,
        "tree_oid": "b" * 40,
        "created_at_utc": "2026-01-01T00:00:00Z",
        "archive_commit_verified": True,
        "source_repository": "FetchNow",
        "compose_version": "2.0.0",
        "docker_version": "29.0.0",
        "build_platform": "linux/amd64",
        "images": [
            {
                "service": i.service,
                "reference": i.reference,
                "image_id": i.image_id,
                "oci_revision": i.oci_revision,
                "platform": i.platform,
            }
            for i in images
        ],
        "contract_hashes": {"compose.yaml": "c" * 64},
        "tool_version": "test",
        "status": "prepared",
        "compose_overlay": "compose.production.yaml",
    }
    if version >= SOURCE_CONTRACT_VERSION_V4:
        raw["release_mode"] = RELEASE_MODE_CANONICAL
    return raw


def test_current_prepare_is_v4() -> None:
    assert CURRENT_PREPARE_SOURCE_CONTRACT_VERSION == SOURCE_CONTRACT_VERSION_V4


def test_legacy_v3_manifest_defaults_canonical_without_media_images() -> None:
    manifest = parse_manifest(_canonical_manifest(version=3))
    assert manifest.release_mode == RELEASE_MODE_CANONICAL
    assert {i.service for i in manifest.images} == {"api", "web", "gateway"}
    # Stable omit of release_mode for legacy docs.
    assert "release_mode" not in manifest.to_dict()


def test_v4_canonical_does_not_require_media_images() -> None:
    raw = _canonical_manifest(version=4)
    manifest = parse_manifest(raw)
    assert manifest.release_mode == RELEASE_MODE_CANONICAL
    assert expected_image_services(RELEASE_MODE_CANONICAL) == frozenset(
        {"api", "web", "gateway"}
    )


def test_v4_media_requires_sidecar_images() -> None:
    rev = "a" * 40
    raw = _canonical_manifest(version=4, rev=rev)
    raw["release_mode"] = RELEASE_MODE_MEDIA
    with pytest.raises(ManifestError, match="images services"):
        parse_manifest(raw)
    for svc, n in (
        ("media-executor", "4"),
        ("media-net-executor", "5"),
        ("egress-proxy", "6"),
    ):
        img = _img(svc, n, rev)
        raw["images"].append(
            {
                "service": img.service,
                "reference": img.reference,
                "image_id": img.image_id,
                "oci_revision": img.oci_revision,
                "platform": img.platform,
            }
        )
    manifest = parse_manifest(raw)
    assert set(build_services_for_mode(RELEASE_MODE_MEDIA)) == {
        i.service for i in manifest.images
    }


def test_identity_mismatch_fails_validation() -> None:
    rev = "a" * 40
    raw = _canonical_manifest(version=4, rev=rev)
    raw["images"][0]["image_id"] = "sha256:" + ("9" * 63) + "g"
    with pytest.raises(ManifestError, match="malformed Docker image ID"):
        parse_manifest(raw)


def test_source_only_plan_keeps_flags_off() -> None:
    plan = build_source_only_plan(release_mode=RELEASE_MODE_CANONICAL)
    assert plan.activation is False
    assert plan.overlays == ()
    assert plan.executor_settings["MEDIA_EXECUTOR_ENABLED"] == "false"
    assert plan.executor_settings["MEDIA_NET_EXECUTOR_ENABLED"] == "false"


def test_explicit_activation_requires_media_manifest() -> None:
    rev = "a" * 40
    manifest = parse_manifest(_canonical_manifest(version=4, rev=rev))
    with pytest.raises(MediaActivationError, match="media-mode"):
        build_activation_plan(
            manifest=manifest,
            required_deny_cidrs=("172.18.0.0/16",),
            required_deny_ips=("10.0.0.1",),
        )


def test_deny_coverage_not_mere_nonempty() -> None:
    with pytest.raises(EgressDenyCoverageError, match="missing required"):
        assert_deny_coverage(
            deny_cidrs="10.0.0.0/8",
            deny_ips="1.2.3.4",
            required_cidrs=("172.18.0.0/16",),
            required_ips=("195.209.215.87",),
        )
    assert_deny_coverage(
        deny_cidrs="172.18.0.0/16,172.17.0.0/16",
        deny_ips="195.209.215.87",
        required_cidrs=("172.18.0.0/16",),
        required_ips=("195.209.215.87",),
    )


def test_resource_budget_not_auto_approved() -> None:
    with pytest.raises(ResourceBudgetError, match="approved"):
        parse_resource_budget(
            {
                "approved": False,
                "aggregate_memory_max_bytes": 1024**3,
                "aggregate_cpu_quota_milli": 500,
                "aggregate_tasks_max": 256,
                "per_container_memory_bytes": {
                    "media-executor": 1024**3,
                    "media-net-executor": 1536 * 1024**2,
                    "egress-proxy": 256 * 1024**2,
                },
                "concurrency": 1,
                "shared_ceiling": True,
                "notes": "would conflict with 1GiB slice",
            }
        )
    budget = disposable_test_budget()
    assert budget.approved is True
    assert budget.shared_ceiling is True


def test_config_rollout_cannot_enable_executor_keys() -> None:
    assert not (set(INFRASTRUCTURE_ACTIVATION_WIRING) & set(RUNTIME_CONFIG_ALLOWLIST))
    with pytest.raises(Exception, match="infrastructure activation"):
        assert_infrastructure_activation_not_config_rollout(("MEDIA_EXECUTOR_ENABLED",))


def test_hidden_flag_enable_refused_without_activation() -> None:
    with pytest.raises(MediaActivationError, match="media_activation"):
        assert_executor_flags_off({"MEDIA_EXECUTOR_ENABLED": "true"})


def test_legacy_plan_without_media_still_parses() -> None:
    plan = parse_plan(
        {
            "schema_version": 1,
            "deployment_id": "11111111-1111-1111-1111-111111111111",
            "target_revision": "a" * 40,
            "previous_revision": "b" * 40,
            "bootstrap": False,
            "target_image_ids": {
                "api": "sha256:" + ("1" * 64),
                "worker": "sha256:" + ("1" * 64),
                "web": "sha256:" + ("2" * 64),
                "gateway": "sha256:" + ("3" * 64),
            },
            "previous_image_ids": {
                "api": "sha256:" + ("4" * 64),
                "worker": "sha256:" + ("4" * 64),
                "web": "sha256:" + ("5" * 64),
                "gateway": "sha256:" + ("6" * 64),
            },
            "release_manifest_sha256": "c" * 64,
            "database_heads": ["0001_initial"],
            "created_at_utc": "2026-01-01T00:00:00Z",
            "project_name": "fetchnow-rollout-test-abcd1234",
        }
    )
    assert plan.media is None


def test_plan_media_rejects_secrets() -> None:
    with pytest.raises(Exception, match="secret"):
        parse_plan(
            {
                "schema_version": 1,
                "deployment_id": "11111111-1111-1111-1111-111111111111",
                "target_revision": "a" * 40,
                "previous_revision": None,
                "bootstrap": True,
                "target_image_ids": {
                    "api": "sha256:" + ("1" * 64),
                    "worker": "sha256:" + ("1" * 64),
                    "web": "sha256:" + ("2" * 64),
                    "gateway": "sha256:" + ("3" * 64),
                },
                "previous_image_ids": None,
                "release_manifest_sha256": "c" * 64,
                "database_heads": ["0001_initial"],
                "created_at_utc": "2026-01-01T00:00:00Z",
                "project_name": "fetchnow-rollout-test-abcd1234",
                "media": {
                    "activation": False,
                    "release_mode": "canonical",
                    "overlays": [],
                    "profiles": [],
                    "media_image_ids": {},
                    "executor_settings": {"MEDIA_EXECUTOR_ENABLED": "false"},
                    "password": "nope",
                },
            }
        )


def test_previous_state_media_journal_off_to_on() -> None:
    doc = PlanDocument(
        schema_version=1,
        deployment_id="11111111-1111-1111-1111-111111111111",
        target_revision="a" * 40,
        previous_revision="b" * 40,
        bootstrap=False,
        target_image_ids={
            "api": "sha256:" + ("1" * 64),
            "worker": "sha256:" + ("1" * 64),
            "web": "sha256:" + ("2" * 64),
            "gateway": "sha256:" + ("3" * 64),
        },
        previous_image_ids={
            "api": "sha256:" + ("4" * 64),
            "worker": "sha256:" + ("4" * 64),
            "web": "sha256:" + ("5" * 64),
            "gateway": "sha256:" + ("6" * 64),
        },
        release_manifest_sha256="c" * 64,
        database_heads=("0001_initial",),
        created_at_utc="2026-01-01T00:00:00Z",
        project_name="fetchnow-rollout-test-abcd1234",
        media={
            "activation": True,
            "release_mode": "media",
            "overlays": [
                "compose.media-executor.yaml",
                "compose.media-net-executor.yaml",
            ],
            "profiles": ["media-executor", "media-net-executor"],
            "media_image_ids": {
                "media-executor": "sha256:" + ("7" * 64),
                "media-net-executor": "sha256:" + ("8" * 64),
                "egress-proxy": "sha256:" + ("9" * 64),
            },
            "executor_settings": {
                "MEDIA_EXECUTOR_ENABLED": "true",
                "MEDIA_NET_EXECUTOR_ENABLED": "true",
                "MEDIA_EXECUTOR_SOCKET": "/run/fetchnow-executor/ctrl/worker.sock",
                "MEDIA_EXECUTOR_WORK_ROOT": "/var/lib/fetchnow/executor",
                "MEDIA_NET_EXECUTOR_SOCKET": "/run/fetchnow-media-net/ctrl/worker.sock",
                "MEDIA_NET_EXECUTOR_WORK_ROOT": "/var/lib/fetchnow/media-net",
            },
            "required_deny_cidrs": ["172.18.0.0/16"],
            "required_deny_ips": ["10.0.0.1"],
            "previous_activation": False,
            "previous_media_image_ids": {},
            "previous_executor_settings": {
                "MEDIA_EXECUTOR_ENABLED": "false",
                "MEDIA_NET_EXECUTOR_ENABLED": "false",
            },
        },
    )
    parsed = parse_plan(doc.to_dict())
    assert parsed.media is not None
    assert parsed.media["activation"] is True
    assert parsed.media["previous_activation"] is False


def test_media_state_roundtrip(tmp_path: Path) -> None:
    deploy = tmp_path / "deploy"
    (deploy / "state").mkdir(parents=True)
    state = inactive_media_state(
        revision="a" * 40, updated_at_utc="2026-01-01T00:00:00Z"
    )
    path = write_media_activation_state(deploy, state)
    loaded = parse_media_activation_state(json.loads(path.read_text(encoding="utf-8")))
    assert loaded.activation is False
    assert "password" not in path.read_text(encoding="utf-8").lower()


def test_v3_manifest_still_validates_without_release_mode_field() -> None:
    manifest = ReleaseManifest(
        schema_version=1,
        source_contract_version=SOURCE_CONTRACT_VERSION_V3,
        revision="a" * 40,
        tree_oid="b" * 40,
        created_at_utc="2026-01-01T00:00:00Z",
        archive_commit_verified=True,
        source_repository="FetchNow",
        compose_version="2",
        docker_version="29",
        build_platform="linux/amd64",
        images=(
            _img("api", "1", "a" * 40),
            _img("web", "2", "a" * 40),
            _img("gateway", "3", "a" * 40),
        ),
        contract_hashes={"compose.yaml": "c" * 64},
        tool_version="t",
        status="prepared",
        compose_overlay="compose.staging.yaml",
        release_mode=RELEASE_MODE_CANONICAL,
    )
    validate_manifest(manifest)
