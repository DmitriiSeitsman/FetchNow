"""Unit tests for versioned release source contracts (PRD1C3B1)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fetchnow_release.archive import (  # noqa: E402
    ArchiveError,
    detect_lfs_pointers,
    verify_required_files,
)
from fetchnow_release.c2_constants import (  # noqa: E402
    REQUIRED_SOURCE_FILES_V1,
    REQUIRED_SOURCE_FILES_V2,
    SOURCE_CONTRACT_VERSION_V1,
    SOURCE_CONTRACT_VERSION_V2,
)
from fetchnow_release.manifest import (  # noqa: E402
    ImageRecord,
    ManifestError,
    ReleaseManifest,
    parse_manifest,
    parse_source_contract_version,
)
from fetchnow_release.source_contract import (  # noqa: E402
    assert_deploy_plan_target_contract,
    prepare_source_contract_version,
)
from fetchnow_release.verify_release import verify_prepared_release  # noqa: E402


def _write_tree(base: Path, rels: tuple[str, ...]) -> None:
    for rel in rels:
        path = base / rel
        if rel.endswith("/") or rel in {"backend/src"}:
            path.mkdir(parents=True, exist_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"fixture:{rel}\n", encoding="utf-8")


def _sample_manifest(
    *, source_contract_version: int | None, rev: str
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "revision": rev,
        "tree_oid": "b" * 40,
        "created_at_utc": "2026-01-01T00:00:00Z",
        "archive_commit_verified": True,
        "source_repository": "FetchNow",
        "compose_version": "2",
        "docker_version": "24",
        "build_platform": "linux/amd64",
        "contract_hashes": {
            "compose.yaml": "c" * 64,
            "compose.staging.yaml": "d" * 64,
            "backend/Dockerfile": "e" * 64,
            "web/Dockerfile": "f" * 64,
            "deploy/nginx/Dockerfile": "0" * 64,
        },
        "tool_version": "1.1.0",
        "status": "prepared",
        "images": [
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
        ],
    }
    if source_contract_version is not None:
        payload["source_contract_version"] = source_contract_version
    return payload


def test_legacy_required_file_set_is_pinned() -> None:
    assert "deploy/migrations/compatibility.json" not in REQUIRED_SOURCE_FILES_V1
    assert REQUIRED_SOURCE_FILES_V2[-1] == "deploy/migrations/compatibility.json"
    assert (
        REQUIRED_SOURCE_FILES_V2[: len(REQUIRED_SOURCE_FILES_V1)]
        == REQUIRED_SOURCE_FILES_V1
    )


def test_legacy_manifest_omits_field_defaults_to_v1() -> None:
    rev = "a" * 40
    manifest = parse_manifest(_sample_manifest(source_contract_version=None, rev=rev))
    assert manifest.source_contract_version == SOURCE_CONTRACT_VERSION_V1


def test_v1_verification_without_compatibility_json(tmp_path: Path) -> None:
    src = tmp_path / "source"
    _write_tree(src, REQUIRED_SOURCE_FILES_V1)
    verify_required_files(src, source_contract_version=SOURCE_CONTRACT_VERSION_V1)
    (src / "deploy" / "migrations").mkdir(parents=True, exist_ok=True)
    (src / "deploy/migrations/compatibility.json").write_text("{}\n", encoding="utf-8")
    verify_required_files(src, source_contract_version=SOURCE_CONTRACT_VERSION_V1)


def test_v1_manifest_with_extra_policy_not_promoted(tmp_path: Path) -> None:
    rev = "a" * 40
    release = tmp_path / rev
    source = release / "source"
    _write_tree(source, REQUIRED_SOURCE_FILES_V1)
    (source / "deploy" / "migrations").mkdir(parents=True, exist_ok=True)
    compat = source / "deploy/migrations/compatibility.json"
    compat.write_text("{}\n", encoding="utf-8")
    contract_hashes = verify_required_files(
        source, source_contract_version=SOURCE_CONTRACT_VERSION_V1
    )
    raw = _sample_manifest(source_contract_version=1, rev=rev)
    raw["contract_hashes"] = contract_hashes
    manifest = parse_manifest(raw)
    (release / "release.json").write_text(
        json.dumps(manifest.to_dict(), indent=2) + "\n", encoding="utf-8"
    )
    with (
        patch("fetchnow_release.verify_release._assert_readonly_tree"),
        patch("fetchnow_release.verify_release.image_exists", return_value=True),
        patch(
            "fetchnow_release.verify_release.inspect_image",
            side_effect=lambda ref: {
                "Id": {
                    f"fetchnow-api:{rev}": "sha256:" + ("1" * 64),
                    f"fetchnow-web:{rev}": "sha256:" + ("2" * 64),
                    f"fetchnow-gateway:{rev}": "sha256:" + ("3" * 64),
                }[ref],
                "Config": {"Labels": {"org.opencontainers.image.revision": rev}},
            },
        ),
    ):
        result = verify_prepared_release(release, expected_revision=rev)
    assert result.ok
    assert manifest.source_contract_version == SOURCE_CONTRACT_VERSION_V1


def test_v2_requires_compatibility_json(tmp_path: Path) -> None:
    src = tmp_path / "source"
    _write_tree(src, REQUIRED_SOURCE_FILES_V1)
    with pytest.raises(ArchiveError, match="required source path missing"):
        verify_required_files(src, source_contract_version=SOURCE_CONTRACT_VERSION_V2)


def test_v2_manifest_without_policy_fails_verify(tmp_path: Path) -> None:
    rev = "a" * 40
    release = tmp_path / rev
    source = release / "source"
    _write_tree(source, REQUIRED_SOURCE_FILES_V1)
    manifest = parse_manifest(_sample_manifest(source_contract_version=2, rev=rev))
    (release / "release.json").write_text(
        json.dumps(manifest.to_dict(), indent=2) + "\n", encoding="utf-8"
    )
    with (
        patch("fetchnow_release.verify_release._assert_readonly_tree"),
        patch("fetchnow_release.verify_release.image_exists", return_value=True),
        patch(
            "fetchnow_release.verify_release.inspect_image",
            return_value={
                "Id": "sha256:" + ("1" * 64),
                "Config": {"Labels": {"org.opencontainers.image.revision": rev}},
            },
        ),
    ):
        result = verify_prepared_release(release, expected_revision=rev)
    assert not result.ok


def test_unknown_source_contract_version_rejected() -> None:
    rev = "a" * 40
    raw = _sample_manifest(source_contract_version=9, rev=rev)
    with pytest.raises(ManifestError, match="unsupported source_contract_version"):
        parse_manifest(raw)


def test_source_contract_version_bool_rejected() -> None:
    with pytest.raises(ManifestError, match="must be integer"):
        parse_source_contract_version(True)


def test_source_contract_version_string_rejected() -> None:
    with pytest.raises(ManifestError, match="must be integer"):
        parse_source_contract_version("2")


def test_new_prepare_emits_v2_contract() -> None:
    assert prepare_source_contract_version() == SOURCE_CONTRACT_VERSION_V2


def test_deploy_plan_rejects_v1_target() -> None:
    with pytest.raises(ValueError, match="deploy-plan targets"):
        assert_deploy_plan_target_contract(SOURCE_CONTRACT_VERSION_V1)


def test_v1_idempotent_manifest_bytes_unchanged(tmp_path: Path) -> None:
    rev = "a" * 40
    release = tmp_path / "releases" / rev
    source = release / "source"
    source.mkdir(parents=True)
    _write_tree(source, REQUIRED_SOURCE_FILES_V1)
    manifest = ReleaseManifest(
        schema_version=1,
        source_contract_version=SOURCE_CONTRACT_VERSION_V1,
        revision=rev,
        tree_oid="b" * 40,
        created_at_utc="2026-01-01T00:00:00Z",
        archive_commit_verified=True,
        source_repository="FetchNow",
        compose_version="2",
        docker_version="24",
        build_platform="linux/amd64",
        images=(
            ImageRecord(
                "api",
                f"fetchnow-api:{rev}",
                "sha256:" + ("1" * 64),
                rev,
                "linux/amd64",
            ),
            ImageRecord(
                "web",
                f"fetchnow-web:{rev}",
                "sha256:" + ("2" * 64),
                rev,
                "linux/amd64",
            ),
            ImageRecord(
                "gateway",
                f"fetchnow-gateway:{rev}",
                "sha256:" + ("3" * 64),
                rev,
                "linux/amd64",
            ),
        ),
        contract_hashes={"compose.yaml": "c" * 64},
        tool_version="1.1.0",
        status="prepared",
    )
    man_path = release / "release.json"
    write_bytes = json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n"
    man_path.write_text(write_bytes, encoding="utf-8")
    before = man_path.read_bytes()
    with (
        patch("fetchnow_release.prepare.verify_prepared_release") as verify,
        patch("fetchnow_release.prepare.release_dir", return_value=release),
        patch(
            "fetchnow_release.prepare.ensure_deploy_layout",
            return_value=(tmp_path / "releases", tmp_path / "locks"),
        ),
        patch("fetchnow_release.prepare.ReleaseRootLock"),
        patch("fetchnow_release.prepare.run_preflight") as preflight,
        patch("fetchnow_release.prepare.assert_clean_worktree"),
        patch("fetchnow_release.prepare.assert_revision_in_origin_main"),
        patch("fetchnow_release.prepare.validate_deploy_root", return_value=tmp_path),
    ):
        verify.return_value.ok = True
        verify.return_value.messages = ("OK: release verification passed",)
        preflight.return_value.ok = True
        preflight.return_value.messages = ("OK: preflight",)
        from fetchnow_release.prepare import PrepareInput, prepare_release

        result = prepare_release(
            PrepareInput(
                project_name="fetchnow-staging",
                env_file=tmp_path / ".env",
                compose_files=(tmp_path / "compose.yaml",),
                expected_revision=rev,
                repo_root=tmp_path,
                deploy_root=tmp_path,
            )
        )
    assert result.ok and result.already_prepared
    assert man_path.read_bytes() == before


def test_lfs_scan_respects_contract_version(tmp_path: Path) -> None:
    src = tmp_path / "source"
    src.mkdir()
    from fetchnow_release.c2_constants import LFS_POINTER_PREFIX

    (src / "compose.yaml").write_bytes(LFS_POINTER_PREFIX + b"\noid sha256:abc\n")
    with pytest.raises(ArchiveError, match="LFS"):
        detect_lfs_pointers(src, source_contract_version=SOURCE_CONTRACT_VERSION_V1)
