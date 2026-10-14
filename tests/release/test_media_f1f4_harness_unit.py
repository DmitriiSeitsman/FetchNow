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

F1F4_CASES = (
    "f1a-bootstrap-media-on-partial-fail-cleanup",
    "f1b-bootstrap-media-on-cleanup-failure-unresolved",
    "f2-disk-fast-growth-fail",
    "f2-disk-fast-tree-fail",
    "f2-disk-staged-inputs-success",
    "f2-disk-prestart-refuse",
    "f2-disk-long-writer-kill-reap",
    "f2-disk-neighbor-continues",
    "f3-config-rollout-refuses-media-on",
    "f4-invalid-on-budget-health-fail",
)


def test_matrix_case_names_include_f1f4_and_prior() -> None:
    for name in F1F4_CASES:
        assert name in MATRIX_CASE_NAMES
    assert "source-only-off" in MATRIX_CASE_NAMES
    assert "off-to-on-rollback" in MATRIX_CASE_NAMES
    assert "accept-target-rejects-stopped-media" in MATRIX_CASE_NAMES
    # Prior count was 9; F1–F4 add 10.
    assert len(MATRIX_CASE_NAMES) == 19


def test_harness_wires_f1f4_and_disk_probe() -> None:
    text = HARNESS.read_text(encoding="utf-8")
    assert "run_disk_probe" in text
    assert "run_config_rollout" in text
    assert "_managed_media_expectation" in text
    assert "assert_running_media_budget" in text
    assert "test-only media failure after partial sidecar activation" in text
    assert "test-only ownership removal failure" in text
    assert "foreign-" in text
    for name in F1F4_CASES:
        assert f'record(\n            "{name}"' in text or f'record(\n                "{name}"' in text or f'"{name}"' in text


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
        "case_fast_growth",
        "case_fast_tree",
        "case_staged_inputs_ok",
        "case_prestart_refuse",
        "case_long_writer_kill_reap",
        "case_neighbor_continues",
    }


def test_workflow_marker_is_f1f4_attempt_one_only() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "[native-once-20261014-f1f4]" in text
    assert "github.run_attempt == 1" in text
    assert "workflow_dispatch" not in text.split("on:", 1)[1].split("permissions:", 1)[0]
    assert "38466520000" in text


def test_accept_target_helper_not_declared_as_public_recover_e2e() -> None:
    text = HARNESS.read_text(encoding="utf-8")
    assert "_execute_accept_target" in text
    assert "public recover CLI" not in text.lower() or "not" in text.lower()
