"""Unit tests for FetchNow PostgreSQL backup tooling (PRD1B)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fetchnow_pg_backup import DUMP_FILENAME, MANIFEST_FILENAME, RESTORE_DB_PREFIX  # noqa: E402
from fetchnow_pg_backup.attestation import build_attestation, write_attestation  # noqa: E402
from fetchnow_pg_backup.checksums import sha256_bytes, sha256_file  # noqa: E402
from fetchnow_pg_backup.compose_target import ComposeTarget, build_compose_argv  # noqa: E402
from fetchnow_pg_backup.create import allocate_backup_id, create_backup  # noqa: E402
from fetchnow_pg_backup.free_space import evaluate_free_space, required_free_bytes  # noqa: E402
from fetchnow_pg_backup.fs_safety import (  # noqa: E402
    PathSafetyError,
    atomic_publish_directory,
    mode_bits,
    remove_directory_tree,
    validate_backup_root,
)
from fetchnow_pg_backup.locking import BackupRootLock, LockError  # noqa: E402
from fetchnow_pg_backup.list_backups import list_backups  # noqa: E402
from fetchnow_pg_backup.manifest import ManifestError, ManifestV1, StructuralValidation  # noqa: E402
from fetchnow_pg_backup.postgres_ops import (  # noqa: E402
    assert_safe_temp_database_name,
    generate_restore_database_name,
)
from fetchnow_pg_backup.process_util import CommandResult, run_command  # noqa: E402
from fetchnow_pg_backup.prune import prune_backups, select_prune_candidates  # noqa: E402
from fetchnow_pg_backup.redaction import redact  # noqa: E402
from fetchnow_pg_backup.semantic import discover_alembic_head_revisions  # noqa: E402


def _manifest(**overrides: object) -> ManifestV1:
    base = dict(
        schema_version=1,
        backup_id="20260804T012345Z_deadbeef",
        created_at_utc="2026-08-04T01:23:45Z",
        compose_project="fetchnow-backup-test",
        database_name="fetchnow",
        postgres_version="16.9",
        pg_dump_version="16.9",
        archive_format="custom",
        compression="gzip:6",
        dump_filename=DUMP_FILENAME,
        dump_size_bytes=12,
        sha256="a" * 64,
        git_revision="deadbeef",
        structural_validation=StructuralValidation(True, "pg_restore -l", 3),
        duration_seconds=1.0,
        tool_version="1.0.0",
    )
    base.update(overrides)
    return ManifestV1(**base)  # type: ignore[arg-type]


def test_manifest_roundtrip(tmp_path: Path) -> None:
    m = _manifest()
    path = tmp_path / MANIFEST_FILENAME
    path.write_text(m.to_json(), encoding="utf-8")
    loaded = ManifestV1.load(path)
    assert loaded.backup_id == m.backup_id
    assert loaded.sha256 == m.sha256


def test_unsupported_manifest_schema() -> None:
    with pytest.raises(ManifestError, match="unsupported"):
        ManifestV1.from_dict({**_manifest().to_dict(), "schema_version": 99})


def test_path_traversal_dump_filename_rejected() -> None:
    with pytest.raises(ManifestError, match="unsafe|unexpected"):
        ManifestV1.from_dict({**_manifest().to_dict(), "dump_filename": "../x.dump"})


def test_unsafe_backup_roots(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    with pytest.raises(PathSafetyError):
        validate_backup_root(Path("/"), repo_root=repo, home=home)
    with pytest.raises(PathSafetyError):
        validate_backup_root(home, repo_root=repo, home=home)
    with pytest.raises(PathSafetyError):
        validate_backup_root(repo, repo_root=repo, home=home)
    inside = repo / "backups"
    inside.mkdir()
    with pytest.raises(PathSafetyError):
        validate_backup_root(inside, repo_root=repo, home=home)


def test_symlink_backup_root_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    real = tmp_path / "real-backups"
    real.mkdir()
    link = tmp_path / "link-backups"
    link.symlink_to(real)
    with pytest.raises(PathSafetyError):
        validate_backup_root(link, repo_root=repo, home=tmp_path / "home")


def test_sha256_and_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "f.bin"
    path.write_bytes(b"abc")
    digest = sha256_file(path)
    assert digest == sha256_bytes(b"abc")
    assert digest != sha256_bytes(b"abd")


def test_atomic_publish_and_permissions(tmp_path: Path) -> None:
    root = tmp_path / "backups"
    root.mkdir(mode=0o700)
    incomplete = root / ".incomplete-abc"
    incomplete.mkdir(mode=0o700)
    dump = incomplete / DUMP_FILENAME
    dump.write_bytes(b"dump")
    os.chmod(dump, 0o600)
    final = root / "20260804T012345Z_abc"
    atomic_publish_directory(incomplete_dir=incomplete, final_dir=final)
    assert final.is_dir()
    assert not incomplete.exists()
    assert mode_bits(dump if False else final / DUMP_FILENAME) == 0o600


def test_cleanup_after_simulated_pg_dump_failure(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    env = tmp_path / "env"
    env.write_text(
        "POSTGRES_DB=fetchnow\nPOSTGRES_USER=fetchnow\nPOSTGRES_PASSWORD=x\n"
    )
    target = ComposeTarget(
        project_name="fetchnow-backup-test",
        env_file=env,
        compose_files=(ROOT / "compose.yaml",),
        repo_root=repo,
    )

    def runner(argv, cwd=None, input_bytes=None):  # noqa: ANN001
        joined = " ".join(argv)
        if "pg_isready" in joined or "command -v" in joined:
            return CommandResult(tuple(argv), 0, b"ok\n", b"")
        if "SHOW server_version" in joined or "pg_dump --version" in joined:
            return CommandResult(
                tuple(argv),
                0,
                b"16.9\npg_dump (PostgreSQL) 16.9\npg_restore (PostgreSQL) 16.9\n",
                b"",
            )
        if "current_database" in joined:
            return CommandResult(tuple(argv), 0, b"fetchnow\n", b"")
        if "pg_database_size" in joined:
            return CommandResult(tuple(argv), 0, b"1000\n", b"")
        if "ps" in argv and "-q" in argv:
            return CommandResult(tuple(argv), 0, b"abc\n", b"")
        if "pg_dump" in joined:
            return CommandResult(tuple(argv), 1, b"", b"dump failed")
        return CommandResult(tuple(argv), 0, b"", b"")

    with pytest.raises(Exception):
        create_backup(
            target=target,
            backup_root=backups,
            repo_root=repo,
            runner=runner,
            skip_free_space=True,
        )
    assert list(backups.glob(".incomplete-*")) == []
    assert list(backups.glob("*T*Z_*")) == []


def test_cleanup_after_simulated_manifest_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    env = tmp_path / "env"
    env.write_text("X=1\n")
    target = ComposeTarget(
        project_name="fetchnow-backup-test",
        env_file=env,
        compose_files=(ROOT / "compose.yaml",),
        repo_root=repo,
    )

    def runner(argv, cwd=None, input_bytes=None):  # noqa: ANN001
        joined = " ".join(argv)
        if "ps" in argv and "-q" in argv:
            return CommandResult(tuple(argv), 0, b"cid\n", b"")
        if "pg_isready" in joined or "command -v" in joined:
            return CommandResult(tuple(argv), 0, b"ok\n", b"")
        if "SHOW server_version" in joined or "pg_dump --version" in joined:
            return CommandResult(
                tuple(argv),
                0,
                b"16.9\npg_dump (PostgreSQL) 16.9\npg_restore (PostgreSQL) 16.9\n",
                b"",
            )
        if "current_database" in joined:
            return CommandResult(tuple(argv), 0, b"fetchnow\n", b"")
        if "pg_dump" in joined:
            return CommandResult(tuple(argv), 0, b"FAKE DUMP DATA", b"")
        if "pg_restore" in joined and "-l" in argv:
            return CommandResult(tuple(argv), 0, b"1; 0 0 TABLE data\n", b"")
        return CommandResult(tuple(argv), 0, b"", b"")

    def boom(*_a, **_k):  # noqa: ANN001
        raise RuntimeError("manifest write failed")

    monkeypatch.setattr("fetchnow_pg_backup.create.write_manifest", boom)
    with pytest.raises(RuntimeError, match="manifest write failed"):
        create_backup(
            target=target,
            backup_root=backups,
            repo_root=repo,
            runner=runner,
            skip_free_space=True,
        )
    assert list(backups.glob(".incomplete-*")) == []


def test_lock_contention(tmp_path: Path) -> None:
    root = tmp_path / "backups"
    root.mkdir(mode=0o700)
    held = BackupRootLock(root, wait=False)
    held.acquire()
    try:
        with pytest.raises(LockError):
            BackupRootLock(root, wait=False).acquire()
    finally:
        held.release()


def test_restore_db_name_safety() -> None:
    name = generate_restore_database_name(source_database="fetchnow")
    assert name.startswith(RESTORE_DB_PREFIX)
    assert name != "fetchnow"
    with pytest.raises(Exception):
        assert_safe_temp_database_name("fetchnow", source_database="fetchnow")
    with pytest.raises(Exception):
        assert_safe_temp_database_name("otherdb", source_database="fetchnow")


def test_attestation_creation(tmp_path: Path) -> None:
    backup = tmp_path / "20260804T012345Z_deadbeef"
    backup.mkdir()
    att = build_attestation(
        backup_id=backup.name,
        backup_sha256="a" * 64,
        postgres_version="16.9",
        pg_restore_version="16.9",
        temporary_database="fetchnow_restore_verify_abc",
        restore_duration_seconds=1.2,
        semantic_checks=[],
        cleanup_ok=True,
        dropped_database="fetchnow_restore_verify_abc",
        result="passed",
        failure_reason=None,
        tool_version="1.0.0",
    )
    path = write_attestation(backup, att)
    assert path.is_file()
    assert mode_bits(path) == 0o600
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["result"] == "passed"
    assert "password" not in path.read_text(encoding="utf-8").lower()
    # Incomplete temp should not remain after atomic replace.
    assert not any(
        p.name.startswith(".incomplete-") for p in (backup / "verifications").iterdir()
    )


def test_attestation_rejects_untruthful_passed() -> None:
    from fetchnow_pg_backup.semantic import SemanticCheckResult

    with pytest.raises(ValueError, match="cleanup"):
        build_attestation(
            backup_id="x",
            backup_sha256="a" * 64,
            postgres_version="16.9",
            pg_restore_version="16.9",
            temporary_database="fetchnow_restore_verify_abc",
            restore_duration_seconds=1.0,
            semantic_checks=[],
            cleanup_ok=False,
            dropped_database=None,
            result="passed",
            failure_reason=None,
            tool_version="1.0.0",
        )
    with pytest.raises(ValueError, match="semantic"):
        build_attestation(
            backup_id="x",
            backup_sha256="a" * 64,
            postgres_version="16.9",
            pg_restore_version="16.9",
            temporary_database="fetchnow_restore_verify_abc",
            restore_duration_seconds=1.0,
            semantic_checks=[SemanticCheckResult("x", False, "no")],
            cleanup_ok=True,
            dropped_database="fetchnow_restore_verify_abc",
            result="passed",
            failure_reason=None,
            tool_version="1.0.0",
        )


def test_integration_project_name_safety() -> None:
    from fetchnow_pg_backup.integration_project import (
        ProjectNameError,
        assert_safe_project,
        make_project_name,
    )

    for bad in (
        "",
        "fetchnow",
        "fetchnow-staging",
        "fetchnow-prod",
        "fetchnow-production",
        "fetchnow-backup-test",
        "fetchnow-backup-test-",
        "other-project",
        "fetchnow-backup-test-SHORT",
    ):
        with pytest.raises(ProjectNameError):
            assert_safe_project(bad)
    good = make_project_name()
    assert good.startswith("fetchnow-backup-test-")
    assert assert_safe_project(good) == good
    assert make_project_name() != good


def test_operator_backup_cli_rejects_ephemeral_and_forbidden_names(
    tmp_path: Path,
) -> None:
    from fetchnow_pg_backup.cli import main as backup_main

    env = tmp_path / ".env.release-test"
    env.write_text("X=1\n", encoding="utf-8")
    compose = tmp_path / "compose.yaml"
    overlay = tmp_path / "compose.staging.yaml"
    compose.write_text("services: {}\n")
    overlay.write_text("services: {}\n")
    for name in (
        "fetchnow-backup-test-abcd1234",
        "fetchnow-prod",
        "fetchnow-rollout-test-abcd1234",
    ):
        with pytest.raises(SystemExit, match="fetchnow-staging or fetchnow-production"):
            backup_main(
                [
                    "create",
                    "--project-name",
                    name,
                    "--env-file",
                    str(env),
                    "--compose-file",
                    str(compose),
                    "--compose-file",
                    str(overlay),
                    "--backup-root",
                    str(tmp_path / "backups"),
                ]
            )


def test_project_file_cleanup_validation(tmp_path: Path) -> None:
    from fetchnow_pg_backup.integration_project import (
        ProjectNameError,
        load_project_file,
    )

    missing = tmp_path / "missing.txt"
    with pytest.raises(ProjectNameError, match="missing"):
        load_project_file(missing)

    empty = tmp_path / "empty.txt"
    empty.write_text("\n")
    with pytest.raises(ProjectNameError, match="empty"):
        load_project_file(empty)

    for bad in (
        "fetchnow",
        "fetchnow-staging",
        "fetchnow-prod",
        "fetchnow-production",
        "fetchnow-backup-test",
    ):
        path = tmp_path / f"{bad}.txt"
        path.write_text(bad + "\n")
        with pytest.raises(ProjectNameError):
            load_project_file(path)

    good = tmp_path / "good.txt"
    good.write_text("fetchnow-backup-test-abcdef012345\n")
    assert load_project_file(good) == "fetchnow-backup-test-abcdef012345"


def test_ci_cleanup_refuses_missing_or_forbidden(tmp_path: Path) -> None:
    import importlib.util

    path = ROOT / "scripts" / "pg_backup_ci_cleanup.py"
    spec = importlib.util.spec_from_file_location("pg_backup_ci_cleanup", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    missing = tmp_path / "nope.txt"
    # Missing project file: refuse mutation, exit 0 (nothing to clean).
    assert (
        mod.main(
            [
                "--project-file",
                str(missing),
                "--compose-file",
                str(ROOT / "compose.yaml"),
                "--repo-root",
                str(ROOT),
            ]
        )
        == 0
    )

    bad = tmp_path / "bad.txt"
    bad.write_text("fetchnow-staging\n")
    assert (
        mod.main(
            [
                "--project-file",
                str(bad),
                "--compose-file",
                str(ROOT / "compose.yaml"),
                "--repo-root",
                str(ROOT),
            ]
        )
        == 1
    )


def _write_valid_backup(root: Path, backup_id: str, *, verified: bool = False) -> None:
    d = root / backup_id
    d.mkdir()
    dump = d / DUMP_FILENAME
    dump.write_bytes(b"x" * 20)
    m = _manifest(backup_id=backup_id, dump_size_bytes=20, sha256=sha256_file(dump))
    (d / MANIFEST_FILENAME).write_text(m.to_json(), encoding="utf-8")
    if verified:
        write_attestation(
            d,
            build_attestation(
                backup_id=backup_id,
                backup_sha256=m.sha256,
                postgres_version="16.9",
                pg_restore_version="16.9",
                temporary_database="fetchnow_restore_verify_x",
                restore_duration_seconds=1.0,
                semantic_checks=[],
                cleanup_ok=True,
                dropped_database="fetchnow_restore_verify_x",
                result="passed",
                failure_reason=None,
                tool_version="1.0.0",
            ),
        )


def test_list_with_invalid_entries(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    root = tmp_path / "backups"
    root.mkdir(mode=0o700)
    _write_valid_backup(root, "20260804T010000Z_aaaa")
    (root / "junk").write_text("nope")
    (root / ".incomplete-xyz").mkdir()
    result = list_backups(backup_root=root, repo_root=repo)
    statuses = {i.backup_id: i.status for i in result.items}
    assert statuses["20260804T010000Z_aaaa"] == "valid"
    assert statuses["junk"] == "unknown"
    assert statuses[".incomplete-xyz"] == "incomplete"
    assert result.invalid_entries


def test_retention_and_prune_safety(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    root = tmp_path / "backups"
    root.mkdir(mode=0o700)
    ids = [
        "20260804T010000Z_a",
        "20260804T020000Z_b",
        "20260804T030000Z_c",
        "20260804T040000Z_d",
    ]
    for i, bid in enumerate(ids):
        _write_valid_backup(root, bid, verified=(bid == "20260804T020000Z_b"))
    (root / "weird").mkdir()
    decisions = select_prune_candidates(backup_root=root, repo_root=repo, keep=2)
    by_id = {d.backup_id: d for d in decisions}
    assert by_id["20260804T040000Z_d"].action == "keep"  # newest valid
    assert by_id["20260804T020000Z_b"].action == "keep"  # newest verified
    assert by_id["weird"].action == "skip"
    dry = prune_backups(backup_root=root, repo_root=repo, keep=2, apply=False)
    assert dry.dry_run and dry.deleted == []
    applied = prune_backups(backup_root=root, repo_root=repo, keep=2, apply=True)
    assert "20260804T040000Z_d" in {
        d.backup_id for d in applied.decisions if d.action == "keep"
    }
    assert "20260804T020000Z_b" not in applied.deleted
    assert (root / "20260804T040000Z_d").is_dir()
    assert (root / "weird").is_dir()  # unknown not deleted


def test_subprocess_list_form_and_redaction() -> None:
    result = run_command(["/bin/echo", "hello"])
    assert result.returncode == 0
    assert result.stdout_text.strip() == "hello"
    text = redact("POSTGRES_PASSWORD=supersecret postgresql://u:secret@host/db")
    assert "supersecret" not in text
    assert "secret" not in text or "***" in text


def test_free_space_decision() -> None:
    req = required_free_bytes(1000, multiplier=2, margin_bytes=100)
    assert req == 2100
    bad = evaluate_free_space(
        database_bytes=1000, free_bytes=100, multiplier=2, margin_bytes=100
    )
    assert not bad.ok
    good = evaluate_free_space(
        database_bytes=1000, free_bytes=5000, multiplier=2, margin_bytes=100
    )
    assert good.ok


def test_discover_alembic_heads() -> None:
    heads = discover_alembic_head_revisions(
        ROOT / "backend" / "migrations" / "versions"
    )
    assert heads == ("0006_browser_delivery_grants",)


def test_compose_argv_requires_project() -> None:
    with pytest.raises(ValueError):
        ComposeTarget(
            project_name=" ", env_file=Path("/tmp/x"), compose_files=(Path("/tmp/a"),)
        )
    target = ComposeTarget(
        project_name="fetchnow-backup-test",
        env_file=Path("/tmp/env"),
        compose_files=(Path("/tmp/compose.yaml"),),
    )
    argv = build_compose_argv(target, "ps")
    assert argv[:2] == ["docker", "compose"]
    assert "--project-name" in argv
    assert "fetchnow-backup-test" in argv
    assert all(isinstance(x, str) for x in argv)


def test_remove_tree_refuses_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(PathSafetyError):
        remove_directory_tree(link)


def test_allocate_collision(tmp_path: Path) -> None:
    root = tmp_path
    first = allocate_backup_id(root, "abcd")
    (root / first).mkdir()
    second = allocate_backup_id(root, "abcd")
    assert second != first
