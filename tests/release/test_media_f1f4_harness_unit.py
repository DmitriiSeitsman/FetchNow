"""Offline regressions for F1–F4 native harness wiring (not native proof)."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from fetchnow_release.media_release_evidence import MATRIX_CASE_NAMES  # noqa: E402

HARNESS = ROOT / "scripts" / "release_media_activation_integration_test.py"
DISK_PROBE = ROOT / "scripts" / "release_media_disk_native_probe.py"
DISK_INSIDE = ROOT / "backend" / "tests" / "media_executor" / "disk_native_inside.py"
WORKFLOW = ROOT / ".github" / "workflows" / "sec09-release-media-activation.yml"
CONFIG_ROLLOUT = ROOT / "scripts" / "fetchnow_release" / "config_rollout.py"

F1F4_CASES = (
    "f3-f4-prerequisites",
    "f3-config-rollout-refuses-media-on",
    "f4-invalid-on-budget-health-fail",
    "f2-disk-layout-prepare",
    "f2-disk-positive-control",
    "f2-disk-fast-growth-fail",
    "f2-disk-fast-tree-fail",
    "f2-disk-staged-inputs-success",
    "f2-disk-prestart-refuse",
    "f2-disk-long-writer-kill-reap",
    "f2-disk-neighbor-continues",
    "f1a-bootstrap-media-on-partial-fail-cleanup",
    "f1b-bootstrap-media-on-cleanup-failure-unresolved",
)

MARKER = "[native-once-20261014-cwdabs]"


def _harness_text() -> str:
    return HARNESS.read_text(encoding="utf-8")


def _case_anchor(text: str, name: str) -> int:
    needle = f'"{name}"'
    idx = text.find(needle)
    assert idx >= 0, name
    return idx


def test_matrix_case_names_include_f1f4_and_prior() -> None:
    for name in F1F4_CASES:
        assert name in MATRIX_CASE_NAMES
    assert "source-only-off" in MATRIX_CASE_NAMES
    assert "off-to-on-rollback" in MATRIX_CASE_NAMES
    assert "accept-target-rejects-stopped-media" in MATRIX_CASE_NAMES
    # Prior matrix (9) + F1–F4 (10) + prerequisites (1) + F2 gates (2) = 22.
    assert len(MATRIX_CASE_NAMES) == 22
    # Execution order in MATRIX_CASE_NAMES: F3→F4→F2→F1 before accept-target.
    assert MATRIX_CASE_NAMES.index("f3-f4-prerequisites") < MATRIX_CASE_NAMES.index(
        "f3-config-rollout-refuses-media-on"
    )
    assert MATRIX_CASE_NAMES.index("f3-config-rollout-refuses-media-on") < (
        MATRIX_CASE_NAMES.index("f4-invalid-on-budget-health-fail")
    )
    assert MATRIX_CASE_NAMES.index("f4-invalid-on-budget-health-fail") < (
        MATRIX_CASE_NAMES.index("f2-disk-layout-prepare")
    )
    assert MATRIX_CASE_NAMES.index("f2-disk-positive-control") < (
        MATRIX_CASE_NAMES.index("f2-disk-fast-growth-fail")
    )
    assert MATRIX_CASE_NAMES.index(
        "f1b-bootstrap-media-on-cleanup-failure-unresolved"
    ) < (MATRIX_CASE_NAMES.index("accept-target-media-health"))


def test_harness_runs_f3_f4_before_synthetic_recovery_plan() -> None:
    text = _harness_text()
    write_plan_idx = text.find("write_plan(recovery_dir, recovery_plan)")
    assert write_plan_idx > 0
    assert _case_anchor(text, "f3-config-rollout-refuses-media-on") < write_plan_idx
    assert _case_anchor(text, "f4-invalid-on-budget-health-fail") < write_plan_idx
    assert _case_anchor(text, "f2-disk-fast-growth-fail") < write_plan_idx
    assert (
        _case_anchor(text, "f1a-bootstrap-media-on-partial-fail-cleanup")
        < write_plan_idx
    )
    assert write_plan_idx < _case_anchor(text, "accept-target-media-health")
    # Stopped-proxy negative case must not precede healthy media F3 prerequisites.
    stop_proxy = text.find('checked(["docker", "stop", proxy["Id"]], ROOT)')
    assert stop_proxy > 0
    assert _case_anchor(text, "f3-f4-prerequisites") < stop_proxy
    assert _case_anchor(text, "f3-config-rollout-refuses-media-on") < stop_proxy


def test_harness_wires_f1f4_and_managed_health() -> None:
    text = _harness_text()
    assert "run_disk_probe" in text
    assert "run_config_rollout" in text
    assert "run_health(" in text
    assert "managed_health_input" in text
    assert "_managed_media_expectation" not in text
    assert "assert_running_media_budget" in text
    assert "test-only media failure after partial sidecar activation" in text
    assert "test-only ownership removal failure" in text
    assert "foreign-" in text
    assert "find_unresolved_deployments" in text
    assert "compose_for_media_plan" in text
    assert "media activation is ON" in text
    assert "resource_budget" in text
    assert "media_path.write_bytes(original_media)" in text
    for name in F1F4_CASES:
        assert f'"{name}"' in text


def test_unresolved_journal_guard_still_blocks_config_rollout() -> None:
    text = CONFIG_ROLLOUT.read_text(encoding="utf-8")
    assert "unresolved release journal(s) block config rollout" in text
    assert "find_unresolved_deployments" in text
    assert "media activation is ON" in text
    # Harness must not disable the unresolved guard to pass F3.
    harness = _harness_text()
    assert "continue-on-error" not in harness
    assert "find_unresolved_deployments = " not in harness
    assert "unresolved release journal" in harness


def test_f1_f2_entrypoints_remain_isolated_projects() -> None:
    text = _harness_text()
    # F1 uses a separate disposable project/deploy root.
    assert "deploy-f1" in text
    assert "make_rollout_project_name()" in text
    # F2 probe is a separate disposable container, not the main project stack.
    assert "run_disk_probe" in text
    assert "sec08-launch" in DISK_PROBE.read_text(encoding="utf-8")


def test_disk_probe_does_not_replace_linux_primitives() -> None:
    text = DISK_PROBE.read_text(encoding="utf-8")
    inside = DISK_INSIDE.read_text(encoding="utf-8")
    assert "LandlockRunner" in inside
    assert "prctl" not in text.lower() or "Does NOT replace prctl" in text
    assert "write_cgroup_kill" not in text
    assert "measure_tree_bytes" not in text
    assert "trusted" in text.lower()
    assert "sec08-launch" in text
    assert "--cgroupns" in text and "private" in text
    assert "writable-cgroups=true" in text
    assert "SETUID" in text and "SETGID" in text and "SETPCAP" in text


def test_disk_inside_cases_cover_required_scenarios() -> None:
    tree = ast.parse(DISK_INSIDE.read_text(encoding="utf-8"))
    names = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("case_")
    }
    assert names >= {
        "case_positive_control",
        "case_fast_growth",
        "case_fast_tree",
        "case_staged_inputs_ok",
        "case_prestart_refuse",
        "case_long_writer_kill_reap",
        "case_neighbor_continues",
    }
    text = DISK_INSIDE.read_text(encoding="utf-8")
    assert "make_job_directory" in text
    assert "make_work_root" in text


def test_workflow_marker_is_cwdabs_attempt_one_only() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert MARKER in text
    assert "github.run_attempt == 1" in text
    assert (
        "workflow_dispatch" not in text.split("on:", 1)[1].split("permissions:", 1)[0]
    )
    assert "38618740901" in text
    assert "[native-once-20261014-diskdac]" not in text
    assert "[native-once-20261014-sec09iso]" not in text
    assert "[native-once-20261014-f1f4]" not in text


def test_accept_target_helper_not_declared_as_public_recover_e2e() -> None:
    text = _harness_text()
    assert "_execute_accept_target" in text
    assert "public recover CLI" not in text.lower() or "not" in text.lower()
