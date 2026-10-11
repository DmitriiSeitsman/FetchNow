"""Prove V4 manifests pass the real overlay resolver on release paths.

These tests call the same ``resolve_runtime_overlay`` /
``snapshot_compose_files_for_release`` entry points used by verify→rollout,
health, deploy-plan, and recover. The resolver itself is not mocked.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fetchnow_release.c2_constants import (  # noqa: E402
    SOURCE_CONTRACT_VERSION_V4,
)
from fetchnow_release.environment import (  # noqa: E402
    resolve_runtime_overlay,
    snapshot_compose_files_for_release,
)
from fetchnow_release.health import (  # noqa: E402
    resolve_runtime_overlay as health_resolve_runtime_overlay,
)
from fetchnow_release.manifest import parse_manifest  # noqa: E402
from fetchnow_release.recover import (  # noqa: E402
    snapshot_compose_files_for_release as recover_snapshot,
)
from fetchnow_release.release_mode import (  # noqa: E402
    RELEASE_MODE_CANONICAL,
    RELEASE_MODE_MEDIA,
)
from fetchnow_release.rollout import (  # noqa: E402
    snapshot_compose_files_for_release as rollout_snapshot,
)


def _v4_manifest(*, mode: str, overlay: str, rev: str = "a" * 40) -> dict[str, object]:
    images: list[dict[str, str]] = [
        {
            "service": "api",
            "reference": f"fetchnow-api:{rev}",
            "image_id": "sha256:" + ("1" * 64),
            "oci_revision": rev,
            "platform": "linux/amd64",
        },
        {
            "service": "web",
            "reference": f"fetchnow-web:{rev}",
            "image_id": "sha256:" + ("2" * 64),
            "oci_revision": rev,
            "platform": "linux/amd64",
        },
        {
            "service": "gateway",
            "reference": f"fetchnow-gateway:{rev}",
            "image_id": "sha256:" + ("3" * 64),
            "oci_revision": rev,
            "platform": "linux/amd64",
        },
    ]
    if mode == RELEASE_MODE_MEDIA:
        for svc, ref, n in (
            ("media-executor", "fetchnow-media-executor", "4"),
            ("media-net-executor", "fetchnow-media-net-executor", "5"),
            ("egress-proxy", "fetchnow-media-egress-proxy", "6"),
        ):
            images.append(
                {
                    "service": svc,
                    "reference": f"{ref}:{rev}",
                    "image_id": "sha256:" + (n * 64),
                    "oci_revision": rev,
                    "platform": "linux/amd64",
                }
            )
    return {
        "schema_version": 1,
        "source_contract_version": SOURCE_CONTRACT_VERSION_V4,
        "revision": rev,
        "tree_oid": "b" * 40,
        "created_at_utc": "2026-01-01T00:00:00Z",
        "archive_commit_verified": True,
        "source_repository": "FetchNow",
        "compose_version": "2.0.0",
        "docker_version": "29.0.0",
        "build_platform": "linux/amd64",
        "images": images,
        "contract_hashes": {"compose.yaml": "c" * 64},
        "tool_version": "test",
        "status": "prepared",
        "compose_overlay": overlay,
        "release_mode": mode,
    }


def _prepared_release(tmp_path: Path, *, overlay: str) -> Path:
    release = tmp_path / "releases" / ("a" * 40)
    source = release / "source"
    source.mkdir(parents=True)
    (source / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (source / overlay).write_text("services: {}\n", encoding="utf-8")
    return release


def test_v4_canonical_manifest_resolves_via_rollout_snapshot(tmp_path: Path) -> None:
    overlay = "compose.staging.yaml"
    release = _prepared_release(tmp_path, overlay=overlay)
    manifest = parse_manifest(_v4_manifest(mode=RELEASE_MODE_CANONICAL, overlay=overlay))
    files = rollout_snapshot(
        release,
        project_name="fetchnow-rollout-test-abcd1234",
        manifest=manifest,
    )
    assert files[1].name == overlay
    assert files[1].parent == (release / "source").resolve()


def test_v4_media_manifest_resolves_via_recover_and_environment(
    tmp_path: Path,
) -> None:
    overlay = "compose.staging.yaml"
    release = _prepared_release(tmp_path, overlay=overlay)
    manifest = parse_manifest(_v4_manifest(mode=RELEASE_MODE_MEDIA, overlay=overlay))
    via_env = snapshot_compose_files_for_release(
        release,
        project_name="fetchnow-rollout-test-abcd1234",
        manifest=manifest,
    )
    via_recover = recover_snapshot(
        release,
        project_name="fetchnow-rollout-test-abcd1234",
        manifest=manifest,
    )
    assert via_env == via_recover
    assert via_env[1].name == overlay


def test_v4_health_path_calls_same_resolver() -> None:
    assert health_resolve_runtime_overlay is resolve_runtime_overlay
    assert (
        health_resolve_runtime_overlay(
            project_name="fetchnow-staging",
            source_contract_version=SOURCE_CONTRACT_VERSION_V4,
            compose_overlay="compose.staging.yaml",
        )
        == "compose.staging.yaml"
    )


def test_v4_verify_compatible_manifest_feeds_resolver(tmp_path: Path) -> None:
    """verify_release loads/validates the manifest; overlay resolve uses it."""
    overlay = "compose.production.yaml"
    release = _prepared_release(tmp_path, overlay=overlay)
    raw = _v4_manifest(mode=RELEASE_MODE_CANONICAL, overlay=overlay)
    manifest = parse_manifest(raw)
    ns = SimpleNamespace(
        source_contract_version=manifest.source_contract_version,
        compose_overlay=manifest.compose_overlay,
    )
    files = snapshot_compose_files_for_release(
        release,
        project_name="fetchnow-rollout-test-abcd1234",
        manifest=ns,
    )
    assert files[1].name == overlay
