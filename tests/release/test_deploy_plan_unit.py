"""Unit tests for PRD1C3B1 migration compatibility and deploy planning."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fetchnow_release.c3b1_constants import COMPATIBILITY_SCHEMA_VERSION  # noqa: E402
from fetchnow_release.cli import build_parser  # noqa: E402
from fetchnow_release.deploy_plan import (  # noqa: E402
    DeployPlanInput,
    _canonical_plan,
    run_deploy_plan,
)
from fetchnow_release.migration_compatibility import (  # noqa: E402
    CompatibilityError,
    find_matching_transition,
    load_compatibility_contract,
    parse_compatibility_contract,
)
from fetchnow_release.readonly_guard import (  # noqa: E402
    ReadonlySubprocessError,
    assert_readonly_subprocess,
)
from fetchnow_release.redact import redact  # noqa: E402
from fetchnow_pg_backup.alembic_graph import build_alembic_graph  # noqa: E402


def _valid_transition(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "from_heads": ["0001_baseline"],
        "to_heads": ["0002_expand"],
        "included_revisions": ["0002_expand"],
        "execution_mode": "online_expand",
        "online_with_previous_application": True,
        "previous_application_rollback_compatible": True,
        "data_loss": "none",
        "database_downgrade": "forbidden",
        "rationale": "Add nullable column for online expand rollout.",
    }
    base.update(overrides)
    return base


def _contract(*transitions: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": COMPATIBILITY_SCHEMA_VERSION,
        "transitions": list(transitions),
    }


def test_empty_valid_policy() -> None:
    contract = parse_compatibility_contract(_contract())
    assert contract.transitions == ()


def test_repository_contract_covers_sequential_alembic_transitions() -> None:
    """Compatibility JSON must cover each forward step of the real Alembic graph.

    Do not assume a single leap from baseline to current head: when heads move
    to 0003, the contract must still include 0001→0002 and 0002→0003.
    """
    graph = build_alembic_graph(ROOT / "backend" / "migrations" / "versions")
    contract = load_compatibility_contract(
        ROOT / "deploy" / "migrations" / "compatibility.json"
    )
    assert graph.heads == ("0004_download_observability",)

    steps = (
        (
            frozenset({"0001_baseline"}),
            frozenset({"0002_media_jobs"}),
            frozenset({"0002_media_jobs"}),
        ),
        (
            frozenset({"0002_media_jobs"}),
            frozenset({"0003_download_jobs"}),
            frozenset({"0003_download_jobs"}),
        ),
        (
            frozenset({"0003_download_jobs"}),
            frozenset({"0004_download_observability"}),
            frozenset({"0004_download_observability"}),
        ),
    )
    for from_heads, to_heads, included in steps:
        # Sanity: included set matches Alembic forward delta for this step.
        assert graph.included_revisions(from_heads, to_heads) == included, (
            from_heads,
            to_heads,
        )
        transition = find_matching_transition(
            contract,
            from_heads=from_heads,
            to_heads=to_heads,
            included_revisions=included,
        )
        assert frozenset(transition.to_heads) == to_heads
        assert frozenset(transition.included_revisions) == included

    # Full baseline→head still must not be assumed as a single transition entry.
    full_included = graph.included_revisions(
        frozenset({"0001_baseline"}), frozenset(graph.heads)
    )
    assert full_included == frozenset(
        {"0002_media_jobs", "0003_download_jobs", "0004_download_observability"}
    )
    with pytest.raises(CompatibilityError, match="no compatibility transition"):
        find_matching_transition(
            contract,
            from_heads=frozenset({"0001_baseline"}),
            to_heads=frozenset(graph.heads),
            included_revisions=full_included,
        )


def test_unknown_top_level_field_rejected() -> None:
    raw = _contract()
    raw["extra"] = True
    with pytest.raises(CompatibilityError, match="unknown fields"):
        parse_compatibility_contract(raw)


def test_missing_transition_field_rejected() -> None:
    transition = _valid_transition()
    del transition["rationale"]
    with pytest.raises(CompatibilityError, match="missing fields"):
        parse_compatibility_contract(_contract(transition))


def test_duplicate_transition_rejected() -> None:
    transition = _valid_transition()
    with pytest.raises(CompatibilityError, match="duplicate transition"):
        parse_compatibility_contract(_contract(transition, transition))


def test_unsorted_revision_ids_rejected() -> None:
    transition = _valid_transition(from_heads=["0002_expand", "0001_baseline"])
    with pytest.raises(CompatibilityError, match="sorted"):
        parse_compatibility_contract(_contract(transition))


def test_placeholder_rationale_rejected() -> None:
    transition = _valid_transition(rationale="TODO add real rationale here")
    with pytest.raises(CompatibilityError, match="placeholder"):
        parse_compatibility_contract(_contract(transition))


def test_unsafe_policy_values_rejected() -> None:
    for key, value in (
        ("execution_mode", "offline"),
        ("online_with_previous_application", False),
        ("previous_application_rollback_compatible", False),
        ("data_loss", "possible"),
        ("database_downgrade", "allowed"),
    ):
        transition = _valid_transition(**{key: value})
        with pytest.raises(CompatibilityError):
            parse_compatibility_contract(_contract(transition))


def test_find_matching_transition_exact() -> None:
    contract = parse_compatibility_contract(_contract(_valid_transition()))
    match = find_matching_transition(
        contract,
        from_heads=frozenset({"0001_baseline"}),
        to_heads=frozenset({"0002_expand"}),
        included_revisions=frozenset({"0002_expand"}),
    )
    assert match.included_revisions == ("0002_expand",)


def test_wrong_from_heads_no_match() -> None:
    contract = parse_compatibility_contract(_contract(_valid_transition()))
    with pytest.raises(CompatibilityError, match="no compatibility transition"):
        find_matching_transition(
            contract,
            from_heads=frozenset({"0009_wrong"}),
            to_heads=frozenset({"0002_expand"}),
            included_revisions=frozenset({"0002_expand"}),
        )


def test_canonical_plan_is_deterministic() -> None:
    payload = {
        "schema_version": 1,
        "project": "fetchnow-staging",
        "current_revision": "a" * 40,
        "target_revision": "b" * 40,
        "current_db_heads": ["0001_baseline"],
        "target_db_heads": ["0001_baseline"],
        "migration_required": False,
        "included_revisions": [],
        "compatibility_contract_sha256": "c" * 64,
        "verified_backup_required": False,
        "previous_application_rollback_allowed": True,
        "database_downgrade_allowed": False,
        "application_rollout_required": False,
    }
    first = _canonical_plan(payload)
    second = _canonical_plan(payload)
    assert first == second
    assert first.endswith("\n")
    assert "POSTGRES_PASSWORD" not in first
    assert '"application_rollout_required"' in _canonical_plan(
        {**payload, "application_rollout_required": True}
    )


def test_redaction_removes_secrets() -> None:
    text = "DATABASE_URL=postgresql://u:secret@host/db POSTGRES_PASSWORD=hunter2"
    out = redact(text)
    assert "hunter2" not in out
    assert "secret@" not in out


def test_forbidden_subprocess_commands() -> None:
    for argv in (
        ["pg_dump", "db"],
        ["docker", "compose", "up", "-d"],
        ["docker", "compose", "run", "api", "alembic", "upgrade", "head"],
        ["docker", "rm", "-f", "container"],
    ):
        with pytest.raises(ReadonlySubprocessError):
            assert_readonly_subprocess(argv)


def test_readonly_psql_select_allowed() -> None:
    assert_readonly_subprocess(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "sh",
            "-c",
            'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT 1"',
        ]
    )


def test_cli_has_no_unsafe_bypass_for_deploy_plan() -> None:
    help_text = build_parser().format_help()
    for banned in (
        "skip-health",
        "allow-dirty",
        "skip-preflight",
        "skip-db-check",
        "test-mode",
        "allow-unsafe",
        "force-migration",
    ):
        assert banned not in help_text
    assert "deploy-plan" in help_text


def test_missing_current_state_blocks_planning(tmp_path: Path) -> None:
    result = run_deploy_plan(
        DeployPlanInput(
            project_name="fetchnow-deploy-plan-test-abc12345",
            env_file=tmp_path / "missing.env",
            compose_files=(tmp_path / "compose.yaml",),
            expected_revision="a" * 40,
            repo_root=ROOT,
            deploy_root=tmp_path / "deploy",
        )
    )
    assert not result.ok


def test_load_compatibility_contract_missing_file(tmp_path: Path) -> None:
    with pytest.raises(CompatibilityError, match="not found"):
        load_compatibility_contract(tmp_path / "missing.json")


def test_duplicate_json_keys_rejected(tmp_path: Path) -> None:
    path = tmp_path / "compatibility.json"
    path.write_text('{"schema_version": 1, "schema_version": 2, "transitions": []}\n')
    with pytest.raises(CompatibilityError, match="duplicate JSON object keys"):
        load_compatibility_contract(path)


def test_schema_version_bool_rejected() -> None:
    with pytest.raises(CompatibilityError, match="schema_version must be integer"):
        parse_compatibility_contract({"schema_version": True, "transitions": []})


def test_schema_version_string_rejected() -> None:
    with pytest.raises(CompatibilityError, match="schema_version must be integer"):
        parse_compatibility_contract({"schema_version": "1", "transitions": []})


def test_bool_one_rejected_for_online_with_previous() -> None:
    transition = _valid_transition(online_with_previous_application=1)
    with pytest.raises(CompatibilityError, match="must be true"):
        parse_compatibility_contract(_contract(transition))


def test_deploy_plan_subprocess_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Record subprocess argv during planner DB-head query path."""
    import subprocess

    recorded: list[list[str]] = []

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        recorded.append(list(argv))

        class Proc:
            returncode = 0
            stdout = "0001_baseline\n"
            stderr = ""

        return Proc()

    monkeypatch.setattr(subprocess, "run", fake_run)
    from fetchnow_release import db_heads

    heads = db_heads.database_heads_via_postgres(
        project_name="fetchnow-staging",
        env_file=Path("/tmp/.env"),
        compose_files=(Path("/tmp/compose.yaml"),),
        cwd=Path("/tmp"),
    )
    assert heads == frozenset({"0001_baseline"})
    assert len(recorded) == 1
    argv = recorded[0]
    assert argv[0:2] == ["docker", "compose"]
    assert "exec" in argv
    assert "postgres" in argv
    joined = " ".join(argv).lower()
    assert "select version_num from alembic_version" in joined
    assert "upgrade" not in joined
    assert "insert" not in joined
    assert "postgres_password" not in joined


def test_wrong_included_revisions_no_match() -> None:
    contract = parse_compatibility_contract(
        _contract(_valid_transition(included_revisions=["0003_other"]))
    )
    with pytest.raises(CompatibilityError, match="no compatibility transition"):
        find_matching_transition(
            contract,
            from_heads=frozenset({"0001_baseline"}),
            to_heads=frozenset({"0002_expand"}),
            included_revisions=frozenset({"0002_expand"}),
        )
