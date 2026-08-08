"""Unit tests for PRD1C3B2B1 backup verification foundation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fetchnow_pg_backup.attestation import (  # noqa: E402
    AttestationError,
    VerificationAttestationV2,
    attestation_sha256,
    build_attestation_v2,
    parse_attestation_bytes,
    write_attestation,
)
from fetchnow_pg_backup.checksums import sha256_file  # noqa: E402
from fetchnow_pg_backup.compose_target import ComposeTarget  # noqa: E402
from fetchnow_pg_backup.create import CreateResult, create_backup  # noqa: E402
from fetchnow_pg_backup.locking import BackupRootLock, LockError  # noqa: E402
from fetchnow_pg_backup.migrations_authority import (  # noqa: E402
    MigrationsAuthorityError,
    assert_expected_matches_static_heads,
    migrations_graph_content_sha256,
    validate_expected_alembic_heads,
    validate_migrations_versions_dir,
)
from fetchnow_pg_backup.semantic import SemanticCheckResult  # noqa: E402
from fetchnow_pg_backup.session import (  # noqa: E402
    BackupRootSession,
    BackupSessionError,
    open_backup_root_session,
)
from fetchnow_pg_backup.verify import VerifyResult  # noqa: E402


def _v2_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "schema_version": 2,
        "backup_id": "20260101T000000Z_abcd",
        "backup_sha256": "a" * 64,
        "verified_at_utc": "2026-01-01T00:00:00Z",
        "postgres_version": "16",
        "pg_restore_version": "16",
        "temporary_database": "fetchnow_restore_verify_abcd",
        "restore_duration_seconds": 1.0,
        "expected_alembic_heads": ["0001_base"],
        "static_alembic_heads": ["0001_base"],
        "migrations_graph_sha256": "b" * 64,
        "verified_restored_heads": ["0001_base"],
        "semantic_checks": [{"name": "connection", "ok": True, "detail": "ok"}],
        "cleanup": {"ok": True, "dropped_database": "fetchnow_restore_verify_abcd"},
        "result": "passed",
        "failure_reason": None,
        "tool_version": "1.0.0",
    }
    base.update(overrides)
    return base


def _multi_head_migrations(tmp_path: Path) -> Path:
    versions = tmp_path / "versions"
    versions.mkdir()
    (versions / "0001_base.py").write_text(
        'revision = "0001_base"\ndown_revision = None\n',
        encoding="utf-8",
    )
    (versions / "0002_branch_a.py").write_text(
        'revision = "0002_branch_a"\ndown_revision = "0001_base"\n',
        encoding="utf-8",
    )
    (versions / "0002_branch_b.py").write_text(
        'revision = "0002_branch_b"\ndown_revision = "0001_base"\n',
        encoding="utf-8",
    )
    return versions


def test_session_requires_context_manager(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    with pytest.raises(BackupSessionError):
        BackupRootSession(
            backup_root=backups,
            repo_root=repo,
            wait_lock=False,
            _lock=BackupRootLock(backups),
            _token=object(),  # type: ignore[arg-type]
        )


def test_session_rejects_use_before_entry(tmp_path: Path) -> None:
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
    with open_backup_root_session(backup_root=backups, repo_root=repo) as session:
        session._active = False
        with pytest.raises(BackupSessionError, match="not active"):
            session.create_backup(target=target, skip_free_space=True)


def test_session_create_verify_single_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    acquires: list[str] = []

    original_acquire = BackupRootLock.acquire

    def counting_acquire(self: BackupRootLock) -> None:
        acquires.append("acquire")
        return original_acquire(self)

    monkeypatch.setattr(BackupRootLock, "acquire", counting_acquire)

    def fake_create(**kwargs):  # noqa: ANN003
        return CreateResult(
            backup_id="bid",
            backup_dir=backups / "bid",
            manifest=__import__(
                "fetchnow_pg_backup.manifest", fromlist=["ManifestV1"]
            ).ManifestV1.from_dict(
                {
                    "schema_version": 1,
                    "backup_id": "bid",
                    "created_at_utc": "2026-01-01T00:00:00Z",
                    "compose_project": "p",
                    "database_name": "fetchnow",
                    "postgres_version": "16",
                    "pg_dump_version": "16",
                    "archive_format": "custom",
                    "compression": "gzip:6",
                    "dump_filename": "database.dump",
                    "dump_size_bytes": 1,
                    "sha256": "a" * 64,
                    "git_revision": "abc",
                    "structural_validation": {
                        "ok": True,
                        "tool": "pg_restore -l",
                        "entry_count": 1,
                    },
                    "duration_seconds": 1.0,
                    "tool_version": "1.0.0",
                }
            ),
        )

    def fake_verify(**kwargs):  # noqa: ANN003
        return VerifyResult(
            backup_id="bid",
            passed=True,
            temporary_database="fetchnow_restore_verify_x",
            attestation_path=backups / "bid" / "verifications" / "v.json",
            failure_reason=None,
            attestation_schema_version=2,
            attestation_sha256="b" * 64,
            expected_alembic_heads=("0001_baseline",),
            static_alembic_heads=("0001_baseline",),
            migrations_graph_sha256="c" * 64,
            verified_restored_heads=("0001_baseline",),
            semantic_success=True,
            cleanup_success=True,
            exact_head_proof=True,
        )

    monkeypatch.setattr(
        "fetchnow_pg_backup.create._create_backup_in_session", fake_create
    )
    monkeypatch.setattr(
        "fetchnow_pg_backup.verify._verify_backup_in_session", fake_verify
    )

    with open_backup_root_session(backup_root=backups, repo_root=repo) as session:
        created = session.create_backup(target=target, skip_free_space=True)
        verified = session.verify_backup(
            target=target,
            backup_dir=created.backup_dir,
            expected_alembic_heads=("0001_baseline",),
            migrations_versions_dir=ROOT / "backend" / "migrations" / "versions",
        )
    assert acquires == ["acquire"]
    assert verified.passed


def test_session_holds_lock_between_create_and_verify(
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

    def fake_create(**kwargs):  # noqa: ANN003
        return CreateResult(
            backup_id="bid",
            backup_dir=backups / "bid",
            manifest=__import__(
                "fetchnow_pg_backup.manifest", fromlist=["ManifestV1"]
            ).ManifestV1.from_dict(
                {
                    "schema_version": 1,
                    "backup_id": "bid",
                    "created_at_utc": "2026-01-01T00:00:00Z",
                    "compose_project": "p",
                    "database_name": "fetchnow",
                    "postgres_version": "16",
                    "pg_dump_version": "16",
                    "archive_format": "custom",
                    "compression": "gzip:6",
                    "dump_filename": "database.dump",
                    "dump_size_bytes": 1,
                    "sha256": "a" * 64,
                    "git_revision": "abc",
                    "structural_validation": {
                        "ok": True,
                        "tool": "pg_restore -l",
                        "entry_count": 1,
                    },
                    "duration_seconds": 1.0,
                    "tool_version": "1.0.0",
                }
            ),
        )

    monkeypatch.setattr(
        "fetchnow_pg_backup.create._create_backup_in_session", fake_create
    )
    monkeypatch.setattr(
        "fetchnow_pg_backup.verify._verify_backup_in_session",
        lambda **_k: VerifyResult(
            backup_id="bid",
            passed=True,
            temporary_database="fetchnow_restore_verify_x",
            attestation_path=backups / "bid" / "verifications" / "v.json",
            failure_reason=None,
            attestation_schema_version=2,
            attestation_sha256="b" * 64,
            expected_alembic_heads=("0001_baseline",),
            static_alembic_heads=("0001_baseline",),
            migrations_graph_sha256="c" * 64,
            verified_restored_heads=("0001_baseline",),
            semantic_success=True,
            cleanup_success=True,
            exact_head_proof=True,
        ),
    )

    with open_backup_root_session(backup_root=backups, repo_root=repo) as session:
        session.create_backup(target=target, skip_free_space=True)
        with pytest.raises(LockError):
            BackupRootLock(backups, wait=False).acquire()
        session.verify_backup(
            target=target,
            backup_dir=backups / "bid",
            expected_alembic_heads=("0001_baseline",),
            migrations_versions_dir=ROOT / "backend" / "migrations" / "versions",
        )


def test_independent_wrappers_acquire_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    acquires: list[str] = []
    original_acquire = BackupRootLock.acquire

    def counting_acquire(self: BackupRootLock) -> None:
        acquires.append("acquire")
        return original_acquire(self)

    monkeypatch.setattr(BackupRootLock, "acquire", counting_acquire)
    monkeypatch.setattr(
        "fetchnow_pg_backup.create._create_backup_in_session",
        lambda **_k: (_ for _ in ()).throw(AssertionError("not reached")),
    )
    with pytest.raises(AssertionError):
        create_backup(
            target=ComposeTarget(
                project_name="fetchnow-backup-test",
                env_file=tmp_path / "env",
                compose_files=(ROOT / "compose.yaml",),
                repo_root=repo,
            ),
            backup_root=backups,
            repo_root=repo,
            skip_free_space=True,
        )
    assert acquires == ["acquire"]


def test_lock_contention_unchanged(tmp_path: Path) -> None:
    root = tmp_path / "backups"
    root.mkdir(mode=0o700)
    held = BackupRootLock(root, wait=False)
    held.acquire()
    try:
        with pytest.raises(LockError):
            BackupRootLock(root, wait=False).acquire()
    finally:
        held.release()


def test_expected_static_mismatch_before_restore(tmp_path: Path) -> None:
    versions = _multi_head_migrations(tmp_path)
    with pytest.raises(MigrationsAuthorityError, match="do not equal static"):
        assert_expected_matches_static_heads(
            expected_alembic_heads=("0001_base",),
            migrations_versions_dir=versions,
        )


def test_multi_head_validation_and_static_match(tmp_path: Path) -> None:
    versions = _multi_head_migrations(tmp_path)
    expected = ("0002_branch_a", "0002_branch_b")
    assert validate_expected_alembic_heads(expected) == expected
    assert assert_expected_matches_static_heads(
        expected_alembic_heads=expected,
        migrations_versions_dir=versions,
    ) == expected


def test_duplicate_expected_heads_rejected() -> None:
    with pytest.raises(MigrationsAuthorityError, match="duplicate"):
        validate_expected_alembic_heads(("0001_base", "0001_base"))


def test_symlink_migrations_dir_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(Exception):
        validate_migrations_versions_dir(link)


def test_attestation_v2_strict_parse() -> None:
    payload = _v2_payload(
        expected_alembic_heads=["0001_base", "0002_branch_a"],
        static_alembic_heads=["0001_base", "0002_branch_a"],
        verified_restored_heads=["0001_base", "0002_branch_a"],
    )
    raw = json.dumps(payload, sort_keys=True).encode()
    parsed = parse_attestation_bytes(raw)
    assert isinstance(parsed, VerificationAttestationV2)
    assert parsed.exact_head_proof


def test_attestation_v2_unknown_field_rejected() -> None:
    payload = _v2_payload(extra=True)
    with pytest.raises(AttestationError, match="unknown fields"):
        parse_attestation_bytes(json.dumps(payload).encode())


def test_attestation_v2_duplicate_json_keys_rejected() -> None:
    text = '{"schema_version": 2, "schema_version": 2}'
    with pytest.raises(AttestationError, match="duplicate JSON"):
        parse_attestation_bytes(text.encode())


def test_attestation_v2_unsorted_heads_rejected() -> None:
    payload = _v2_payload(
        backup_id="x",
        temporary_database="fetchnow_restore_verify_x",
        expected_alembic_heads=["0002_branch_a", "0001_base"],
        static_alembic_heads=["0001_base", "0002_branch_a"],
        verified_restored_heads=["0001_base", "0002_branch_a"],
        cleanup={"ok": True, "dropped_database": "fetchnow_restore_verify_x"},
    )
    with pytest.raises(AttestationError, match="sorted unique"):
        parse_attestation_bytes(json.dumps(payload).encode())


def test_attestation_v1_legacy_parse_unchanged() -> None:
    payload = {
        "schema_version": 1,
        "backup_id": "legacy",
        "backup_sha256": "a" * 64,
        "verified_at_utc": "2026-01-01T00:00:00Z",
        "postgres_version": "16",
        "pg_restore_version": "16",
        "temporary_database": "fetchnow_restore_verify_x",
        "restore_duration_seconds": 1.0,
        "semantic_checks": [],
        "cleanup": {"ok": True, "dropped_database": "fetchnow_restore_verify_x"},
        "result": "passed",
        "failure_reason": None,
        "tool_version": "1.0.0",
    }
    raw = json.dumps(payload, sort_keys=True).encode()
    parsed = parse_attestation_bytes(raw)
    assert parsed.schema_version == 1
    assert not hasattr(parsed, "expected_alembic_heads")


def test_attestation_v2_immutable_write(tmp_path: Path) -> None:
    backup = tmp_path / "backup"
    backup.mkdir()
    att = build_attestation_v2(
        backup_id="bid",
        backup_sha256="a" * 64,
        postgres_version="16",
        pg_restore_version="16",
        temporary_database="fetchnow_restore_verify_x",
        restore_duration_seconds=1.0,
        expected_alembic_heads=("0001_base",),
        static_alembic_heads=("0001_base",),
        migrations_graph_sha256="b" * 64,
        verified_restored_heads=("0001_base",),
        semantic_checks=[
            SemanticCheckResult("connection", True, "ok"),
        ],
        cleanup_ok=True,
        dropped_database="fetchnow_restore_verify_x",
        result="passed",
        failure_reason=None,
        tool_version="1.0.0",
    )
    path1 = write_attestation(backup, att)
    path2 = write_attestation(backup, att)
    assert path1 != path2
    assert path1.exists() and path2.exists()
    assert attestation_sha256(path1) == sha256_file(path1)


def test_passed_v2_requires_cleanup_and_semantic() -> None:
    with pytest.raises(ValueError, match="cleanup"):
        build_attestation_v2(
            backup_id="bid",
            backup_sha256="a" * 64,
            postgres_version="16",
            pg_restore_version="16",
            temporary_database="fetchnow_restore_verify_x",
            restore_duration_seconds=1.0,
            expected_alembic_heads=("0001_base",),
            static_alembic_heads=("0001_base",),
            migrations_graph_sha256="b" * 64,
            verified_restored_heads=("0001_base",),
            semantic_checks=[SemanticCheckResult("connection", True, "ok")],
            cleanup_ok=False,
            dropped_database=None,
            result="passed",
            failure_reason=None,
            tool_version="1.0.0",
        )


def test_attestation_v2_failed_allows_null_restored_heads() -> None:
    payload = _v2_payload(
        result="failed",
        failure_reason="expected/static mismatch",
        verified_restored_heads=None,
        cleanup={"ok": False, "dropped_database": None},
        semantic_checks=[],
        temporary_database=None,
        restore_duration_seconds=None,
        expected_alembic_heads=["0001_base"],
        static_alembic_heads=["0002_branch_a"],
    )
    parsed = parse_attestation_bytes(json.dumps(payload, sort_keys=True).encode())
    assert isinstance(parsed, VerificationAttestationV2)
    assert parsed.result == "failed"
    assert parsed.verified_restored_heads is None
    assert not parsed.exact_head_proof


def test_migrations_graph_content_sha256_is_stable(tmp_path: Path) -> None:
    versions = _multi_head_migrations(tmp_path)
    first = migrations_graph_content_sha256(versions)
    second = migrations_graph_content_sha256(versions)
    assert first == second
    assert len(first) == 64


def test_attestation_collision_uses_suffix_not_overwrite(tmp_path: Path) -> None:
    backup = tmp_path / "backup"
    backup.mkdir()
    att = build_attestation_v2(
        backup_id="bid",
        backup_sha256="a" * 64,
        postgres_version="16",
        pg_restore_version="16",
        temporary_database="fetchnow_restore_verify_x",
        restore_duration_seconds=1.0,
        expected_alembic_heads=("0001_base",),
        static_alembic_heads=("0001_base",),
        migrations_graph_sha256="b" * 64,
        verified_restored_heads=("0001_base",),
        semantic_checks=[SemanticCheckResult("connection", True, "ok")],
        cleanup_ok=True,
        dropped_database="fetchnow_restore_verify_x",
        result="passed",
        failure_reason=None,
        tool_version="1.0.0",
    )
    path1 = write_attestation(backup, att)
    path2 = write_attestation(backup, att)
    assert path1 != path2
    assert path1.read_bytes() == path2.read_bytes()
