"""Unit tests for initial database bootstrap (plan + transaction)."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "tests" / "release"))

from fetchnow_release.bootstrap_db import (  # noqa: E402
    BootstrapDbInput,
    run_bootstrap_db,
)
from fetchnow_release.bootstrap_freshness import (  # noqa: E402
    CLASS_AMBIGUOUS,
    CLASS_APP_PRESENT,
    CLASS_CLEAN,
    CLASS_DIRTY,
    CLASS_INITIALIZED,
    CLASS_UNINITIALIZED,
    BootstrapFreshness,
    UserCatalogProbe,
    USER_CATALOG_SQL,
    _user_catalog_select_argv,
    classify_healthy_postgres_schema,
    unexpected_catalog_objects,
)
from fetchnow_release.bootstrap_journal import (  # noqa: E402
    BootstrapIdentity,
    BootstrapJournalError,
    BootstrapPlanDocument,
    BootstrapResultDocument,
    append_bootstrap_event,
    ensure_bootstrap_layout,
    find_matching_committed_bootstrap,
    find_unresolved_bootstraps,
    prepare_bootstrap_dir,
    utc_now,
    write_bootstrap_plan,
    write_bootstrap_result,
)
from fetchnow_release.bootstrap_project import (  # noqa: E402
    BootstrapProjectError,
    assert_bootstrap_project,
    load_bootstrap_project_file,
    make_bootstrap_project_name,
)
from fetchnow_release.c3b3_constants import (  # noqa: E402
    BOOTSTRAP_ALLOWED_USER_RELATIONS,
    BOOTSTRAP_PLAN_NAME,
    STATUS_BOOTSTRAP_COMMITTED,
    STATUS_BOOTSTRAP_FAILED,
    STATUS_BOOTSTRAP_FRESHNESS_VERIFIED,
    STATUS_BOOTSTRAP_PLANNED,
)
from fetchnow_release.cli import build_parser  # noqa: E402
from fetchnow_release.current_state import current_path  # noqa: E402
from fetchnow_release.db_heads import DatabaseHeadsProbe  # noqa: E402
from fetchnow_release.deploy_plan import DeployPlanInput, run_deploy_plan  # noqa: E402
from fetchnow_release.postgres_bootstrap import (  # noqa: E402
    PostgresBootstrapError,
    build_postgres_up_argv,
)
from fetchnow_release.readonly_guard import assert_readonly_subprocess  # noqa: E402
from fetchnow_release.verify_release import VerifyResult  # noqa: E402
from password_fixture import valid_test_password  # noqa: E402

REV = "a" * 40
IMAGE = f"sha256:{'b' * 64}"
CONTRACT = "c" * 64
HEAD = "0006_browser_delivery_grants"
BOOT_ID = "12345678-1234-1234-1234-123456789abc"


def _identity(**overrides: object) -> BootstrapIdentity:
    payload: dict[str, object] = {
        "project_name": "fetchnow-bootstrap-test-abcd1234",
        "target_revision": REV,
        "target_db_heads": (HEAD,),
        "release_manifest_sha256": "d" * 64,
        "target_api_image_id": IMAGE,
        "compose_overlay": "compose.staging.yaml",
        "source_contract_version": 3,
    }
    payload.update(overrides)
    return BootstrapIdentity(**payload)  # type: ignore[arg-type]


def _plan_doc(**overrides: object) -> BootstrapPlanDocument:
    payload: dict[str, object] = {
        "schema_version": 1,
        "bootstrap_id": BOOT_ID,
        "project_name": "fetchnow-bootstrap-test-abcd1234",
        "target_revision": REV,
        "target_db_heads": [HEAD],
        "release_manifest_sha256": "d" * 64,
        "target_api_image_id": IMAGE,
        "compose_overlay": "compose.staging.yaml",
        "source_contract_version": 3,
        "pgdata_volume_existed_before": False,
        "created_at_utc": "2026-01-01T00:00:00Z",
    }
    payload.update(overrides)
    return BootstrapPlanDocument(**payload)  # type: ignore[arg-type]


def _result_doc(**overrides: object) -> BootstrapResultDocument:
    payload: dict[str, object] = {
        "schema_version": 1,
        "bootstrap_id": BOOT_ID,
        "status": STATUS_BOOTSTRAP_COMMITTED,
        "already_initialized": False,
        "target_revision": REV,
        "verified_db_heads": [HEAD],
        "current_json_written": False,
        "messages": ["OK"],
        "finished_at_utc": "2026-01-01T00:00:00Z",
    }
    payload.update(overrides)
    return BootstrapResultDocument(**payload)  # type: ignore[arg-type]


def _chmod_600(path: Path) -> None:
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _write_env(path: Path, project: str, deploy: Path, backup: Path) -> None:
    path.write_text(
        "\n".join(
            [
                f"COMPOSE_PROJECT_NAME={project}",
                f"FETCHNOW_RELEASE_REVISION={REV}",
                "GATEWAY_PORT=127.0.0.1:8091",
                "PUBLIC_SITE_URL=http://127.0.0.1",
                "POSTGRES_DB=fetchnow",
                "POSTGRES_USER=fetchnow",
                f"POSTGRES_PASSWORD={valid_test_password()}",
                f"FETCHNOW_DEPLOY_ROOT={deploy}",
                f"FETCHNOW_BACKUP_ROOT={backup}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _chmod_600(path)


def _freshness(
    classification: str,
    *,
    reason: str = "test",
    app: tuple[str, ...] = (),
    postgres_healthy: bool = False,
    pgdata: bool = False,
    volumes: tuple[str, ...] = (),
    heads: frozenset[str] | None = None,
    user_relations: tuple[str, ...] = (),
) -> BootstrapFreshness:
    probe = None
    if classification == CLASS_INITIALIZED:
        probe = DatabaseHeadsProbe(status="heads", heads=heads or frozenset({HEAD}))
    elif classification == CLASS_UNINITIALIZED:
        probe = DatabaseHeadsProbe(status="missing_table", heads=frozenset())
    elif classification == CLASS_DIRTY:
        probe = DatabaseHeadsProbe(status="missing_table", heads=frozenset())
    return BootstrapFreshness(
        classification=classification,
        reason=reason,
        application_services=app,
        postgres_state="running" if postgres_healthy else None,
        postgres_health="healthy" if postgres_healthy else None,
        postgres_healthy=postgres_healthy,
        project_volumes=volumes,
        pgdata_volume_present=pgdata,
        alembic_probe=probe,
        user_relations=user_relations,
    )


@dataclass
class _Manifest:
    source_contract_version: int = 3
    compose_overlay: str = "compose.staging.yaml"


def _common_release_mocks(
    monkeypatch: pytest.MonkeyPatch,
    deploy: Path,
    *,
    freshness: BootstrapFreshness,
) -> list[str]:
    calls: list[str] = []
    source = deploy / "releases" / REV / "source"
    source.mkdir(parents=True, exist_ok=True)
    (source / "compose.yaml").write_text("services:\n  postgres:\n    image: x\n")
    (source / "compose.staging.yaml").write_text("services: {}\n")

    monkeypatch.setattr(
        "fetchnow_release.bootstrap_db.validate_deploy_root",
        lambda path, repo_root=None: path,
    )
    monkeypatch.setattr(
        "fetchnow_release.bootstrap_db.verify_prepared_release",
        lambda *_a, **_k: VerifyResult(ok=True, messages=("OK",)),
    )
    monkeypatch.setattr(
        "fetchnow_release.bootstrap_db.load_manifest",
        lambda _path: _Manifest(),
    )
    monkeypatch.setattr(
        "fetchnow_release.bootstrap_db.assert_release_images_present",
        lambda _m: {"api": IMAGE, "worker": IMAGE, "web": IMAGE, "gateway": IMAGE},
    )
    monkeypatch.setattr(
        "fetchnow_release.bootstrap_db.sha256_file",
        lambda _path: "d" * 64,
    )
    monkeypatch.setattr(
        "fetchnow_release.bootstrap_db.snapshot_compose_files_for_release",
        lambda *_a, **_k: (
            source / "compose.yaml",
            source / "compose.staging.yaml",
        ),
    )
    monkeypatch.setattr(
        "fetchnow_release.bootstrap_db.target_heads_from_release_source",
        lambda _release: frozenset({HEAD}),
    )
    monkeypatch.setattr(
        "fetchnow_release.bootstrap_db.inspect_bootstrap_freshness",
        lambda **_k: freshness,
    )
    monkeypatch.setattr(
        "fetchnow_release.bootstrap_db.RolloutLock",
        lambda *a, **k: nullcontext(),
    )
    monkeypatch.setattr(
        "fetchnow_release.bootstrap_db.find_unresolved_deployments",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "fetchnow_release.bootstrap_db.find_unresolved_migrations",
        lambda *_a, **_k: [],
    )

    def _start(**_k: object) -> None:
        calls.append("postgres")

    monkeypatch.setattr("fetchnow_release.bootstrap_db.start_postgres_only", _start)
    monkeypatch.setattr(
        "fetchnow_release.bootstrap_db.wait_services_healthy",
        lambda **_k: calls.append("wait-postgres"),
    )

    class _Alembic:
        exit_code = 0
        stderr_summary = ""
        stdout_summary = "ok"

    monkeypatch.setattr(
        "fetchnow_release.bootstrap_db.run_alembic_upgrade",
        lambda **_k: calls.append("alembic") or _Alembic(),
    )
    monkeypatch.setattr(
        "fetchnow_release.bootstrap_db.probe_database_heads",
        lambda **_k: DatabaseHeadsProbe(status="heads", heads=frozenset({HEAD})),
    )
    monkeypatch.setattr(
        "fetchnow_release.bootstrap_db.write_images_override",
        lambda directory, *_a, **_k: directory / "images.yaml",
    )
    monkeypatch.setattr(
        "fetchnow_release.bootstrap_db.compose_files_include_delivery",
        lambda _files: False,
    )
    monkeypatch.setattr(
        "fetchnow_release.bootstrap_db.compose_files_include_storage_init",
        lambda _files: False,
    )
    return calls


def _bootstrap_input(tmp_path: Path, project: str) -> tuple[BootstrapDbInput, Path]:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / "locks").mkdir()
    backup = tmp_path / "backups"
    backup.mkdir()
    env = tmp_path / ".env"
    _write_env(env, project, deploy, backup)
    compose = tmp_path / "compose.yaml"
    overlay = tmp_path / "compose.staging.yaml"
    compose.write_text("services: {}\n")
    overlay.write_text("services: {}\n")
    return (
        BootstrapDbInput(
            project_name=project,
            env_file=env,
            compose_files=(compose, overlay),
            expected_revision=REV,
            repo_root=tmp_path,
            deploy_root=deploy,
        ),
        deploy,
    )


def test_bootstrap_project_rejects_real_and_forbidden_names(tmp_path: Path) -> None:
    name = make_bootstrap_project_name()
    assert name.startswith("fetchnow-bootstrap-test-")
    assert_bootstrap_project(name)
    for bad in (
        "fetchnow-staging",
        "fetchnow-production",
        "fetchnow-prod",
        "fetchnow",
        "fetchnow-bootstrap-test",
        "fetchnow-backup-test-abcd1234",
    ):
        with pytest.raises(BootstrapProjectError):
            assert_bootstrap_project(bad)
    path = tmp_path / "project.txt"
    path.write_text("fetchnow-production\n")
    with pytest.raises(BootstrapProjectError):
        load_bootstrap_project_file(path)


def test_postgres_up_argv_targets_postgres_only(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("x=1\n")
    compose = tmp_path / "compose.yaml"
    overlay = tmp_path / "compose.staging.yaml"
    compose.write_text("services: {}\n")
    overlay.write_text("services: {}\n")
    argv = build_postgres_up_argv(
        project_name="fetchnow-bootstrap-test-abcd1234",
        env_file=env,
        compose_files=(compose, overlay),
    )
    assert argv[-1] == "postgres"
    assert "--no-deps" in argv and "--pull" in argv and "never" in argv
    joined = " ".join(argv)
    assert "api" not in argv[argv.index("up") :]
    assert "worker" not in argv[argv.index("up") :]
    assert "-v" not in argv
    assert "password" not in joined.lower()


def test_cli_has_bootstrap_db_without_bypass_flags() -> None:
    help_text = build_parser().format_help()
    assert "bootstrap-db" in help_text
    for banned in (
        "skip-health",
        "allow-dirty",
        "skip-preflight",
        "test-mode",
        "allow-unsafe",
        "force-migration",
    ):
        assert banned not in help_text


def test_bootstrap_refuses_application_containers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inp, deploy = _bootstrap_input(tmp_path, "fetchnow-bootstrap-test-abcd1234")
    _common_release_mocks(
        monkeypatch,
        deploy,
        freshness=_freshness(
            CLASS_APP_PRESENT,
            app=("api",),
            reason="application containers already exist: api",
        ),
    )
    result = run_bootstrap_db(inp)
    assert result.ok is False
    assert any("application containers" in msg for msg in result.messages)
    assert not current_path(deploy).exists()


def test_bootstrap_refuses_unresolved_journals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inp, deploy = _bootstrap_input(tmp_path, "fetchnow-bootstrap-test-abcd1234")
    _common_release_mocks(
        monkeypatch,
        deploy,
        freshness=_freshness(CLASS_CLEAN),
    )
    monkeypatch.setattr(
        "fetchnow_release.bootstrap_db.find_unresolved_migrations",
        lambda *_a, **_k: ["11111111-1111-1111-1111-111111111111"],
    )
    result = run_bootstrap_db(inp)
    assert result.ok is False
    assert any("unresolved migration" in msg for msg in result.messages)


def test_bootstrap_refuses_ambiguous_preexisting_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inp, deploy = _bootstrap_input(tmp_path, "fetchnow-bootstrap-test-abcd1234")
    _common_release_mocks(
        monkeypatch,
        deploy,
        freshness=_freshness(
            CLASS_AMBIGUOUS,
            reason="postgres container or project volume exists but postgres is not healthy",
            pgdata=True,
            volumes=("fetchnow-bootstrap-test-abcd1234_pgdata",),
        ),
    )
    result = run_bootstrap_db(inp)
    assert result.ok is False
    assert any("not healthy" in msg or "refused" in msg.lower() for msg in result.messages)
    assert not current_path(deploy).exists()


def test_bootstrap_clean_path_starts_postgres_then_alembic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inp, deploy = _bootstrap_input(tmp_path, "fetchnow-bootstrap-test-abcd1234")
    calls = _common_release_mocks(
        monkeypatch,
        deploy,
        freshness=_freshness(CLASS_CLEAN),
    )
    after_start = _freshness(
        CLASS_UNINITIALIZED, postgres_healthy=True, reason="alembic_version absent"
    )
    states = [_freshness(CLASS_CLEAN), after_start]

    def _inspect(**_k: object) -> BootstrapFreshness:
        return states.pop(0) if states else after_start

    monkeypatch.setattr(
        "fetchnow_release.bootstrap_db.inspect_bootstrap_freshness", _inspect
    )
    result = run_bootstrap_db(inp)
    assert result.ok is True
    assert result.already_initialized is False
    assert calls == ["postgres", "wait-postgres", "alembic"]
    assert not current_path(deploy).exists()
    assert result.status == STATUS_BOOTSTRAP_COMMITTED
    assert find_unresolved_bootstraps(deploy) == []


def test_bootstrap_failure_before_schema_is_terminal_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inp, deploy = _bootstrap_input(tmp_path, "fetchnow-bootstrap-test-abcd1234")
    _common_release_mocks(monkeypatch, deploy, freshness=_freshness(CLASS_CLEAN))

    def _boom(**_k: object) -> None:
        raise PostgresBootstrapError("compose up postgres failed: secret=hunter2")

    monkeypatch.setattr("fetchnow_release.bootstrap_db.start_postgres_only", _boom)
    result = run_bootstrap_db(inp)
    assert result.ok is False
    assert result.status == STATUS_BOOTSTRAP_FAILED
    assert all("hunter2" not in msg for msg in result.messages)
    assert find_unresolved_bootstraps(deploy) == []
    assert not current_path(deploy).exists()


def test_bootstrap_alembic_failure_requires_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inp, deploy = _bootstrap_input(tmp_path, "fetchnow-bootstrap-test-abcd1234")
    after_start = _freshness(
        CLASS_UNINITIALIZED, postgres_healthy=True, reason="alembic_version absent"
    )
    _common_release_mocks(monkeypatch, deploy, freshness=_freshness(CLASS_CLEAN))
    states = [_freshness(CLASS_CLEAN), after_start]
    monkeypatch.setattr(
        "fetchnow_release.bootstrap_db.inspect_bootstrap_freshness",
        lambda **_k: states.pop(0) if states else after_start,
    )

    class _Bad:
        exit_code = 1
        stderr_summary = "alembic failed"
        stdout_summary = ""

    monkeypatch.setattr(
        "fetchnow_release.bootstrap_db.run_alembic_upgrade",
        lambda **_k: _Bad(),
    )
    result = run_bootstrap_db(inp)
    assert result.ok is False
    assert result.status == "requires_recovery"
    assert result.bootstrap_id in find_unresolved_bootstraps(deploy)
    assert not current_path(deploy).exists()


def test_bootstrap_idempotent_after_committed_exact_heads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inp, deploy = _bootstrap_input(tmp_path, "fetchnow-bootstrap-test-abcd1234")
    _common_release_mocks(
        monkeypatch,
        deploy,
        freshness=_freshness(
            CLASS_INITIALIZED,
            postgres_healthy=True,
            heads=frozenset({HEAD}),
            reason="already initialized",
        ),
    )
    boot_id = "12345678-1234-1234-1234-123456789abc"
    ensure_bootstrap_layout(deploy)
    boot = prepare_bootstrap_dir(deploy, boot_id)
    write_bootstrap_plan(
        boot / BOOTSTRAP_PLAN_NAME,
        BootstrapPlanDocument(
            schema_version=1,
            bootstrap_id=boot_id,
            project_name=inp.project_name,
            target_revision=REV,
            target_db_heads=[HEAD],
            release_manifest_sha256="d" * 64,
            target_api_image_id=IMAGE,
            compose_overlay="compose.staging.yaml",
            source_contract_version=3,
            pgdata_volume_existed_before=False,
            created_at_utc=utc_now(),
        ),
    )
    append_bootstrap_event(boot, STATUS_BOOTSTRAP_PLANNED, "plan")
    append_bootstrap_event(boot, STATUS_BOOTSTRAP_FRESHNESS_VERIFIED, "fresh")
    append_bootstrap_event(boot, STATUS_BOOTSTRAP_COMMITTED, "done")
    write_bootstrap_result(
        boot,
        BootstrapResultDocument(
            schema_version=1,
            bootstrap_id=boot_id,
            status=STATUS_BOOTSTRAP_COMMITTED,
            already_initialized=False,
            target_revision=REV,
            verified_db_heads=[HEAD],
            current_json_written=False,
            messages=["OK"],
            finished_at_utc=utc_now(),
        ),
    )
    alembic_calls: list[str] = []
    monkeypatch.setattr(
        "fetchnow_release.bootstrap_db.run_alembic_upgrade",
        lambda **_k: alembic_calls.append("alembic") or SimpleNamespace(exit_code=0),
    )
    result = run_bootstrap_db(inp)
    assert result.ok is True
    assert result.already_initialized is True
    assert alembic_calls == []
    assert not current_path(deploy).exists()


def test_bootstrap_initialized_heads_without_matching_journal_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inp, deploy = _bootstrap_input(tmp_path, "fetchnow-bootstrap-test-abcd1234")
    _common_release_mocks(
        monkeypatch,
        deploy,
        freshness=_freshness(
            CLASS_INITIALIZED,
            postgres_healthy=True,
            heads=frozenset({HEAD}),
            reason="already initialized",
        ),
    )
    result = run_bootstrap_db(inp)
    assert result.ok is False
    assert any("no committed bootstrap journal" in msg for msg in result.messages)
    assert not current_path(deploy).exists()


def test_bootstrap_revision_only_journal_is_not_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inp, deploy = _bootstrap_input(tmp_path, "fetchnow-bootstrap-test-abcd1234")
    _common_release_mocks(
        monkeypatch,
        deploy,
        freshness=_freshness(
            CLASS_INITIALIZED,
            postgres_healthy=True,
            heads=frozenset({HEAD}),
        ),
    )
    ensure_bootstrap_layout(deploy)
    boot = prepare_bootstrap_dir(deploy, BOOT_ID)
    write_bootstrap_plan(
        boot / BOOTSTRAP_PLAN_NAME,
        _plan_doc(release_manifest_sha256="e" * 64),
    )
    append_bootstrap_event(boot, STATUS_BOOTSTRAP_PLANNED, "plan")
    append_bootstrap_event(boot, STATUS_BOOTSTRAP_FRESHNESS_VERIFIED, "fresh")
    append_bootstrap_event(boot, STATUS_BOOTSTRAP_COMMITTED, "done")
    write_bootstrap_result(boot, _result_doc())
    result = run_bootstrap_db(inp)
    assert result.ok is False
    assert any("exact prepared-release identity" in msg for msg in result.messages)


def test_bootstrap_dirty_table_without_alembic_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inp, deploy = _bootstrap_input(tmp_path, "fetchnow-bootstrap-test-abcd1234")
    _common_release_mocks(
        monkeypatch,
        deploy,
        freshness=_freshness(
            CLASS_DIRTY,
            postgres_healthy=True,
            reason="alembic_version is absent but unexpected user objects exist: public.leftover:r",
            user_relations=("public.leftover:r",),
        ),
    )
    result = run_bootstrap_db(inp)
    assert result.ok is False
    assert any("unexpected user objects" in msg for msg in result.messages)
    assert not current_path(deploy).exists()


def _patch_deploy_plan(
    monkeypatch: pytest.MonkeyPatch,
    deploy: Path,
    *,
    current: object | None,
    freshness: BootstrapFreshness,
) -> None:
    source = deploy / "releases" / REV / "source"
    source.mkdir(parents=True, exist_ok=True)
    (source / "compose.yaml").write_text("services: {}\n")
    (source / "compose.staging.yaml").write_text("services: {}\n")

    monkeypatch.setattr(
        "fetchnow_release.deploy_plan.validate_deploy_root",
        lambda path, repo_root=None: path,
    )
    monkeypatch.setattr(
        "fetchnow_release.deploy_plan.validate_operator_root",
        lambda path, **_k: path,
    )
    monkeypatch.setattr(
        "fetchnow_release.deploy_plan.verify_prepared_release",
        lambda *_a, **_k: VerifyResult(ok=True, messages=("OK",)),
    )
    monkeypatch.setattr(
        "fetchnow_release.deploy_plan.load_manifest",
        lambda _path: _Manifest(),
    )
    monkeypatch.setattr(
        "fetchnow_release.deploy_plan.assert_deploy_plan_target_contract",
        lambda _v: None,
    )
    monkeypatch.setattr(
        "fetchnow_release.deploy_plan.resolve_runtime_overlay",
        lambda **_k: "compose.staging.yaml",
    )
    monkeypatch.setattr(
        "fetchnow_release.deploy_plan.find_unresolved_migrations",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "fetchnow_release.deploy_plan.load_and_resolve_current_state",
        lambda *_a, **_k: current,
    )
    monkeypatch.setattr(
        "fetchnow_release.deploy_plan.inspect_bootstrap_freshness",
        lambda **_k: freshness,
    )
    monkeypatch.setattr(
        "fetchnow_release.deploy_plan.snapshot_compose_files_for_release",
        lambda *_a, **_k: (
            source / "compose.yaml",
            source / "compose.staging.yaml",
        ),
    )
    monkeypatch.setattr(
        "fetchnow_release.deploy_plan.target_heads_from_release_source",
        lambda _release: frozenset({HEAD}),
    )
    monkeypatch.setattr(
        "fetchnow_release.deploy_plan.load_compatibility_contract",
        lambda _path: object(),
    )
    monkeypatch.setattr(
        "fetchnow_release.deploy_plan.compatibility_contract_sha256",
        lambda _path: CONTRACT,
    )
    monkeypatch.setattr(
        "fetchnow_release.deploy_plan.sha256_file",
        lambda _path: "d" * 64,
    )
    monkeypatch.setattr(
        "fetchnow_release.deploy_plan.assert_release_images_present",
        lambda _m: {"api": IMAGE, "worker": IMAGE, "web": IMAGE, "gateway": IMAGE},
    )
    monkeypatch.setattr(
        "fetchnow_release.deploy_plan.find_unresolved_deployments",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "fetchnow_release.deploy_plan.find_unresolved_bootstraps",
        lambda *_a, **_k: [],
    )


def test_deploy_plan_clean_initial_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    backup = tmp_path / "backups"
    backup.mkdir()
    env = tmp_path / ".env"
    project = "fetchnow-deploy-plan-test-abcd1234"
    _write_env(env, project, deploy, backup)
    compose = tmp_path / "compose.yaml"
    overlay = tmp_path / "compose.staging.yaml"
    compose.write_text("services: {}\n")
    overlay.write_text("services: {}\n")
    _patch_deploy_plan(
        monkeypatch,
        deploy,
        current=None,
        freshness=_freshness(CLASS_CLEAN),
    )
    result = run_deploy_plan(
        DeployPlanInput(
            project_name=project,
            env_file=env,
            compose_files=(compose, overlay),
            expected_revision=REV,
            repo_root=tmp_path,
            deploy_root=deploy,
            backup_root=backup,
        )
    )
    assert result.ok is True
    plan = json.loads(result.plan_json or "{}")
    assert plan["initial_bootstrap"] is True
    assert plan["current_revision"] is None
    assert plan["target_revision"] == REV
    assert plan["current_db_heads"] == []
    assert plan["target_db_heads"] == [HEAD]
    assert plan["initial_schema_required"] is True
    assert plan["migration_required"] is False
    assert plan["verified_backup_required"] is False
    assert plan["application_rollout_required"] is True


def test_deploy_plan_initialized_without_journal_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    backup = tmp_path / "backups"
    backup.mkdir()
    env = tmp_path / ".env"
    project = "fetchnow-deploy-plan-test-abcd1234"
    _write_env(env, project, deploy, backup)
    compose = tmp_path / "compose.yaml"
    overlay = tmp_path / "compose.staging.yaml"
    compose.write_text("services: {}\n")
    overlay.write_text("services: {}\n")
    _patch_deploy_plan(
        monkeypatch,
        deploy,
        current=None,
        freshness=_freshness(
            CLASS_INITIALIZED,
            postgres_healthy=True,
            heads=frozenset({HEAD}),
        ),
    )
    result = run_deploy_plan(
        DeployPlanInput(
            project_name=project,
            env_file=env,
            compose_files=(compose, overlay),
            expected_revision=REV,
            repo_root=tmp_path,
            deploy_root=deploy,
            backup_root=backup,
        )
    )
    assert result.ok is False
    assert any("not a proven-empty bootstrap" in msg for msg in result.messages)


def test_existing_current_json_skips_initial_bootstrap_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[str] = []
    monkeypatch.setattr(
        "fetchnow_release.deploy_plan._plan_initial_bootstrap",
        lambda *_a, **_k: called.append("initial")
        or (_ for _ in ()).throw(AssertionError("should not plan initial")),
    )
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    backup = tmp_path / "backups"
    backup.mkdir()
    env = tmp_path / ".env"
    project = "fetchnow-deploy-plan-test-abcd1234"
    _write_env(env, project, deploy, backup)
    compose = tmp_path / "compose.yaml"
    overlay = tmp_path / "compose.staging.yaml"
    compose.write_text("services: {}\n")
    overlay.write_text("services: {}\n")
    current = SimpleNamespace(
        application=SimpleNamespace(
            revision="e" * 40, release_manifest_sha256="f" * 64
        ),
        database=SimpleNamespace(heads=(HEAD,), schema_release_revision="e" * 40),
    )
    _patch_deploy_plan(
        monkeypatch,
        deploy,
        current=current,
        freshness=_freshness(CLASS_CLEAN),
    )
    result = run_deploy_plan(
        DeployPlanInput(
            project_name=project,
            env_file=env,
            compose_files=(compose, overlay),
            expected_revision=REV,
            repo_root=tmp_path,
            deploy_root=deploy,
            backup_root=backup,
        )
    )
    assert called == []
    assert result.plan_json is None or "initial_bootstrap" not in result.plan_json


def test_deploy_plan_initialized_with_matching_journal_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    backup = tmp_path / "backups"
    backup.mkdir()
    env = tmp_path / ".env"
    project = "fetchnow-deploy-plan-test-abcd1234"
    _write_env(env, project, deploy, backup)
    compose = tmp_path / "compose.yaml"
    overlay = tmp_path / "compose.staging.yaml"
    compose.write_text("services: {}\n")
    overlay.write_text("services: {}\n")
    ensure_bootstrap_layout(deploy)
    boot = prepare_bootstrap_dir(deploy, BOOT_ID)
    write_bootstrap_plan(
        boot / BOOTSTRAP_PLAN_NAME,
        _plan_doc(project_name=project),
    )
    append_bootstrap_event(boot, STATUS_BOOTSTRAP_PLANNED, "plan")
    append_bootstrap_event(boot, STATUS_BOOTSTRAP_FRESHNESS_VERIFIED, "fresh")
    append_bootstrap_event(boot, STATUS_BOOTSTRAP_COMMITTED, "done")
    write_bootstrap_result(boot, _result_doc())
    _patch_deploy_plan(
        monkeypatch,
        deploy,
        current=None,
        freshness=_freshness(
            CLASS_INITIALIZED,
            postgres_healthy=True,
            heads=frozenset({HEAD}),
        ),
    )
    result = run_deploy_plan(
        DeployPlanInput(
            project_name=project,
            env_file=env,
            compose_files=(compose, overlay),
            expected_revision=REV,
            repo_root=tmp_path,
            deploy_root=deploy,
            backup_root=backup,
        )
    )
    assert result.ok is True
    plan = json.loads(result.plan_json or "{}")
    assert plan["initial_bootstrap"] is True
    assert plan["initial_schema_required"] is False
    assert plan["current_db_heads"] == [HEAD]


def test_catalog_sql_is_readonly_and_ignores_system_catalogs() -> None:
    lowered = USER_CATALOG_SQL.lower()
    assert "pg_catalog" in lowered
    assert "information_schema" in lowered
    assert "deptype = 'e'" in lowered or "deptype='e'" in lowered
    for banned in ("create table", "insert into", "drop table", "alter table"):
        assert banned not in lowered
    argv = _user_catalog_select_argv(
        project_name="fetchnow-bootstrap-test-abcd1234",
        env_file=Path("/tmp/.env"),
        compose_files=(Path("/tmp/compose.yaml"), Path("/tmp/compose.staging.yaml")),
    )
    assert_readonly_subprocess(argv)


def test_allowed_user_relations_allowlist_is_explicitly_empty() -> None:
    assert BOOTSTRAP_ALLOWED_USER_RELATIONS == frozenset()


def test_missing_alembic_with_arbitrary_table_is_not_fresh() -> None:
    alembic = DatabaseHeadsProbe(status="missing_table", heads=frozenset())
    catalog = UserCatalogProbe(
        status="ok",
        relations=("public.leftover_it:r",),
        extra_schemas=(),
    )
    classification, reason = classify_healthy_postgres_schema(alembic, catalog)
    assert classification == CLASS_DIRTY
    assert "public.leftover_it:r" in reason
    assert unexpected_catalog_objects(
        relations=("public.leftover_it:r",), extra_schemas=()
    ) == ("public.leftover_it:r",)


def test_missing_alembic_with_sequence_or_view_is_not_fresh() -> None:
    alembic = DatabaseHeadsProbe(status="missing_table", heads=frozenset())
    for rel in ("public.leftover_seq:S", "public.leftover_view:v"):
        catalog = UserCatalogProbe(status="ok", relations=(rel,), extra_schemas=())
        classification, reason = classify_healthy_postgres_schema(alembic, catalog)
        assert classification == CLASS_DIRTY
        assert rel in reason


def test_truly_empty_catalog_is_structurally_fresh() -> None:
    alembic = DatabaseHeadsProbe(status="missing_table", heads=frozenset())
    catalog = UserCatalogProbe(status="ok", relations=(), extra_schemas=())
    classification, reason = classify_healthy_postgres_schema(alembic, catalog)
    assert classification == CLASS_UNINITIALIZED
    assert "no unexpected user relations" in reason
    assert unexpected_catalog_objects(relations=(), extra_schemas=()) == ()


def test_catalog_inspection_failure_is_not_fresh() -> None:
    alembic = DatabaseHeadsProbe(status="missing_table", heads=frozenset())
    catalog = UserCatalogProbe(
        status="query_failed", relations=(), extra_schemas=(), detail="psql failed"
    )
    classification, reason = classify_healthy_postgres_schema(alembic, catalog)
    assert classification == CLASS_AMBIGUOUS
    assert "catalog inspection failed" in reason


def test_committed_journal_identity_rejects_revision_only(tmp_path: Path) -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    ensure_bootstrap_layout(deploy)
    boot = prepare_bootstrap_dir(deploy, BOOT_ID)
    write_bootstrap_plan(
        boot / BOOTSTRAP_PLAN_NAME,
        _plan_doc(project_name="fetchnow-bootstrap-test-other9999"),
    )
    append_bootstrap_event(boot, STATUS_BOOTSTRAP_PLANNED, "plan")
    append_bootstrap_event(boot, STATUS_BOOTSTRAP_FRESHNESS_VERIFIED, "fresh")
    append_bootstrap_event(boot, STATUS_BOOTSTRAP_COMMITTED, "done")
    write_bootstrap_result(boot, _result_doc())
    found = find_matching_committed_bootstrap(deploy, _identity())
    assert found is None


def test_committed_journal_identity_rejects_wrong_manifest_hash(
    tmp_path: Path,
) -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    ensure_bootstrap_layout(deploy)
    boot = prepare_bootstrap_dir(deploy, BOOT_ID)
    write_bootstrap_plan(
        boot / BOOTSTRAP_PLAN_NAME,
        _plan_doc(release_manifest_sha256="f" * 64),
    )
    append_bootstrap_event(boot, STATUS_BOOTSTRAP_PLANNED, "plan")
    append_bootstrap_event(boot, STATUS_BOOTSTRAP_FRESHNESS_VERIFIED, "fresh")
    append_bootstrap_event(boot, STATUS_BOOTSTRAP_COMMITTED, "done")
    write_bootstrap_result(boot, _result_doc())
    with pytest.raises(BootstrapJournalError, match="exact prepared-release identity"):
        find_matching_committed_bootstrap(deploy, _identity())


def test_committed_journal_identity_accepts_exact_binding(tmp_path: Path) -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    ensure_bootstrap_layout(deploy)
    boot = prepare_bootstrap_dir(deploy, BOOT_ID)
    write_bootstrap_plan(boot / BOOTSTRAP_PLAN_NAME, _plan_doc())
    append_bootstrap_event(boot, STATUS_BOOTSTRAP_PLANNED, "plan")
    append_bootstrap_event(boot, STATUS_BOOTSTRAP_FRESHNESS_VERIFIED, "fresh")
    append_bootstrap_event(boot, STATUS_BOOTSTRAP_COMMITTED, "done")
    write_bootstrap_result(boot, _result_doc())
    found = find_matching_committed_bootstrap(deploy, _identity())
    assert found is not None
    assert found.bootstrap_id == BOOT_ID


def test_bootstrap_ci_cleanup_rejects_staging_and_production(tmp_path: Path) -> None:
    script = ROOT / "scripts" / "release_bootstrap_db_ci_cleanup.py"
    for name in ("fetchnow-staging", "fetchnow-production"):
        path = tmp_path / "project.txt"
        path.write_text(name + "\n", encoding="utf-8")
        proc = subprocess.run(
            [
                sys.executable,
                str(script),
                "--project-file",
                str(path),
                "--repo-root",
                str(ROOT),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 1
        assert name in proc.stderr
