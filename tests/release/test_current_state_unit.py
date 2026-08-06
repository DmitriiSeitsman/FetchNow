"""Unit tests for PRD1C3B2A dual application/database current state."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fetchnow_release.application_compatibility import (  # noqa: E402
    CompatibilityError,
    DatabaseDriftError,
    application_compatible_with_database,
    assert_application_compatible_with_database,
    assert_live_matches_saved_database_heads,
)
from fetchnow_release.c3b2a_constants import (  # noqa: E402
    CURRENT_SCHEMA_VERSION_V1,
    CURRENT_SCHEMA_VERSION_V2,
)
from fetchnow_release.current_state import (  # noqa: E402
    ApplicationState,
    CurrentStateError,
    DatabaseState,
    LegacyCurrentStateV1,
    build_bootstrap_database_state,
    build_committed_current_state,
    current_path,
    load_parsed_current_state,
    next_database_state_after_app_commit,
    parse_current_state_bytes,
    parse_current_state_document,
    parse_current_state_v2,
    parse_legacy_current_state_v1,
    resolve_current_state,
    write_current_state,
    write_legacy_v1_fixture_for_tests,
)
from fetchnow_release.deploy_plan import _canonical_plan  # noqa: E402


def _ids() -> dict[str, str]:
    api = "sha256:" + ("a" * 64)
    return {
        "api": api,
        "worker": api,
        "web": "sha256:" + ("b" * 64),
        "gateway": "sha256:" + ("c" * 64),
    }


def _v1_dict(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "schema_version": 1,
        "revision": "a" * 40,
        "release_manifest_sha256": "d" * 64,
        "image_ids": _ids(),
        "updated_at_utc": "2026-01-01T00:00:00Z",
        "deployment_id": "12345678-1234-1234-1234-123456789abc",
    }
    base.update(overrides)
    return base


def _v2_dict(**overrides: object) -> dict[str, object]:
    rev = "a" * 40
    base: dict[str, object] = {
        "schema_version": 2,
        "application": {
            "revision": rev,
            "release_manifest_sha256": "d" * 64,
            "image_ids": _ids(),
            "deployment_id": "12345678-1234-1234-1234-123456789abc",
        },
        "database": {
            "heads": ["0001_baseline"],
            "schema_release_revision": rev,
            "last_migration_id": None,
            "compatibility_contract_sha256": None,
            "compatible_application_revisions": [rev],
        },
        "updated_at_utc": "2026-01-01T00:00:00Z",
    }
    base.update(overrides)
    return base


def test_valid_legacy_v1_parse() -> None:
    parsed = parse_legacy_current_state_v1(_v1_dict())
    assert parsed.schema_version == CURRENT_SCHEMA_VERSION_V1
    assert parsed.revision == "a" * 40


def test_valid_v2_parse() -> None:
    parsed = parse_current_state_v2(_v2_dict())
    assert parsed.schema_version == CURRENT_SCHEMA_VERSION_V2
    assert parsed.database.heads == ("0001_baseline",)


def test_unknown_v2_fields_rejected() -> None:
    raw = _v2_dict()
    raw["extra"] = True
    with pytest.raises(CurrentStateError, match="keys"):
        parse_current_state_v2(raw)


def test_bool_schema_version_rejected() -> None:
    with pytest.raises(CurrentStateError, match="exact integer"):
        parse_current_state_document(_v2_dict(schema_version=True))  # type: ignore[arg-type]


def test_string_schema_version_rejected() -> None:
    with pytest.raises(CurrentStateError, match="exact integer"):
        parse_current_state_document({**_v2_dict(), "schema_version": "2"})


def test_empty_heads_rejected() -> None:
    raw = _v2_dict()
    database = dict(raw["database"])  # type: ignore[arg-type]
    database["heads"] = []
    raw["database"] = database
    with pytest.raises(CurrentStateError, match="non-empty"):
        parse_current_state_v2(raw)


def test_unsorted_heads_rejected() -> None:
    raw = _v2_dict()
    database = dict(raw["database"])  # type: ignore[arg-type]
    database["heads"] = ["b_head", "a_head"]
    raw["database"] = database
    with pytest.raises(CurrentStateError, match="sorted"):
        parse_current_state_v2(raw)


def test_duplicate_heads_rejected() -> None:
    raw = _v2_dict()
    database = dict(raw["database"])  # type: ignore[arg-type]
    database["heads"] = ["0001_baseline", "0001_baseline"]
    raw["database"] = database
    with pytest.raises(CurrentStateError, match="unique"):
        parse_current_state_v2(raw)


def test_schema_release_missing_from_envelope_rejected() -> None:
    raw = _v2_dict()
    database = dict(raw["database"])  # type: ignore[arg-type]
    database["compatible_application_revisions"] = ["b" * 40]
    raw["database"] = database
    with pytest.raises(CurrentStateError, match="compatible_application_revisions"):
        parse_current_state_v2(raw)


def test_migration_id_hash_null_consistency() -> None:
    raw = _v2_dict()
    database = dict(raw["database"])  # type: ignore[arg-type]
    database["last_migration_id"] = "12345678-1234-1234-1234-123456789abc"
    raw["database"] = database
    with pytest.raises(CurrentStateError, match="both be null"):
        parse_current_state_v2(raw)


def test_symlink_state_rejected(tmp_path: Path) -> None:
    deploy = tmp_path / "deploy"
    (deploy / "state").mkdir(parents=True)
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = deploy / "state" / "current.json"
    link.symlink_to(target)
    with pytest.raises(CurrentStateError, match="symlink"):
        load_parsed_current_state(deploy)


def test_legacy_resolution_builds_dual_state(tmp_path: Path) -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    rev = "a" * 40
    write_legacy_v1_fixture_for_tests(
        deploy,
        LegacyCurrentStateV1(
            schema_version=1,
            revision=rev,
            release_manifest_sha256="d" * 64,
            image_ids=_ids(),
            updated_at_utc="2026-01-01T00:00:00Z",
            deployment_id="12345678-1234-1234-1234-123456789abc",
        ),
    )
    path = current_path(deploy)
    before = path.read_bytes()
    mtime = path.stat().st_mtime_ns
    parsed = load_parsed_current_state(deploy)
    assert isinstance(parsed, LegacyCurrentStateV1)

    fake_release = tmp_path / "releases" / rev
    (fake_release / "source" / "backend" / "migrations" / "versions").mkdir(parents=True)

    with (
        mock.patch(
            "fetchnow_release.current_state.verify_prepared_release",
            return_value=mock.Mock(ok=True, messages=()),
        ),
        mock.patch(
            "fetchnow_release.current_state.release_dir",
            return_value=fake_release,
        ),
        mock.patch(
            "fetchnow_release.current_state.target_heads_from_release_source",
            return_value=frozenset({"0001_baseline"}),
        ),
    ):
        resolved = resolve_current_state(
            parsed, deploy_root=deploy, repo_root=tmp_path
        )
    assert resolved.legacy_v1 is True
    assert resolved.database.heads == ("0001_baseline",)
    assert resolved.database.compatible_application_revisions == (rev,)
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == mtime


def test_legacy_resolution_missing_release_fails(tmp_path: Path) -> None:
    parsed = parse_legacy_current_state_v1(_v1_dict())
    with mock.patch(
        "fetchnow_release.current_state.release_dir",
        return_value=tmp_path / "missing",
    ):
        with pytest.raises(CurrentStateError, match="missing"):
            resolve_current_state(parsed, deploy_root=tmp_path, repo_root=tmp_path)


def test_compatibility_equal_heads_allowed() -> None:
    database = build_bootstrap_database_state(
        heads=frozenset({"0001_baseline"}),
        schema_release_revision="a" * 40,
    )
    decision = application_compatible_with_database(
        target_revision="b" * 40,
        target_source_heads=frozenset({"0001_baseline"}),
        database=database,
    )
    assert decision.allowed
    assert decision.via_equal_heads


def test_compatibility_recorded_envelope_allowed() -> None:
    database = DatabaseState(
        heads=("0002_expand",),
        schema_release_revision="b" * 40,
        last_migration_id=None,
        compatibility_contract_sha256=None,
        compatible_application_revisions=tuple(sorted(["a" * 40, "b" * 40])),
    )
    decision = application_compatible_with_database(
        target_revision="a" * 40,
        target_source_heads=frozenset({"0001_baseline"}),
        database=database,
    )
    assert decision.allowed
    assert decision.via_recorded_envelope
    assert not decision.via_equal_heads


def test_compatibility_unrelated_rejected() -> None:
    database = build_bootstrap_database_state(
        heads=frozenset({"0001_baseline"}),
        schema_release_revision="a" * 40,
    )
    with pytest.raises(CompatibilityError):
        assert_application_compatible_with_database(
            target_revision="c" * 40,
            target_source_heads=frozenset({"0009_other"}),
            database=database,
        )


def test_no_transitive_compatibility() -> None:
    database = DatabaseState(
        heads=("0002_expand",),
        schema_release_revision="b" * 40,
        last_migration_id=None,
        compatibility_contract_sha256=None,
        compatible_application_revisions=tuple(sorted(["a" * 40, "b" * 40])),
    )
    decision = application_compatible_with_database(
        target_revision="d" * 40,
        target_source_heads=frozenset({"0001_baseline"}),
        database=database,
    )
    assert not decision.allowed


def test_live_saved_drift_rejected() -> None:
    with pytest.raises(DatabaseDriftError, match="drift"):
        assert_live_matches_saved_database_heads(
            live_heads=frozenset({"0002_expand"}),
            saved_heads=("0001_baseline",),
        )


def test_next_database_state_extends_envelope() -> None:
    previous = build_bootstrap_database_state(
        heads=frozenset({"0001_baseline"}),
        schema_release_revision="a" * 40,
    )
    nxt = next_database_state_after_app_commit(
        previous=previous,
        target_revision="b" * 40,
        target_source_heads=frozenset({"0001_baseline"}),
    )
    assert nxt.heads == previous.heads
    assert nxt.schema_release_revision == previous.schema_release_revision
    assert set(nxt.compatible_application_revisions) == {"a" * 40, "b" * 40}


def test_next_database_state_preserves_for_envelope_target() -> None:
    previous = DatabaseState(
        heads=("0002_expand",),
        schema_release_revision="b" * 40,
        last_migration_id="12345678-1234-1234-1234-123456789abc",
        compatibility_contract_sha256="e" * 64,
        compatible_application_revisions=tuple(sorted(["a" * 40, "b" * 40])),
    )
    nxt = next_database_state_after_app_commit(
        previous=previous,
        target_revision="a" * 40,
        target_source_heads=frozenset({"0001_baseline"}),
    )
    assert nxt == previous


def test_writer_publishes_v2_0600(tmp_path: Path) -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    rev = "a" * 40
    write_current_state(
        deploy,
        build_committed_current_state(
            application=ApplicationState(
                revision=rev,
                release_manifest_sha256="d" * 64,
                image_ids=_ids(),
                deployment_id="12345678-1234-1234-1234-123456789abc",
            ),
            database=build_bootstrap_database_state(
                heads=frozenset({"0001_baseline"}),
                schema_release_revision=rev,
            ),
            updated_at_utc="2026-01-01T00:00:00Z",
        ),
    )
    path = current_path(deploy)
    assert path.is_file()
    assert (path.stat().st_mode & 0o777) == 0o600
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema_version"] == 2


def test_deploy_plan_field_application_rollout_required() -> None:
    payload = {
        "application_rollout_required": True,
        "migration_required": False,
        "schema_version": 1,
    }
    text = _canonical_plan(payload)
    assert '"application_rollout_required":true' in text
    assert '"migration_required":false' in text


def test_parse_bytes_roundtrip() -> None:
    raw = _v2_dict()
    parsed = parse_current_state_bytes(
        (json.dumps(raw, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    assert parsed.schema_version == 2


def test_application_must_be_in_envelope() -> None:
    raw = _v2_dict()
    application = dict(raw["application"])  # type: ignore[arg-type]
    application["revision"] = "f" * 40
    raw["application"] = application
    with pytest.raises(CurrentStateError, match="compatible_application_revisions"):
        parse_current_state_v2(raw)
