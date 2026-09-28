"""SEC-01 extended short-probe fail-closed regressions + docker rm timeout."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from fetchnow_release.bootstrap_cleanup import (  # noqa: E402
    BootstrapCleanupError,
    RemovedBootstrapContainer,
    remove_owned_bootstrap_containers,
)
from fetchnow_release.bootstrap_freshness import (  # noqa: E402
    BootstrapFreshnessError,
    CLASS_AMBIGUOUS,
    classify_healthy_postgres_schema,
    list_project_volumes,
    probe_user_catalog,
)
from fetchnow_release.bounded_subprocess import (  # noqa: E402
    BoundedCancelledError,
    BoundedResult,
    BoundedTimeoutError,
    OutputLimitExceededError,
)
from fetchnow_release.db_heads import (  # noqa: E402
    DatabaseHeadsProbe,
    DbHeadsError,
    database_heads_via_postgres,
    probe_database_heads,
)
from fetchnow_release.image_build import (  # noqa: E402
    ImageBuildError,
    compose_version,
    docker_version,
    image_exists,
)
from fetchnow_release.migrate import MigrationError, _snapshot_container_ids  # noqa: E402


def _ok(stdout: bytes = b"", *, argv: tuple[str, ...] = ("docker",)) -> BoundedResult:
    return BoundedResult(argv=argv, returncode=0, stdout=stdout, stderr=b"")


def _fail(stderr: bytes, *, code: int = 1) -> BoundedResult:
    return BoundedResult(
        argv=("docker",), returncode=code, stdout=b"", stderr=stderr
    )


# --- db_heads -----------------------------------------------------------------


def test_probe_database_heads_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "fetchnow_release.db_heads.run_docker_probe",
        lambda *a, **k: _ok(b"0001_baseline\n"),
    )
    probe = probe_database_heads(
        project_name="p",
        env_file=Path("/tmp/e"),
        compose_files=(Path("/tmp/c.yaml"),),
        cwd=Path("/tmp"),
    )
    assert probe.status == "heads"
    assert probe.heads == frozenset({"0001_baseline"})


def test_probe_database_heads_missing_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "fetchnow_release.db_heads.run_docker_probe",
        lambda *a, **k: _fail(b'relation "alembic_version" does not exist'),
    )
    probe = probe_database_heads(
        project_name="p",
        env_file=Path("/tmp/e"),
        compose_files=(Path("/tmp/c.yaml"),),
        cwd=Path("/tmp"),
    )
    assert probe.table_missing
    assert probe.heads == frozenset()


def test_probe_database_heads_timeout_is_query_failed_not_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*a, **k):  # noqa: ANN001
        raise BoundedTimeoutError("timed out")

    monkeypatch.setattr("fetchnow_release.db_heads.run_docker_probe", boom)
    probe = probe_database_heads(
        project_name="p",
        env_file=Path("/tmp/e"),
        compose_files=(Path("/tmp/c.yaml"),),
        cwd=Path("/tmp"),
    )
    assert probe.status == "query_failed"
    assert probe.heads == frozenset()
    with pytest.raises(DbHeadsError, match="failed to read"):
        database_heads_via_postgres(
            project_name="p",
            env_file=Path("/tmp/e"),
            compose_files=(Path("/tmp/c.yaml"),),
            cwd=Path("/tmp"),
        )


def test_probe_database_heads_cancel_is_query_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*a, **k):  # noqa: ANN001
        raise BoundedCancelledError("cancelled")

    monkeypatch.setattr("fetchnow_release.db_heads.run_docker_probe", boom)
    probe = probe_database_heads(
        project_name="p",
        env_file=Path("/tmp/e"),
        compose_files=(Path("/tmp/c.yaml"),),
        cwd=Path("/tmp"),
    )
    assert probe.status == "query_failed"


def test_probe_database_heads_truncated_output_not_partial_heads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*a, **k):  # noqa: ANN001
        raise OutputLimitExceededError("stdout exceeded")

    monkeypatch.setattr("fetchnow_release.db_heads.run_docker_probe", boom)
    probe = probe_database_heads(
        project_name="p",
        env_file=Path("/tmp/e"),
        compose_files=(Path("/tmp/c.yaml"),),
        cwd=Path("/tmp"),
    )
    assert probe.status == "query_failed"
    assert probe.heads == frozenset()


# --- bootstrap_freshness ------------------------------------------------------


def test_list_project_volumes_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "fetchnow_release.bootstrap_freshness.run_docker_probe",
        lambda *a, **k: _ok(b"proj_pgdata\nproj_tmp\n"),
    )
    names = list_project_volumes(project_name="proj")
    assert names == ("proj_pgdata", "proj_tmp")


def test_list_project_volumes_timeout_fail_closed_not_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*a, **k):  # noqa: ANN001
        raise BoundedTimeoutError("timed out")

    monkeypatch.setattr("fetchnow_release.bootstrap_freshness.run_docker_probe", boom)
    with pytest.raises(BootstrapFreshnessError, match="timed out"):
        list_project_volumes(project_name="proj")


def test_probe_user_catalog_timeout_not_empty_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*a, **k):  # noqa: ANN001
        raise BoundedTimeoutError("timed out")

    monkeypatch.setattr("fetchnow_release.bootstrap_freshness.run_docker_probe", boom)
    catalog = probe_user_catalog(
        project_name="p",
        env_file=Path("/tmp/e"),
        compose_files=(Path("/tmp/c.yaml"),),
        cwd=Path("/tmp"),
    )
    assert catalog.status == "query_failed"
    assert catalog.relations == ()
    classification, _reason = classify_healthy_postgres_schema(
        DatabaseHeadsProbe(status="missing_table", heads=frozenset()),
        catalog,
    )
    assert classification == CLASS_AMBIGUOUS


def test_catalog_nonzero_exit_is_query_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "fetchnow_release.bootstrap_freshness.run_docker_probe",
        lambda *a, **k: _fail(b"psql: connection refused"),
    )
    catalog = probe_user_catalog(
        project_name="p",
        env_file=Path("/tmp/e"),
        compose_files=(Path("/tmp/c.yaml"),),
        cwd=Path("/tmp"),
    )
    assert catalog.status == "query_failed"


# --- migrate snapshot ---------------------------------------------------------


def test_snapshot_container_ids_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake(argv, **kwargs):  # noqa: ANN001
        svc = argv[-1]
        calls.append(svc)
        return _ok(f"cid-{svc}\n".encode())

    monkeypatch.setattr("fetchnow_release.migrate.run_docker_probe", fake)
    ids = _snapshot_container_ids(
        project_name="p",
        env_file=Path("/tmp/e"),
        compose_files=(Path("/tmp/c.yaml"),),
        cwd=Path("/tmp"),
    )
    assert ids
    assert all(v.startswith("cid-") for v in ids.values())


def test_snapshot_timeout_not_missing_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*a, **k):  # noqa: ANN001
        raise BoundedTimeoutError("timed out")

    monkeypatch.setattr("fetchnow_release.migrate.run_docker_probe", boom)
    with pytest.raises(MigrationError, match="timed out") as ei:
        _snapshot_container_ids(
            project_name="p",
            env_file=Path("/tmp/e"),
            compose_files=(Path("/tmp/c.yaml"),),
            cwd=Path("/tmp"),
        )
    assert "missing running container" not in str(ei.value)


def test_snapshot_nonzero_is_failed_not_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "fetchnow_release.migrate.run_docker_probe",
        lambda *a, **k: _fail(b"daemon error"),
    )
    with pytest.raises(MigrationError, match="compose ps failed"):
        _snapshot_container_ids(
            project_name="p",
            env_file=Path("/tmp/e"),
            compose_files=(Path("/tmp/c.yaml"),),
            cwd=Path("/tmp"),
        )


# --- image_build short probes -------------------------------------------------


def test_docker_version_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "fetchnow_release.image_build.run_docker_probe",
        lambda *a, **k: _ok(b"24.0.0\n"),
    )
    assert docker_version() == "24.0.0"


def test_compose_version_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):  # noqa: ANN001
        raise BoundedTimeoutError("timed out")

    monkeypatch.setattr("fetchnow_release.image_build.run_docker_probe", boom)
    with pytest.raises(ImageBuildError, match="timed out"):
        compose_version()


def test_image_exists_timeout_not_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):  # noqa: ANN001
        raise BoundedTimeoutError("timed out")

    monkeypatch.setattr("fetchnow_release.image_build.run_docker_probe", boom)
    with pytest.raises(ImageBuildError, match="timed out"):
        image_exists("fetchnow-api:deadbeef")


def test_image_exists_missing_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "fetchnow_release.image_build.run_docker_probe",
        lambda *a, **k: _fail(b"No such image"),
    )
    assert image_exists("missing:tag") is False


# --- docker rm -f timeout -----------------------------------------------------


def test_docker_rm_timeout_is_cleanup_error_not_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owned = [
        RemovedBootstrapContainer(
            container_id="abc123deadbeef",
            service="api",
            labels={"com.fetchnow.deployment-id": "d" * 36},
        )
    ]
    monkeypatch.setattr(
        "fetchnow_release.bootstrap_cleanup.discover_owned_bootstrap_containers",
        lambda **_k: owned,
    )
    calls: list[list[str]] = []

    def boom(argv, **kwargs):  # noqa: ANN001
        calls.append(list(argv))
        raise BoundedTimeoutError("rm timed out")

    monkeypatch.setattr(
        "fetchnow_release.bootstrap_cleanup.run_docker_probe", boom
    )
    with pytest.raises(BootstrapCleanupError, match="timed out"):
        remove_owned_bootstrap_containers(
            project_name="fetchnow-rollout-test-abc12345",
            env_file=Path("/tmp/env"),
            compose_files=(Path("/tmp/c.yaml"),),
            cwd=Path("/tmp"),
            deployment_id="12345678-1234-1234-1234-123456789abc",
            release_revision="a" * 40,
            expected_container_ids=["abc123deadbeef"],
        )
    assert len(calls) == 1
    assert calls[0][:3] == ["docker", "rm", "-f"]
    # Journal propagation of BootstrapCleanupError → unresolved deployment is
    # covered by test_bootstrap_cleanup_failure_leaves_unresolved_journal.
