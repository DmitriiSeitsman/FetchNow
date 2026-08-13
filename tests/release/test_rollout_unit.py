"""Unit tests for PRD1C3A rollout journal, overrides, and safety contracts."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fetchnow_release.activate import (  # noqa: E402
    ActivateError,
    build_activate_argv,
    build_storage_init_argv,
)
from fetchnow_release.c3_constants import (  # noqa: E402
    BOOTSTRAP_CLEANUP_AUDIT_PREFIX,
    BOOTSTRAP_FAILURE_TRANSITIONS,
    LABEL_DEPLOYMENT_ID,
    LABEL_RELEASE_REVISION,
    LEGAL_TRANSITIONS,
    RESOLUTION_OUTCOME_ROLLBACK,
    RESOLUTION_SCHEMA_VERSION,
    STATUS_ACTIVATING_APP,
    STATUS_ACTIVATING_GATEWAY,
    STATUS_APP_HEALTHY,
    STATUS_BOOTSTRAP_CLEANED,
    STATUS_BOOTSTRAP_CLEANUP_STARTED,
    STATUS_COMMITTED,
    STATUS_FAILED,
    STATUS_INITIALIZING_STORAGE,
    STATUS_PLANNED,
    STATUS_ROLLBACK_FAILED,
    STATUS_ROLLBACK_STARTED,
    STATUS_ROLLED_BACK,
    STATUS_STABILIZING,
)
from fetchnow_release.cli import build_parser  # noqa: E402
from fetchnow_release.db_heads import assert_heads_equal, DbHeadsError  # noqa: E402
from fetchnow_release.image_build import image_exists  # noqa: E402
from fetchnow_release.journal import (  # noqa: E402
    JournalError,
    PlanDocument,
    RecoveryPlanDocument,
    ResolutionDocument,
    append_bootstrap_failure_event,
    append_event,
    append_recovery_event,
    assert_legal_transition,
    begin_bootstrap_cleanup_event,
    deployment_dir,
    deployment_blocks_rollout,
    find_unresolved_deployments,
    load_current_state,
    new_recovery_id,
    parse_result,
    publish_bootstrap_failed_after_cleanup,
    recovery_dir,
    result_path,
    sha256_file,
    validate_bootstrap_failed_result,
    write_plan,
    write_recovery_plan,
    write_recovery_result,
    write_resolution,
    write_result,
)
from fetchnow_release.override import (  # noqa: E402
    OverrideError,
    compose_files_include_storage_init,
    render_images_override,
)
from fetchnow_release.recover import RecoverInput, recover_deployment  # noqa: E402
from fetchnow_release.redact import redact  # noqa: E402
from fetchnow_release.rollout_lock import RolloutLock, RolloutLockError  # noqa: E402
from fetchnow_release.rollout_project import (  # noqa: E402
    RolloutProjectError,
    assert_rollout_project,
    make_rollout_project_name,
)


def _ids() -> dict[str, str]:
    api = "sha256:" + ("a" * 64)
    return {
        "api": api,
        "worker": api,
        "web": "sha256:" + ("b" * 64),
        "gateway": "sha256:" + ("c" * 64),
    }


def test_all_illegal_transitions() -> None:
    for current, allowed in LEGAL_TRANSITIONS.items():
        for nxt in LEGAL_TRANSITIONS:
            if nxt == current:
                continue
            if nxt in allowed:
                assert_legal_transition(current, nxt)
            else:
                with pytest.raises(JournalError, match="illegal"):
                    assert_legal_transition(current, nxt)


def test_first_event_must_be_planned(tmp_path: Path) -> None:
    dep = tmp_path / "dep"
    dep.mkdir()
    (dep / "events").mkdir()
    with pytest.raises(JournalError, match="first journal event"):
        append_event(dep, STATUS_ACTIVATING_APP)


def test_planned_to_committed_is_illegal() -> None:
    with pytest.raises(JournalError, match="illegal"):
        assert_legal_transition(STATUS_PLANNED, STATUS_COMMITTED)


def test_atomic_current_json(tmp_path: Path) -> None:
    from fetchnow_release.current_state import (
        ApplicationState,
        CurrentStateError,
        build_bootstrap_database_state,
        build_committed_current_state,
        load_parsed_current_state,
        write_current_state,
    )

    deploy = tmp_path / "deploy"
    deploy.mkdir()
    rev = "a" * 40
    state = build_committed_current_state(
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
    )
    write_current_state(deploy, state)
    loaded = load_current_state(deploy)
    assert loaded is not None
    assert loaded.revision == rev
    assert loaded.database.heads == ("0001_baseline",)
    parsed = load_parsed_current_state(deploy)
    assert parsed is not None
    assert getattr(parsed, "schema_version", None) == 2
    with pytest.raises(CurrentStateError):
        write_current_state(
            deploy,
            build_committed_current_state(
                application=ApplicationState(
                    revision=rev,
                    release_manifest_sha256="d" * 64,
                    image_ids={"api": "bad"},
                    deployment_id="12345678-1234-1234-1234-123456789abc",
                ),
                database=build_bootstrap_database_state(
                    heads=frozenset({"0001_baseline"}),
                    schema_release_revision=rev,
                ),
                updated_at_utc="2026-01-01T00:00:00Z",
            ),
        )


def test_plan_immutable_and_events_monotonic(tmp_path: Path) -> None:
    deploy = tmp_path / "deploy"
    dep_id = "12345678-1234-1234-1234-123456789abc"
    dep = deployment_dir(deploy, dep_id)
    plan = PlanDocument(
        schema_version=1,
        deployment_id=dep_id,
        target_revision="a" * 40,
        previous_revision=None,
        bootstrap=True,
        target_image_ids=_ids(),
        previous_image_ids=None,
        release_manifest_sha256="e" * 64,
        database_heads=("0001_baseline",),
        created_at_utc="2026-01-01T00:00:00Z",
        project_name="fetchnow-rollout-test-abc123",
    )
    write_plan(dep, plan)
    with pytest.raises(JournalError, match="immutable"):
        write_plan(dep, plan)
    append_event(dep, STATUS_PLANNED)
    append_event(dep, STATUS_ACTIVATING_APP)
    with pytest.raises(JournalError, match="illegal"):
        append_event(dep, STATUS_COMMITTED)


def test_deployment_result_is_immutable(tmp_path: Path) -> None:
    from fetchnow_release.journal_io import read_json

    dep = tmp_path / "dep"
    dep.mkdir()
    dep_id = "12345678-1234-1234-1234-123456789abc"
    write_result(
        dep,
        deployment_id=dep_id,
        status=STATUS_ROLLBACK_FAILED,
        messages=("rollback failed",),
    )
    before = (dep / "result.json").read_bytes()
    mtime = (dep / "result.json").stat().st_mtime_ns
    assert parse_result(read_json(dep / "result.json")).status == STATUS_ROLLBACK_FAILED
    with pytest.raises(JournalError, match="already published"):
        write_result(
            dep,
            deployment_id=dep_id,
            status=STATUS_ROLLED_BACK,
            messages=("again",),
        )
    assert (dep / "result.json").read_bytes() == before
    assert (dep / "result.json").stat().st_mtime_ns == mtime


def test_recovery_creates_separate_directory_and_resolution(tmp_path: Path) -> None:
    deploy = tmp_path / "deploy"
    dep_id = "12345678-1234-1234-1234-123456789abc"
    dep = deployment_dir(deploy, dep_id)
    write_result(
        dep,
        deployment_id=dep_id,
        status=STATUS_ROLLBACK_FAILED,
        messages=("rollback failed",),
    )
    rec_id = new_recovery_id()
    rec = recovery_dir(dep, rec_id)
    write_recovery_plan(
        rec,
        RecoveryPlanDocument(
            schema_version=1,
            recovery_id=rec_id,
            deployment_id=dep_id,
            action="rollback",
            created_at_utc="2026-01-01T00:00:00Z",
        ),
    )
    append_recovery_event(rec, STATUS_PLANNED)
    append_recovery_event(rec, STATUS_ROLLED_BACK)
    write_recovery_result(
        rec,
        recovery_id=rec_id,
        status=STATUS_ROLLED_BACK,
        messages=("OK: recovery rollback",),
    )
    write_resolution(
        dep,
        ResolutionDocument(
            schema_version=RESOLUTION_SCHEMA_VERSION,
            deployment_id=dep_id,
            deployment_result_sha256=sha256_file(result_path(dep)),
            recovery_id=rec_id,
            recovery_result_sha256=sha256_file(rec / "result.json"),
            final_active_revision="a" * 40,
            outcome=RESOLUTION_OUTCOME_ROLLBACK,
            resolved_at_utc="2026-01-01T00:00:01Z",
        ),
    )
    assert not deployment_blocks_rollout(dep)
    assert find_unresolved_deployments(deploy) == []


def test_malformed_resolution_still_blocks(tmp_path: Path) -> None:
    deploy = tmp_path / "deploy"
    dep_id = "12345678-1234-1234-1234-123456789abc"
    dep = deployment_dir(deploy, dep_id)
    dep.mkdir(parents=True)
    write_result(
        dep,
        deployment_id=dep_id,
        status=STATUS_ROLLBACK_FAILED,
        messages=("rollback failed",),
    )
    (dep / "resolution.json").write_text('{"schema_version":1}', encoding="utf-8")
    assert deployment_blocks_rollout(dep)
    assert find_unresolved_deployments(deploy) == [dep_id]


def test_resolution_hash_mismatch_blocks(tmp_path: Path) -> None:
    deploy = tmp_path / "deploy"
    dep_id = "12345678-1234-1234-1234-123456789abc"
    dep = deployment_dir(deploy, dep_id)
    write_result(
        dep,
        deployment_id=dep_id,
        status=STATUS_ROLLBACK_FAILED,
        messages=("rollback failed",),
    )
    rec_id = new_recovery_id()
    rec = recovery_dir(dep, rec_id)
    write_recovery_result(
        rec,
        recovery_id=rec_id,
        status=STATUS_ROLLED_BACK,
        messages=("OK",),
    )
    write_recovery_plan(
        rec,
        RecoveryPlanDocument(
            schema_version=1,
            recovery_id=rec_id,
            deployment_id=dep_id,
            action="rollback",
            created_at_utc="2026-01-01T00:00:00Z",
        ),
    )
    with pytest.raises(JournalError, match="mismatch"):
        write_resolution(
            dep,
            ResolutionDocument(
                schema_version=RESOLUTION_SCHEMA_VERSION,
                deployment_id=dep_id,
                deployment_result_sha256="f" * 64,
                recovery_id=rec_id,
                recovery_result_sha256=sha256_file(rec / "result.json"),
                final_active_revision="a" * 40,
                outcome=RESOLUTION_OUTCOME_ROLLBACK,
                resolved_at_utc="2026-01-01T00:00:01Z",
            ),
        )
    assert deployment_blocks_rollout(dep)


def test_committed_deployment_does_not_block(tmp_path: Path) -> None:
    deploy = tmp_path / "deploy"
    dep_id = "12345678-1234-1234-1234-123456789abc"
    dep = deployment_dir(deploy, dep_id)
    write_result(
        dep,
        deployment_id=dep_id,
        status=STATUS_COMMITTED,
        messages=("OK",),
    )
    assert not deployment_blocks_rollout(dep)
    assert find_unresolved_deployments(deploy) == []


def test_override_renders_ownership_labels() -> None:
    dep_id = "12345678-1234-1234-1234-123456789abc"
    rev = "a" * 40
    text = render_images_override(
        _ids(), deployment_id=dep_id, release_revision=rev
    )
    assert LABEL_DEPLOYMENT_ID in text
    assert LABEL_RELEASE_REVISION in text
    assert dep_id in text
    assert rev in text
    assert "postgres" not in text


def test_malformed_result_is_unresolved(tmp_path: Path) -> None:
    deploy = tmp_path / "deploy"
    dep = (
        deploy
        / "deployments"
        / "12345678-1234-1234-1234-123456789abc"
    )
    dep.mkdir(parents=True)
    (dep / "plan.json").write_text("{}", encoding="utf-8")
    (dep / "result.json").write_text('{"status":"committed"}', encoding="utf-8")
    assert find_unresolved_deployments(deploy) == [
        "12345678-1234-1234-1234-123456789abc"
    ]


def test_images_override_pins_ids_excludes_postgres() -> None:
    text = render_images_override(_ids())
    assert "sha256:" + ("a" * 64) in text
    assert "pull_policy: never" in text
    assert "postgres" not in text
    with pytest.raises(OverrideError):
        render_images_override({**_ids(), "worker": "sha256:" + ("f" * 64)})


def test_images_override_delivery_is_release_shape_aware() -> None:
    legacy = render_images_override(_ids(), include_delivery=False)
    current = render_images_override(_ids(), include_delivery=True)
    assert "\n  delivery:\n" not in legacy
    assert "\n  delivery:\n" in current
    assert current.count("sha256:" + ("a" * 64)) >= 3


def test_images_override_storage_init_pins_api_image_id() -> None:
    legacy = render_images_override(_ids(), include_storage_init=False)
    current = render_images_override(_ids(), include_storage_init=True)
    assert "\n  storage-init:\n" not in legacy
    assert "\n  storage-init:\n" in current
    assert f"    image: sha256:{'a' * 64}" in current
    assert current.count("sha256:" + ("a" * 64)) >= 3


def test_storage_init_detected_from_verified_compose_source(tmp_path: Path) -> None:
    pr9 = tmp_path / "compose.yaml"
    pr9.write_text("services:\n  storage-init:\n    image: x\n", encoding="utf-8")
    pr8 = tmp_path / "other.yaml"
    pr8.write_text("services:\n  api:\n    image: x\n", encoding="utf-8")
    named = tmp_path / "compose.staging.yaml"
    named.write_text("services:\n  storage-init:\n    image: x\n", encoding="utf-8")
    assert compose_files_include_storage_init((pr9,)) is True
    assert compose_files_include_storage_init((pr8,)) is False
    assert compose_files_include_storage_init((named,)) is False


def test_storage_init_argv_contract() -> None:
    argv = build_storage_init_argv(
        project_name="fetchnow-staging",
        env_file=Path("/tmp/env"),
        compose_files=(Path("/tmp/compose.yaml"), Path("/tmp/override.yaml")),
    )
    assert "--rm" in argv
    assert "--no-deps" in argv
    assert "--no-build" in argv
    assert "--pull" in argv and "never" in argv
    assert "postgres" not in argv
    assert argv[-1] == "storage-init"


def test_initializing_storage_journal_transitions() -> None:
    assert_legal_transition(STATUS_PLANNED, STATUS_INITIALIZING_STORAGE)
    assert_legal_transition(STATUS_INITIALIZING_STORAGE, STATUS_ACTIVATING_APP)
    assert_legal_transition(STATUS_INITIALIZING_STORAGE, STATUS_FAILED)
    with pytest.raises(JournalError, match="illegal"):
        assert_legal_transition(STATUS_INITIALIZING_STORAGE, STATUS_COMMITTED)


def test_activate_argv_contract() -> None:
    argv = build_activate_argv(
        project_name="fetchnow-staging",
        env_file=Path("/tmp/env"),
        compose_files=(Path("/tmp/compose.yaml"), Path("/tmp/override.yaml")),
        services=("api", "worker", "web"),
    )
    assert "--no-build" in argv
    assert "--no-deps" in argv
    assert "--pull" in argv and "never" in argv
    assert "postgres" not in argv
    assert argv[-3:] == ["api", "worker", "web"]
    with pytest.raises(ActivateError, match="postgres"):
        build_activate_argv(
            project_name="fetchnow-staging",
            env_file=Path("/tmp/env"),
            compose_files=(Path("/tmp/c.yaml"),),
            services=("api", "postgres"),  # type: ignore[arg-type]
        )


def test_db_heads_equality() -> None:
    assert_heads_equal(
        database_heads=frozenset({"0001_baseline"}),
        target_heads=frozenset({"0001_baseline"}),
    )
    with pytest.raises(DbHeadsError, match="PRD1C3B"):
        assert_heads_equal(
            database_heads=frozenset({"0001_baseline"}),
            target_heads=frozenset({"0002_next"}),
        )


def test_alembic_branched_heads(tmp_path: Path) -> None:
    from fetchnow_pg_backup.alembic_graph import discover_alembic_head_revisions

    versions = tmp_path / "versions"
    versions.mkdir()
    (versions / "0001_a.py").write_text(
        'revision = "0001_a"\ndown_revision = None\n',
        encoding="utf-8",
    )
    (versions / "0002_b.py").write_text(
        'revision = "0002_b"\ndown_revision = "0001_a"\n',
        encoding="utf-8",
    )
    (versions / "0003_c.py").write_text(
        'revision = "0003_c"\ndown_revision = "0001_a"\n',
        encoding="utf-8",
    )
    heads = discover_alembic_head_revisions(versions)
    assert heads == ("0002_b", "0003_c")


def test_alembic_cycle_fails_closed(tmp_path: Path) -> None:
    from fetchnow_pg_backup.alembic_graph import AlembicGraphError, discover_alembic_head_revisions

    versions = tmp_path / "versions"
    versions.mkdir()
    (versions / "0001_a.py").write_text(
        'revision = "0001_a"\ndown_revision = "0002_b"\n',
        encoding="utf-8",
    )
    (versions / "0002_b.py").write_text(
        'revision = "0002_b"\ndown_revision = "0001_a"\n',
        encoding="utf-8",
    )
    with pytest.raises(AlembicGraphError, match="cycle"):
        discover_alembic_head_revisions(versions)


def test_unresolved_blocks(tmp_path: Path) -> None:
    deploy = tmp_path / "deploy"
    dep = (
        deploy
        / "deployments"
        / "12345678-1234-1234-1234-123456789abc"
    )
    dep.mkdir(parents=True)
    (dep / "plan.json").write_text("{}", encoding="utf-8")
    found = find_unresolved_deployments(deploy)
    assert found == ["12345678-1234-1234-1234-123456789abc"]


def test_rollout_lock_contention(tmp_path: Path) -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    with RolloutLock(deploy, wait=False):
        with pytest.raises(RolloutLockError, match="lock"):
            with RolloutLock(deploy, wait=False):
                pass


def test_project_name_validator() -> None:
    with pytest.raises(RolloutProjectError):
        assert_rollout_project("fetchnow-staging")
    with pytest.raises(RolloutProjectError):
        assert_rollout_project("fetchnow")
    with pytest.raises(RolloutProjectError):
        assert_rollout_project("fetchnow-rollout-test")
    name = make_rollout_project_name()
    assert name.startswith("fetchnow-rollout-test-")
    assert_rollout_project(name)


def test_cli_has_no_unsafe_bypass() -> None:
    help_text = build_parser().format_help()
    for banned in (
        "skip-health",
        "allow-dirty",
        "skip-preflight",
        "skip-db-check",
        "test-mode",
        "allow_test_project",
    ):
        assert banned not in help_text
    assert "rollout" in help_text
    assert "recover" in help_text


def test_secret_redaction_in_rollout_context() -> None:
    text = "DATABASE_URL=postgresql://u:secret@host/db POSTGRES_PASSWORD=hunter2"
    out = redact(text)
    assert "hunter2" not in out
    assert "secret@" not in out


def test_recover_rejects_missing_journal(tmp_path: Path) -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    result = recover_deployment(
        RecoverInput(
            project_name="fetchnow-rollout-test-abc12345",
            env_file=Path("/tmp/env"),
            deploy_root=deploy,
            repo_root=ROOT,
            deployment_id="12345678-1234-1234-1234-123456789abc",
            action="accept-target",
        )
    )
    assert not result.ok
    assert "journal not found" in result.messages[0]


def test_previous_image_missing_blocks_before_mutation() -> None:
    missing = "sha256:" + ("f" * 64)
    assert not image_exists(missing)


def test_rollback_transition_paths() -> None:
    for phase in (
        STATUS_ACTIVATING_APP,
        STATUS_APP_HEALTHY,
        STATUS_ACTIVATING_GATEWAY,
        STATUS_STABILIZING,
    ):
        assert_legal_transition(phase, STATUS_ROLLBACK_STARTED)
    assert_legal_transition(STATUS_ROLLBACK_STARTED, STATUS_ROLLED_BACK)
    assert_legal_transition(STATUS_ROLLBACK_STARTED, STATUS_ROLLBACK_FAILED)


def test_resolution_is_write_once(tmp_path: Path) -> None:
    deploy = tmp_path / "deploy"
    dep_id = "12345678-1234-1234-1234-123456789abc"
    dep = deployment_dir(deploy, dep_id)
    write_result(
        dep,
        deployment_id=dep_id,
        status=STATUS_ROLLBACK_FAILED,
        messages=("rollback failed",),
    )
    rec_id = new_recovery_id()
    rec = recovery_dir(dep, rec_id)
    write_recovery_result(
        rec,
        recovery_id=rec_id,
        status=STATUS_ROLLED_BACK,
        messages=("OK",),
    )
    write_recovery_plan(
        rec,
        RecoveryPlanDocument(
            schema_version=1,
            recovery_id=rec_id,
            deployment_id=dep_id,
            action="rollback",
            created_at_utc="2026-01-01T00:00:00Z",
        ),
    )
    resolution = ResolutionDocument(
        schema_version=RESOLUTION_SCHEMA_VERSION,
        deployment_id=dep_id,
        deployment_result_sha256=sha256_file(result_path(dep)),
        recovery_id=rec_id,
        recovery_result_sha256=sha256_file(rec / "result.json"),
        final_active_revision="a" * 40,
        outcome=RESOLUTION_OUTCOME_ROLLBACK,
        resolved_at_utc="2026-01-01T00:00:01Z",
    )
    write_resolution(dep, resolution)
    with pytest.raises(JournalError, match="already published"):
        write_resolution(dep, resolution)


def _bootstrap_plan(dep_id: str = "12345678-1234-1234-1234-123456789abc") -> PlanDocument:
    return PlanDocument(
        schema_version=1,
        deployment_id=dep_id,
        target_revision="a" * 40,
        previous_revision=None,
        bootstrap=True,
        target_image_ids=_ids(),
        previous_image_ids=None,
        release_manifest_sha256="e" * 64,
        database_heads=("0001_baseline",),
        created_at_utc="2026-01-01T00:00:00Z",
        project_name="fetchnow-rollout-test-abc123",
    )


def _seed_post_mutation_journal(dep: Path, phase: str) -> PlanDocument:
    plan = _bootstrap_plan()
    write_plan(dep, plan)
    append_event(dep, STATUS_PLANNED)
    for status in (
        STATUS_ACTIVATING_APP,
        STATUS_APP_HEALTHY,
        STATUS_ACTIVATING_GATEWAY,
        STATUS_STABILIZING,
    ):
        append_event(dep, status)
        if status == phase:
            break
    return plan


def test_post_mutation_phases_cannot_transition_directly_to_failed() -> None:
    for phase in (
        STATUS_ACTIVATING_APP,
        STATUS_APP_HEALTHY,
        STATUS_ACTIVATING_GATEWAY,
        STATUS_STABILIZING,
    ):
        with pytest.raises(JournalError, match="illegal"):
            assert_legal_transition(phase, STATUS_FAILED)


def test_bootstrap_failure_transitions_require_cleanup_chain() -> None:
    for phase in (
        STATUS_ACTIVATING_APP,
        STATUS_APP_HEALTHY,
        STATUS_ACTIVATING_GATEWAY,
        STATUS_STABILIZING,
    ):
        allowed = BOOTSTRAP_FAILURE_TRANSITIONS[phase]
        assert allowed == frozenset({STATUS_BOOTSTRAP_CLEANUP_STARTED})
    assert BOOTSTRAP_FAILURE_TRANSITIONS[STATUS_BOOTSTRAP_CLEANUP_STARTED] == frozenset(
        {STATUS_BOOTSTRAP_CLEANED}
    )
    assert BOOTSTRAP_FAILURE_TRANSITIONS[STATUS_BOOTSTRAP_CLEANED] == frozenset(
        {STATUS_FAILED}
    )


def test_bootstrap_failed_result_requires_cleanup_audit(tmp_path: Path) -> None:
    dep = tmp_path / "dep"
    dep.mkdir()
    plan = _bootstrap_plan()
    with pytest.raises(JournalError, match="cleanup audit"):
        validate_bootstrap_failed_result(plan, ("FAIL: target rollout error",))
    audit = (
        BOOTSTRAP_CLEANUP_AUDIT_PREFIX
        + '[{"service":"api","container_id":"abc123"}]'
    )
    validate_bootstrap_failed_result(plan, (audit,))


def test_bootstrap_failed_publishes_cleanup_chain_and_result(tmp_path: Path) -> None:
    from fetchnow_release.journal_io import read_json

    dep = tmp_path / "dep"
    dep.mkdir()
    plan = _seed_post_mutation_journal(dep, STATUS_STABILIZING)
    audit = (
        BOOTSTRAP_CLEANUP_AUDIT_PREFIX
        + '[{"service":"api","container_id":"abc123"}]'
    )
    begin_bootstrap_cleanup_event(dep)
    publish_bootstrap_failed_after_cleanup(
        dep,
        plan=plan,
        deployment_id=plan.deployment_id,
        messages=(audit,),
        detail="health check failed",
    )
    events = sorted((dep / "events").glob("*.json"))
    statuses = [p.name.split("_", 1)[1].removesuffix(".json") for p in events]
    assert statuses[-3:] == [
        STATUS_BOOTSTRAP_CLEANUP_STARTED,
        STATUS_BOOTSTRAP_CLEANED,
        STATUS_FAILED,
    ]
    assert parse_result(read_json(dep / "result.json")).status == STATUS_FAILED


def test_bootstrap_partial_activation_uses_same_cleanup_contract(
    tmp_path: Path,
) -> None:
    from fetchnow_release.journal_io import read_json

    dep = tmp_path / "dep"
    dep.mkdir()
    plan = _seed_post_mutation_journal(dep, STATUS_ACTIVATING_APP)
    audit = (
        BOOTSTRAP_CLEANUP_AUDIT_PREFIX
        + '[{"service":"api","container_id":"abc123"}]'
    )
    begin_bootstrap_cleanup_event(dep)
    publish_bootstrap_failed_after_cleanup(
        dep,
        plan=plan,
        deployment_id=plan.deployment_id,
        messages=(audit,),
        detail="partial activation",
    )
    events = sorted((dep / "events").glob("*.json"))
    statuses = [p.name.split("_", 1)[1].removesuffix(".json") for p in events]
    assert STATUS_ACTIVATING_APP in statuses
    assert STATUS_APP_HEALTHY not in statuses
    assert statuses[-3:] == [
        STATUS_BOOTSTRAP_CLEANUP_STARTED,
        STATUS_BOOTSTRAP_CLEANED,
        STATUS_FAILED,
    ]
    assert parse_result(read_json(dep / "result.json")).status == STATUS_FAILED


def test_forged_bootstrap_failed_result_rejected_at_write(tmp_path: Path) -> None:
    dep = tmp_path / "dep"
    dep.mkdir()
    plan = _seed_post_mutation_journal(dep, STATUS_STABILIZING)
    begin_bootstrap_cleanup_event(dep)
    append_bootstrap_failure_event(dep, STATUS_BOOTSTRAP_CLEANED, "forged")
    append_bootstrap_failure_event(dep, STATUS_FAILED, "forged")
    with pytest.raises(JournalError, match="cleanup audit"):
        write_result(
            dep,
            deployment_id=plan.deployment_id,
            status=STATUS_FAILED,
            messages=("FAIL: target rollout error",),
        )
    assert not (dep / "result.json").exists()


def test_bootstrap_cleanup_failure_leaves_unresolved_journal(
    tmp_path: Path,
) -> None:
    deploy = tmp_path / "deploy"
    dep_id = "12345678-1234-1234-1234-123456789abc"
    dep = deployment_dir(deploy, dep_id)
    _seed_post_mutation_journal(dep, STATUS_STABILIZING)
    begin_bootstrap_cleanup_event(dep)
    assert not result_path(dep).exists()
    assert find_unresolved_deployments(deploy) == [dep_id]


def test_bootstrap_cleanup_rejects_container_id_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    from fetchnow_release.bootstrap_cleanup import (
        BootstrapCleanupError,
        RemovedBootstrapContainer,
        remove_owned_bootstrap_containers,
    )

    owned = [
        RemovedBootstrapContainer(
            container_id="abc123",
            service="api",
            labels={"com.fetchnow.deployment-id": "d" * 36},
        )
    ]
    monkeypatch.setattr(
        "fetchnow_release.bootstrap_cleanup.discover_owned_bootstrap_containers",
        lambda **_kwargs: owned,
    )

    with pytest.raises(BootstrapCleanupError, match="drift"):
        remove_owned_bootstrap_containers(
            project_name="fetchnow-rollout-test-abc12345",
            env_file=Path("/tmp/env"),
            compose_files=(Path("/tmp/c.yaml"),),
            cwd=Path("/tmp"),
            deployment_id="12345678-1234-1234-1234-123456789abc",
            release_revision="a" * 40,
            expected_container_ids=["def456"],
        )


def test_rollout_reads_legacy_v1_manifest_without_policy_field() -> None:
    rev = "a" * 40
    raw = {
        "schema_version": 1,
        "revision": rev,
        "tree_oid": "b" * 40,
        "created_at_utc": "2026-01-01T00:00:00Z",
        "archive_commit_verified": True,
        "source_repository": "FetchNow",
        "compose_version": "2",
        "docker_version": "24",
        "build_platform": "linux/amd64",
        "contract_hashes": {"compose.yaml": "c" * 64},
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
    from fetchnow_release.manifest import parse_manifest

    manifest = parse_manifest(raw)
    assert manifest.source_contract_version == 1


def test_incompatible_target_rejected_before_activation_and_state_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compatibility rejection must happen before plan write / activate / current write."""
    from fetchnow_release.application_compatibility import CompatibilityError
    from fetchnow_release.current_state import (
        ApplicationState,
        DatabaseState,
        ResolvedCurrentState,
    )
    from contextlib import nullcontext

    from fetchnow_release.rollout import RolloutInput, rollout_release
    from fetchnow_release.verify_release import VerifyResult

    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / "locks").mkdir()
    (deploy / "state").mkdir()
    (deploy / "deployments").mkdir()
    (deploy / "releases").mkdir()
    rev_current = "a" * 40
    rev_target = "c" * 40
    target_release = deploy / "releases" / rev_target
    current_release = deploy / "releases" / rev_current
    (target_release / "source").mkdir(parents=True)
    (current_release / "source").mkdir(parents=True)
    for release in (target_release, current_release):
        (release / "source" / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
        (release / "source" / "compose.staging.yaml").write_text(
            "services: {}\n", encoding="utf-8"
        )

    current = ResolvedCurrentState(
        schema_version=2,
        application=ApplicationState(
            revision=rev_current,
            release_manifest_sha256="d" * 64,
            image_ids=_ids(),
            deployment_id="12345678-1234-1234-1234-123456789abc",
        ),
        database=DatabaseState(
            heads=("0001_baseline",),
            schema_release_revision=rev_current,
            last_migration_id=None,
            compatibility_contract_sha256=None,
            compatible_application_revisions=(rev_current,),
        ),
        updated_at_utc="2026-01-01T00:00:00Z",
        legacy_v1=False,
    )

    calls: list[str] = []

    monkeypatch.setattr(
        "fetchnow_release.rollout.validate_deploy_root",
        lambda path, repo_root=None: path,
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.ensure_rollout_layout",
        lambda _deploy: None,
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.verify_prepared_release",
        lambda *_a, **_k: VerifyResult(ok=True, messages=("OK",)),
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.load_and_resolve_current_state",
        lambda *_a, **_k: current,
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.find_unresolved_deployments",
        lambda *_a, **_k: [],
    )

    def _release_dir(_deploy: Path, rev: str) -> Path:
        return deploy / "releases" / rev

    monkeypatch.setattr("fetchnow_release.rollout.release_dir", _release_dir)
    monkeypatch.setattr(
        "fetchnow_release.rollout.load_manifest",
        lambda _path: object(),
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.assert_release_images_present",
        lambda _manifest: _ids(),
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.sha256_file",
        lambda _path: "e" * 64,
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.manifest_path",
        lambda release: release / "release.json",
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.target_heads_from_release_source",
        lambda _release: frozenset({"0002_incompatible_rollout"}),
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.database_heads_via_postgres",
        lambda **_k: frozenset({"0001_baseline"}),
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.assert_live_matches_saved_database_heads",
        lambda **_k: None,
    )

    def _reject(**_kwargs: object) -> None:
        raise CompatibilityError(
            "application revision is not compatible with current database state"
        )

    monkeypatch.setattr(
        "fetchnow_release.rollout.assert_application_compatible_with_database",
        _reject,
    )

    def _track_plan(*_a: object, **_k: object) -> None:
        calls.append("write_plan")

    def _track_activate(*_a: object, **_k: object) -> None:
        calls.append("activate")

    def _track_write_state(*_a: object, **_k: object) -> None:
        calls.append("write_current_state")

    monkeypatch.setattr("fetchnow_release.rollout.write_plan", _track_plan)
    monkeypatch.setattr("fetchnow_release.rollout.activate_app_tier", _track_activate)
    monkeypatch.setattr("fetchnow_release.rollout.write_current_state", _track_write_state)
    monkeypatch.setattr(
        "fetchnow_release.rollout.RolloutLock",
        lambda *a, **k: nullcontext(),
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout_project.assert_rollout_project",
        lambda _name: None,
    )

    result = rollout_release(
        RolloutInput(
            project_name="fetchnow-rollout-test-abcd1234",
            env_file=tmp_path / "env",
            expected_revision=rev_target,
            repo_root=tmp_path,
            deploy_root=deploy,
        )
    )
    assert result.ok is False
    assert any(
        "not compatible" in msg.lower() or "compatible" in msg.lower()
        for msg in result.messages
    )
    assert calls == []


def test_automatic_rollback_checks_compatibility_before_activate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auto-rollback must gate live/saved + compatibility before activate_app_tier."""
    from fetchnow_release.application_compatibility import CompatibilityError
    from fetchnow_release.current_state import (
        ApplicationState,
        DatabaseState,
        ResolvedCurrentState,
    )
    from fetchnow_release.journal import append_event, deployment_dir, write_plan, PlanDocument
    from fetchnow_release.rollout import RolloutInput, _perform_rollback
    from fetchnow_release.verify_release import VerifyResult

    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / "deployments").mkdir()
    (deploy / "state").mkdir()
    (deploy / "releases").mkdir()
    prev = "a" * 40
    target = "b" * 40
    prev_release = deploy / "releases" / prev
    (prev_release / "source").mkdir(parents=True)
    (prev_release / "source" / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (prev_release / "source" / "compose.staging.yaml").write_text(
        "services: {}\n", encoding="utf-8"
    )

    dep_id = "12345678-1234-1234-1234-123456789abc"
    dep_dir = deployment_dir(deploy, dep_id)
    write_plan(
        dep_dir,
        PlanDocument(
            schema_version=1,
            deployment_id=dep_id,
            target_revision=target,
            previous_revision=prev,
            bootstrap=False,
            target_image_ids=_ids(),
            previous_image_ids=_ids(),
            release_manifest_sha256="d" * 64,
            database_heads=("0001_baseline",),
            created_at_utc="2026-01-01T00:00:00Z",
            project_name="fetchnow-rollout-test-abcd1234",
        ),
    )
    append_event(dep_dir, "planned")
    append_event(dep_dir, "activating_app")

    current = ResolvedCurrentState(
        schema_version=2,
        application=ApplicationState(
            revision=prev,
            release_manifest_sha256="d" * 64,
            image_ids=_ids(),
            deployment_id=dep_id,
        ),
        database=DatabaseState(
            heads=("0001_baseline",),
            schema_release_revision=prev,
            last_migration_id=None,
            compatibility_contract_sha256=None,
            compatible_application_revisions=(prev,),
        ),
        updated_at_utc="2026-01-01T00:00:00Z",
        legacy_v1=False,
    )

    calls: list[str] = []
    monkeypatch.setattr(
        "fetchnow_release.rollout.verify_prepared_release",
        lambda *_a, **_k: VerifyResult(ok=True, messages=("OK",)),
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.load_manifest",
        lambda _p: object(),
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.assert_release_images_present",
        lambda _m: _ids(),
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.load_and_resolve_current_state",
        lambda *_a, **_k: current,
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.database_heads_via_postgres",
        lambda **_k: frozenset({"0001_baseline"}),
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.assert_live_matches_saved_database_heads",
        lambda **_k: None,
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.target_heads_from_release_source",
        lambda _r: frozenset({"0009_other"}),
    )

    def _reject(**_k: object) -> None:
        calls.append("compat")
        raise CompatibilityError("previous application is not compatible")

    def _activate(**_k: object) -> None:
        calls.append("activate")

    monkeypatch.setattr(
        "fetchnow_release.rollout.assert_application_compatible_with_database",
        _reject,
    )
    monkeypatch.setattr("fetchnow_release.rollout.activate_app_tier", _activate)
    monkeypatch.setattr(
        "fetchnow_release.rollout.write_images_override",
        lambda *_a, **_k: prev_release / "override.yaml",
    )

    result = _perform_rollback(
        inp=RolloutInput(
            project_name="fetchnow-rollout-test-abcd1234",
            env_file=tmp_path / "env",
            expected_revision=target,
            repo_root=tmp_path,
            deploy_root=deploy,
        ),
        dep_dir=dep_dir,
        deployment_id=dep_id,
        previous_revision=prev,
        previous_ids=_ids(),
        messages=[],
        mutation_began=True,
    )
    assert result.ok is False
    assert result.status == "rollback_failed"
    assert calls == ["compat"]


def _write_release_compose(release: Path, *, storage_init: bool) -> None:
    source = release / "source"
    source.mkdir(parents=True, exist_ok=True)
    if storage_init:
        compose = "services:\n  storage-init:\n    image: fetchnow-api:x\n"
    else:
        compose = "services:\n  api:\n    image: fetchnow-api:x\n"
    (source / "compose.yaml").write_text(compose, encoding="utf-8")
    (source / "compose.staging.yaml").write_text("services: {}\n", encoding="utf-8")


def _patch_bootstrap_rollout(
    monkeypatch: pytest.MonkeyPatch, deploy: Path
) -> list[str]:
    from contextlib import nullcontext

    from fetchnow_release.verify_release import VerifyResult

    calls: list[str] = []
    monkeypatch.setattr(
        "fetchnow_release.rollout.validate_deploy_root",
        lambda path, repo_root=None: path,
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.ensure_rollout_layout",
        lambda _deploy: None,
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.verify_prepared_release",
        lambda *_a, **_k: VerifyResult(ok=True, messages=("OK",)),
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.load_and_resolve_current_state",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.find_unresolved_deployments",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "fetchnow_release.migration_journal.find_unresolved_migrations",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.release_dir",
        lambda _deploy, rev: deploy / "releases" / rev,
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.load_manifest",
        lambda _path: object(),
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.assert_release_images_present",
        lambda _manifest: _ids(),
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.sha256_file",
        lambda _path: "e" * 64,
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.manifest_path",
        lambda release: release / "release.json",
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.target_heads_from_release_source",
        lambda _release: frozenset({"0001_baseline"}),
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.database_heads_via_postgres",
        lambda **_k: frozenset({"0001_baseline"}),
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout._app_containers_present",
        lambda **_k: False,
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout._postgres_healthy",
        lambda **_k: True,
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.RolloutLock",
        lambda *a, **k: nullcontext(),
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout_project.assert_rollout_project",
        lambda _name: None,
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.wait_services_healthy",
        lambda **_k: None,
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.stabilize_full_health",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.write_current_state",
        lambda *_a, **_k: None,
    )

    def _init(**_k: object) -> None:
        calls.append("storage-init")

    def _activate(**_k: object) -> None:
        calls.append("activate")

    def _gateway(**_k: object) -> None:
        calls.append("gateway")

    monkeypatch.setattr("fetchnow_release.rollout.run_storage_init", _init)
    monkeypatch.setattr("fetchnow_release.rollout.activate_app_tier", _activate)
    monkeypatch.setattr("fetchnow_release.rollout.activate_gateway", _gateway)
    return calls


def test_storage_init_runs_before_app_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fetchnow_release.rollout import RolloutInput, rollout_release

    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / "locks").mkdir()
    (deploy / "state").mkdir()
    (deploy / "deployments").mkdir()
    rev = "c" * 40
    target = deploy / "releases" / rev
    _write_release_compose(target, storage_init=True)
    calls = _patch_bootstrap_rollout(monkeypatch, deploy)
    result = rollout_release(
        RolloutInput(
            project_name="fetchnow-rollout-test-abcd1234",
            env_file=tmp_path / "env",
            expected_revision=rev,
            repo_root=tmp_path,
            deploy_root=deploy,
            bootstrap=True,
        )
    )
    assert result.ok is True
    assert calls[:2] == ["storage-init", "activate"]
    assert calls.count("activate") == 1
    assert calls.count("gateway") == 1
    overrides = list((deploy / "deployments").glob("*/overrides/images.yaml"))
    assert len(overrides) == 1
    text = overrides[0].read_text(encoding="utf-8")
    assert "storage-init:" in text
    assert f"image: sha256:{'a' * 64}" in text
    events = list((deploy / "deployments").glob("*/events/*.json"))
    names = sorted(p.name for p in events)
    assert any("initializing_storage" in name for name in names)
    assert any("activating_app" in name for name in names)


def test_storage_init_failure_prevents_app_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fetchnow_release.activate import ActivateError
    from fetchnow_release.rollout import RolloutInput, rollout_release

    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / "locks").mkdir()
    (deploy / "state").mkdir()
    (deploy / "deployments").mkdir()
    rev = "c" * 40
    _write_release_compose(deploy / "releases" / rev, storage_init=True)
    calls = _patch_bootstrap_rollout(monkeypatch, deploy)

    def _fail(**_k: object) -> None:
        calls.append("storage-init")
        raise ActivateError("storage-init failed: exit 1")

    monkeypatch.setattr("fetchnow_release.rollout.run_storage_init", _fail)
    result = rollout_release(
        RolloutInput(
            project_name="fetchnow-rollout-test-abcd1234",
            env_file=tmp_path / "env",
            expected_revision=rev,
            repo_root=tmp_path,
            deploy_root=deploy,
            bootstrap=True,
        )
    )
    assert result.ok is False
    assert result.status == STATUS_FAILED
    assert calls == ["storage-init"]
    events = list((deploy / "deployments").glob("*/events/*.json"))
    names = sorted(p.name for p in events)
    assert any("initializing_storage" in name for name in names)
    assert any("_failed.json" in name for name in names)
    assert not any("activating_app" in name for name in names)


def test_pr8_rollback_skips_storage_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fetchnow_release.application_compatibility import CompatibilityDecision
    from fetchnow_release.current_state import (
        ApplicationState,
        DatabaseState,
        ResolvedCurrentState,
    )
    from fetchnow_release.journal import (
        PlanDocument,
        append_event,
        deployment_dir,
        write_plan,
    )
    from fetchnow_release.rollout import RolloutInput, _perform_rollback
    from fetchnow_release.verify_release import VerifyResult

    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / "deployments").mkdir()
    (deploy / "state").mkdir()
    prev = "a" * 40
    target = "b" * 40
    prev_release = deploy / "releases" / prev
    _write_release_compose(prev_release, storage_init=False)
    dep_id = "12345678-1234-1234-1234-123456789abc"
    dep_dir = deployment_dir(deploy, dep_id)
    write_plan(
        dep_dir,
        PlanDocument(
            schema_version=1,
            deployment_id=dep_id,
            target_revision=target,
            previous_revision=prev,
            bootstrap=False,
            target_image_ids=_ids(),
            previous_image_ids=_ids(),
            release_manifest_sha256="d" * 64,
            database_heads=("0001_baseline",),
            created_at_utc="2026-01-01T00:00:00Z",
            project_name="fetchnow-rollout-test-abcd1234",
        ),
    )
    append_event(dep_dir, STATUS_PLANNED)
    append_event(dep_dir, STATUS_ACTIVATING_APP)
    current = ResolvedCurrentState(
        schema_version=2,
        application=ApplicationState(
            revision=prev,
            release_manifest_sha256="d" * 64,
            image_ids=_ids(),
            deployment_id=dep_id,
        ),
        database=DatabaseState(
            heads=("0001_baseline",),
            schema_release_revision=prev,
            last_migration_id=None,
            compatibility_contract_sha256=None,
            compatible_application_revisions=(prev,),
        ),
        updated_at_utc="2026-01-01T00:00:00Z",
        legacy_v1=False,
    )
    calls: list[str] = []
    monkeypatch.setattr(
        "fetchnow_release.rollout.verify_prepared_release",
        lambda *_a, **_k: VerifyResult(ok=True, messages=("OK",)),
    )
    monkeypatch.setattr("fetchnow_release.rollout.load_manifest", lambda _p: object())
    monkeypatch.setattr(
        "fetchnow_release.rollout.assert_release_images_present",
        lambda _m: _ids(),
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.load_and_resolve_current_state",
        lambda *_a, **_k: current,
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.database_heads_via_postgres",
        lambda **_k: frozenset({"0001_baseline"}),
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.assert_live_matches_saved_database_heads",
        lambda **_k: None,
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.target_heads_from_release_source",
        lambda _r: frozenset({"0001_baseline"}),
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.assert_application_compatible_with_database",
        lambda **_k: CompatibilityDecision(
            allowed=True,
            reason="equal heads",
            via_equal_heads=True,
            via_recorded_envelope=False,
        ),
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.wait_services_healthy",
        lambda **_k: None,
    )
    monkeypatch.setattr(
        "fetchnow_release.rollout.stabilize_full_health",
        lambda *_a, **_k: None,
    )

    def _init(**_k: object) -> None:
        calls.append("storage-init")

    def _activate(**_k: object) -> None:
        calls.append("activate")

    def _gateway(**_k: object) -> None:
        calls.append("gateway")

    monkeypatch.setattr("fetchnow_release.rollout.run_storage_init", _init)
    monkeypatch.setattr("fetchnow_release.rollout.activate_app_tier", _activate)
    monkeypatch.setattr("fetchnow_release.rollout.activate_gateway", _gateway)
    result = _perform_rollback(
        inp=RolloutInput(
            project_name="fetchnow-rollout-test-abcd1234",
            env_file=tmp_path / "env",
            expected_revision=target,
            repo_root=tmp_path,
            deploy_root=deploy,
        ),
        dep_dir=dep_dir,
        deployment_id=dep_id,
        previous_revision=prev,
        previous_ids=_ids(),
        messages=[],
        mutation_began=True,
    )
    assert result.ok is False
    assert result.status == STATUS_ROLLED_BACK
    assert calls == ["activate", "gateway"]
    override = dep_dir / "overrides" / "images.yaml"
    text = override.read_text(encoding="utf-8")
    assert "storage-init:" not in text
