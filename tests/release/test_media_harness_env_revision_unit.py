"""Offline regressions for disposable-env revision sync before prepare."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fetchnow_release.env_file import (  # noqa: E402
    EnvFileError,
    load_env_file,
    replace_env_file_key,
)
from fetchnow_release.media_release_evidence import (  # noqa: E402
    MATRIX_CASE_NAMES,
    mark_remaining_cases_not_run,
    prepare_result_snapshot,
)
from fetchnow_release.preflight import PreflightInput, run_preflight  # noqa: E402
from password_fixture import valid_test_password  # noqa: E402

REV_A = "a" * 40
REV_B = "b" * 40
PASSWORD = valid_test_password()


def _disposable_env(path: Path, *, revision: str = REV_A) -> Path:
    """Shape mirrors the media harness disposable env (revision + deny flags)."""
    path.write_text(
        "\n".join(
            [
                "COMPOSE_PROJECT_NAME=fetchnow-media-test",
                f"FETCHNOW_RELEASE_REVISION={revision}",
                f"POSTGRES_PASSWORD={PASSWORD}",
                "GATEWAY_PORT=18091",
                "MEDIA_EXECUTOR_ENABLED=false",
                "MEDIA_NET_EXECUTOR_ENABLED=false",
                "EGRESS_DENY_CIDRS=172.16.0.0/12",
                "EGRESS_DENY_IPS=127.0.0.1",
                "",
            ]
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    return path


def test_a_to_b_changes_only_revision_key(tmp_path: Path) -> None:
    env = _disposable_env(tmp_path / ".env")
    before = load_env_file(env)
    before_text = env.read_text(encoding="utf-8")
    replace_env_file_key(env, "FETCHNOW_RELEASE_REVISION", REV_B)
    after = load_env_file(env)
    assert after["FETCHNOW_RELEASE_REVISION"] == REV_B
    assert before["FETCHNOW_RELEASE_REVISION"] == REV_A
    for key in before:
        if key == "FETCHNOW_RELEASE_REVISION":
            continue
        assert after[key] == before[key]
    assert after["POSTGRES_PASSWORD"] == PASSWORD
    assert after["MEDIA_EXECUTOR_ENABLED"] == "false"
    assert after["MEDIA_NET_EXECUTOR_ENABLED"] == "false"
    assert after["EGRESS_DENY_CIDRS"] == "172.16.0.0/12"
    assert after["EGRESS_DENY_IPS"] == "127.0.0.1"
    assert "POSTGRES_PASSWORD=" + PASSWORD in env.read_text(encoding="utf-8")
    assert before_text.count("=") == env.read_text(encoding="utf-8").count("=")
    mode = stat.S_IMODE(env.stat().st_mode)
    assert mode == 0o600


def test_duplicate_and_missing_revision_fail_closed(tmp_path: Path) -> None:
    missing = tmp_path / "missing.env"
    missing.write_text("COMPOSE_PROJECT_NAME=x\n", encoding="utf-8")
    os.chmod(missing, 0o600)
    with pytest.raises(EnvFileError, match="missing env key"):
        replace_env_file_key(missing, "FETCHNOW_RELEASE_REVISION", REV_B)

    dup = tmp_path / "dup.env"
    dup.write_text(
        f"FETCHNOW_RELEASE_REVISION={REV_A}\nFETCHNOW_RELEASE_REVISION={REV_A}\n",
        encoding="utf-8",
    )
    os.chmod(dup, 0o600)
    with pytest.raises(EnvFileError, match="duplicate env key"):
        replace_env_file_key(dup, "FETCHNOW_RELEASE_REVISION", REV_B)


def test_prepare_receives_env_matching_expected_after_sync(tmp_path: Path) -> None:
    env = _disposable_env(tmp_path / ".env", revision=REV_A)
    replace_env_file_key(env, "FETCHNOW_RELEASE_REVISION", REV_B)
    env_revision = load_env_file(env)["FETCHNOW_RELEASE_REVISION"]
    expected_revision = REV_B
    assert env_revision == expected_revision
    # Real preflight comparison: matching pins must not raise the mismatch error.
    # Full preflight needs Docker; exercise the same compare used by production.
    from fetchnow_release.revision import validate_full_sha

    assert validate_full_sha(expected_revision) == validate_full_sha(env_revision)


def test_mismatch_still_rejected_by_real_preflight(tmp_path: Path) -> None:
    """Real run_preflight rejects env pin ≠ expected (no mocked compare)."""
    deploy = tmp_path / "deploy"
    backup = tmp_path / "backup"
    deploy.mkdir()
    backup.mkdir()
    env = tmp_path / ".env.staging"
    env.write_text(
        "\n".join(
            [
                "COMPOSE_PROJECT_NAME=fetchnow-staging",
                f"FETCHNOW_RELEASE_REVISION={REV_A}",
                "GATEWAY_PORT=127.0.0.1:8091",
                "PUBLIC_SITE_URL=https://example.test",
                "POSTGRES_DB=fetchnow",
                "POSTGRES_USER=fetchnow",
                f"POSTGRES_PASSWORD={PASSWORD}",
                f"FETCHNOW_DEPLOY_ROOT={deploy}",
                f"FETCHNOW_BACKUP_ROOT={backup}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(env, 0o600)
    compose = tmp_path / "compose.yaml"
    compose.write_text("services: {}\n", encoding="utf-8")
    # Intentionally leave env on REV_A while expecting REV_B.
    result = run_preflight(
        PreflightInput(
            project_name="fetchnow-staging",
            env_file=env,
            compose_files=(compose,),
            expected_revision=REV_B,
            repo_root=tmp_path,
        )
    )
    assert result.ok is False
    assert any("does not match" in m for m in result.messages)


def test_non_ok_prepare_result_preserves_domain_messages() -> None:
    snap = prepare_result_snapshot(
        requested_revision=REV_B,
        env_revision=REV_A,
        result=SimpleNamespace(
            ok=False,
            already_prepared=False,
            messages=(
                "FAIL: expected revision does not match FETCHNOW_RELEASE_REVISION "
                "in env file",
                "POSTGRES_PASSWORD=s3cret-should-redact",
            ),
            release_dir=Path("/tmp/releases") / REV_B,
        ),
    )
    assert snap["ok"] is False
    assert snap["requested_revision"] == REV_B
    assert snap["env_revision"] == REV_A
    assert snap["release_dir_name"] == REV_B
    assert any("expected revision does not match" in m for m in snap["messages"])
    assert "s3cret-should-redact" not in "\n".join(snap["messages"])
    assert snap["stage"] == "prepare"


def test_remaining_cases_stay_not_run_after_prepare_failure() -> None:
    recorded = [
        {"name": f"prepare-{REV_A}", "status": "PASS"},
        {"name": "source-only-off", "status": "PASS"},
        {"name": "off-to-on-rollback", "status": "PASS"},
        {"name": "same-source-activation", "status": "PASS"},
        {"name": "active-idempotency", "status": "PASS"},
        {"name": f"prepare-{REV_B}", "status": "FAIL"},
        {"name": "execution", "status": "FAIL"},
    ]
    extras = mark_remaining_cases_not_run(planned=MATRIX_CASE_NAMES, recorded=recorded)
    assert extras
    assert all(item["status"] == "NOT_RUN" for item in extras)
    names = {item["name"] for item in extras}
    assert "on-to-on-rollback" in names
    assert "on-to-on-commit" in names
    assert "accept-target-media-health" in names
