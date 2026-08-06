"""Unit tests for PRD1C3A rollout journal, overrides, and safety contracts."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fetchnow_release.activate import ActivateError, build_activate_argv  # noqa: E402
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
    CurrentState,
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
    write_current_state,
    write_plan,
    write_recovery_plan,
    write_recovery_result,
    write_resolution,
    write_result,
)
from fetchnow_release.override import (  # noqa: E402
    OverrideError,
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
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    state = CurrentState(
        schema_version=1,
        revision="a" * 40,
        release_manifest_sha256="d" * 64,
        image_ids=_ids(),
        updated_at_utc="2026-01-01T00:00:00Z",
        deployment_id="12345678-1234-1234-1234-123456789abc",
    )
    write_current_state(deploy, state)
    loaded = load_current_state(deploy)
    assert loaded is not None
    assert loaded.revision == "a" * 40
    with pytest.raises(JournalError):
        write_current_state(
            deploy,
            CurrentState(
                schema_version=1,
                revision="a" * 40,
                release_manifest_sha256="d" * 64,
                image_ids={"api": "bad"},
                updated_at_utc="2026-01-01T00:00:00Z",
                deployment_id="12345678-1234-1234-1234-123456789abc",
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
