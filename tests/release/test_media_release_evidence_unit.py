"""Offline regressions for media release matrix diagnostic evidence."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fetchnow_release.media_release_evidence import (  # noqa: E402
    MATRIX_CASE_NAMES,
    RolloutLike,
    bounded_container_rows,
    evaluate_source_only_off,
    mark_remaining_cases_not_run,
    merge_primary_and_diagnostic_errors,
    prepare_result_snapshot,
    redact_messages,
    rollout_result_snapshot,
    safe_load_media_authority,
    write_json_evidence,
)
from fetchnow_release.media_state import MediaActivationState  # noqa: E402


def _inactive(**kwargs: object) -> MediaActivationState:
    base = {
        "schema_version": 1,
        "activation": False,
        "release_mode": "canonical",
        "revision": "a" * 40,
        "media_image_ids": {},
        "overlays": (),
        "profiles": (),
        "executor_settings": {
            "MEDIA_EXECUTOR_ENABLED": "false",
            "MEDIA_NET_EXECUTOR_ENABLED": "false",
        },
        "updated_at_utc": "2026-10-11T00:00:00Z",
    }
    base.update(kwargs)
    return MediaActivationState(**base)  # type: ignore[arg-type]


def test_prepare_result_snapshot_preserves_messages_before_assertion() -> None:
    from types import SimpleNamespace

    snap = prepare_result_snapshot(
        requested_revision="b" * 40,
        env_revision="a" * 40,
        result=SimpleNamespace(
            ok=False,
            already_prepared=False,
            messages=(
                "FAIL: expected revision does not match FETCHNOW_RELEASE_REVISION",
                "POSTGRES_PASSWORD=s3cret",
            ),
            release_dir=Path("/tmp/x") / ("b" * 40),
        ),
    )
    assert snap["ok"] is False
    assert snap["requested_revision"] == "b" * 40
    assert snap["env_revision"] == "a" * 40
    assert "does not match" in snap["messages"][0]
    assert "s3cret" not in snap["messages"][1]
    assert snap["release_dir_name"] == "b" * 40


def test_source_ok_false_preserves_messages_status_deployment_id() -> None:
    result = RolloutLike(
        ok=False,
        already_active=False,
        messages=("FAIL: target rollout error: boom", "POSTGRES_PASSWORD=s3cret"),
        deployment_id="12345678-1234-1234-1234-123456789abc",
        status="failed",
    )
    evaluation = evaluate_source_only_off(result, media={"exists": False})
    assert evaluation["ok"] is False
    assert evaluation["checks"]["rollout_ok"] is False
    assert "rollout_failed" in evaluation["failures"]
    snap = evaluation["rollout"]
    assert snap["status"] == "failed"
    assert snap["deployment_id"] == result.deployment_id
    assert snap["messages"][0].startswith("FAIL: target rollout error")
    assert "s3cret" not in snap["messages"][1]
    assert "POSTGRES_PASSWORD=<redacted>" in snap["messages"][1]


def test_rollout_pass_authority_missing_is_separate_failure() -> None:
    result = RolloutLike(
        ok=True,
        already_active=False,
        messages=("OK: rollout committed",),
        deployment_id="12345678-1234-1234-1234-123456789abc",
        status="committed",
    )
    evaluation = evaluate_source_only_off(result, media={"exists": False})
    assert evaluation["ok"] is False
    assert evaluation["checks"]["rollout_ok"] is True
    assert evaluation["checks"]["media_authority_present"] is False
    assert "media_authority_missing" in evaluation["failures"]
    assert "rollout_failed" not in evaluation["failures"]


def test_rollout_pass_activation_true_is_flags_off_failure() -> None:
    result = RolloutLike(
        ok=True,
        already_active=False,
        messages=("OK: rollout committed",),
        status="committed",
        deployment_id="12345678-1234-1234-1234-123456789abc",
    )
    media = {
        "exists": True,
        "activation": True,
        "release_mode": "media",
        "revision": "a" * 40,
    }
    evaluation = evaluate_source_only_off(result, media=media)
    assert evaluation["ok"] is False
    assert evaluation["checks"]["rollout_ok"] is True
    assert evaluation["checks"]["flags_off"] is False
    assert "media_activation_not_off" in evaluation["failures"]


def test_diagnostic_error_does_not_replace_primary_rollout_failure() -> None:
    primary = evaluate_source_only_off(
        RolloutLike(
            ok=False,
            already_active=False,
            messages=("FAIL: primary",),
            status="failed",
            deployment_id="12345678-1234-1234-1234-123456789abc",
        ),
        media={"exists": False},
    )
    merged = merge_primary_and_diagnostic_errors(
        primary=primary, diagnostic_error="OSError"
    )
    assert merged["ok"] is False
    assert "rollout_failed" in merged["failures"]
    assert merged["rollout"]["messages"] == ["FAIL: primary"]
    assert merged["diagnostic_error"] == "OSError"


def test_secrets_are_redacted_in_messages() -> None:
    messages = redact_messages(
        (
            "DATABASE_URL=postgresql://u:p@host/db",
            "token=abc https://user:pass@example.test/x?q=1",
        )
    )
    blob = "\n".join(messages)
    assert "postgresql://u:p@host/db" not in blob
    assert "user:pass" not in blob
    assert (
        "token=abc" not in blob
        or "TOKEN=<redacted>" in blob.upper()
        or "<redacted>" in blob
    )


def test_evidence_written_before_cleanup_and_cleanup_error_keeps_primary(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    primary = {
        "stage": "source-only-off",
        "ok": False,
        "rollout": rollout_result_snapshot(
            RolloutLike(
                ok=False,
                already_active=False,
                messages=("FAIL: keep me POSTGRES_PASSWORD=s3cret",),
                status="failed",
                deployment_id="12345678-1234-1234-1234-123456789abc",
            )
        ),
        "failures": ["rollout_failed"],
    }
    write_json_evidence(evidence / "primary_failure.json", primary)
    # Simulate cleanup failure after evidence persistence.
    cleanup_error = "RuntimeError"
    result_payload = {
        "status": "FAIL",
        "cases": [
            {"name": "source-only-off", "status": "FAIL"},
            {"name": "scoped-cleanup", "status": "FAIL", "error_type": cleanup_error},
        ],
        "primary_failure_present": True,
    }
    write_json_evidence(evidence / "result.json", result_payload)
    loaded = json.loads((evidence / "primary_failure.json").read_text(encoding="utf-8"))
    assert loaded["ok"] is False
    assert "s3cret" not in loaded["rollout"]["messages"][0]
    assert "FAIL: keep me" in loaded["rollout"]["messages"][0]
    assert result_payload["status"] == "FAIL"


def test_remaining_cases_after_stop_are_not_run_not_pass() -> None:
    recorded = [
        {"name": "prepare-aaa", "status": "PASS"},
        {"name": "source-only-off", "status": "FAIL"},
        {"name": "execution", "status": "FAIL"},
    ]
    extras = mark_remaining_cases_not_run(planned=MATRIX_CASE_NAMES, recorded=recorded)
    assert extras
    assert all(item["status"] == "NOT_RUN" for item in extras)
    assert all(item["status"] != "PASS" for item in extras)
    names = {item["name"] for item in extras}
    assert "off-to-on-rollback" in names
    assert "source-only-off" not in names


def test_safe_load_media_authority_error_does_not_raise() -> None:
    def boom() -> None:
        raise RuntimeError("POSTGRES_PASSWORD=s3cret")

    snap = safe_load_media_authority(boom)
    assert snap["exists"] is None
    assert snap["load_error"] == "RuntimeError"
    assert "s3cret" not in snap["load_detail"]


def test_bounded_container_rows_omit_env() -> None:
    rows = bounded_container_rows(
        [
            {
                "Config": {
                    "Labels": {"com.docker.compose.service": "api"},
                    "Env": ["POSTGRES_PASSWORD=s3cret"],
                },
                "Image": "sha256:" + "a" * 64,
                "State": {
                    "Running": True,
                    "ExitCode": 0,
                    "Health": {"Status": "healthy"},
                },
            }
        ]
    )
    assert rows[0]["service"] == "api"
    assert "Env" not in rows[0]
    assert "s3cret" not in json.dumps(rows)
