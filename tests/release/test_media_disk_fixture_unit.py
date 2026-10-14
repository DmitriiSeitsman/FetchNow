"""Offline regressions for F2 disk fixture DAC + verdict criteria."""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
DISK_INSIDE = ROOT / "backend" / "tests" / "media_executor" / "disk_native_inside.py"
PROBE = ROOT / "scripts" / "release_media_disk_native_probe.py"
DAC_SMOKE = ROOT / "scripts" / "release_media_disk_dac_smoke.py"
HARNESS = ROOT / "scripts" / "release_media_activation_integration_test.py"
WORKFLOW = ROOT / ".github" / "workflows" / "sec09-release-media-activation.yml"

# Import fixture helpers without pulling Landlock at import time for pure fns.
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "backend" / "tests" / "media_executor"))

spec = importlib.util.spec_from_file_location("disk_native_inside", DISK_INSIDE)
assert spec and spec.loader
disk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(disk)


def test_fixture_uses_production_layout_helpers_not_chmod_0777() -> None:
    text = DISK_INSIDE.read_text(encoding="utf-8")
    assert "make_work_root" in text
    assert "make_job_directory" in text
    assert "JOB_DIR_MODE" in text
    assert "TOOL_UID" in text
    assert "WORKER_GID" in text
    assert "chmod(0o777" not in text
    assert "never chmod 0777" in text.lower() or "never root-owned Path.mkdir" in text
    # Attempt creation must not rely on bare Path.mkdir as the only DAC setup.
    assert "make_attempt" in text
    assert "case_positive_control" in text
    assert "make_job_directory" in text


def test_positive_control_gates_negative_cases_in_source() -> None:
    text = DISK_INSIDE.read_text(encoding="utf-8")
    assert "positive_control_failed" in text
    assert "positive_control_gate" in text
    tree = ast.parse(text)
    names = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("case_")
    }
    assert "case_positive_control" in names
    assert "case_fast_growth" in names


def test_exit_one_zero_writes_is_not_disk_pass() -> None:
    outcome = SimpleNamespace(
        exit_code=1, timed_out=False, cancelled=False, stdout=b"", stderr=b""
    )
    klass = disk.classify_tool_failure(
        outcome=outcome, bytes_written=0, expected_min_write=8192, raised=None
    )
    assert klass == "dac_or_no_write"
    evidence = {
        "ok_disk": False,
        "failure_class": klass,
        "writer_started": False,
        "size": 0,
        "expected_size": 8192,
        "exit_code": 1,
        "timed_out": False,
    }
    assert disk.evaluate_fast_growth(evidence) is False


def test_launcher_or_dac_error_is_not_disk_pass() -> None:
    assert (
        disk.classify_tool_failure(
            outcome=None,
            bytes_written=0,
            expected_min_write=1,
            raised=OSError("Permission denied"),
        )
        == "dac_error"
    )
    assert (
        disk.classify_tool_failure(
            outcome=None,
            bytes_written=0,
            expected_min_write=1,
            raised=OSError("landlock abi below floor"),
        )
        == "launcher_error"
    )


def test_disk_limit_requires_expected_bytes_and_exit_one() -> None:
    outcome = SimpleNamespace(
        exit_code=1, timed_out=False, cancelled=False, stdout=b"", stderr=b""
    )
    klass = disk.classify_tool_failure(
        outcome=outcome, bytes_written=8192, expected_min_write=8192, raised=None
    )
    assert klass == "disk_limit"
    evidence = {
        "ok_disk": True,
        "failure_class": "disk_limit",
        "writer_started": True,
        "size": 8192,
        "expected_size": 8192,
        "exit_code": 1,
        "timed_out": False,
    }
    assert disk.evaluate_fast_growth(evidence) is True
    evidence["size"] = 0
    evidence["writer_started"] = False
    assert disk.evaluate_fast_growth(evidence) is False


def test_positive_control_evaluator_requires_layout_and_launcher_zero() -> None:
    evidence = {
        "layout_ok": True,
        "tool_marker_ok": True,
        "controller_read_ok": True,
        "launcher_exit_code": 0,
        "timed_out": False,
        "failure_class": "success",
    }
    assert disk.evaluate_positive_control(evidence) is True
    evidence["launcher_exit_code"] = 1
    evidence["failure_class"] = "disk_limit"
    assert disk.evaluate_positive_control(evidence) is False


def test_evaluate_long_writer_requires_heartbeat_and_stop() -> None:
    evidence = {
        "failure_class": "disk_limit",
        "writer_started": True,
        "exit_code": 1,
        "timed_out": False,
        "size": 8000,
        "heartbeat": 3,
        "recording_stopped": True,
        "elapsed": 1.2,
    }
    assert disk.evaluate_long_writer(evidence) is True
    evidence["heartbeat"] = 0
    evidence["writer_started"] = False
    assert disk.evaluate_long_writer(evidence) is False
    evidence["heartbeat"] = 3
    evidence["writer_started"] = True
    evidence["failure_class"] = "dac_or_no_write"
    assert disk.evaluate_long_writer(evidence) is False


def test_sanitize_strips_urls_and_secrets() -> None:
    text = disk._sanitize_text(
        b"fail password=sekrit https://evil.example/x Bearer abcdef"
    )
    assert "password=sekrit" not in text
    assert "https://evil.example/x" not in text
    assert "<redacted>" in text


def test_probe_cleanup_checks_return_codes() -> None:
    text = PROBE.read_text(encoding="utf-8")
    assert "_bounded_cleanup" in text
    assert "cleanup_ok" in text
    assert "probe_cleanup_failed" in text
    assert "ignore_errors=True" not in text or "rmtree(work)" in text


def test_harness_records_positive_control_before_negatives() -> None:
    text = HARNESS.read_text(encoding="utf-8")
    assert "f2-disk-layout-prepare" in text
    assert "f2-disk-positive-control" in text
    assert "positive_control_gate" in text
    pos = text.find('"f2-disk-positive-control"')
    growth = text.find('"f2-disk-fast-growth-fail"')
    assert 0 < pos < growth


def test_dac_smoke_script_is_not_landlock_acceptance() -> None:
    text = DAC_SMOKE.read_text(encoding="utf-8")
    assert "dac_smoke" in text
    assert "native_landlock_cgroup_acceptance" in text
    assert "SETUID" in text and "SETGID" in text and "SETPCAP" in text
    assert "no-new-privileges" in text
    assert "make_job_directory" in text
    assert "writable-cgroups" not in text


def test_workflow_marker_nbhs() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "[native-once-20261014-nbhs]" in text
    assert "[native-once-20261014-cwdabs]" not in text
    assert "[native-once-20261014-diskdac]" not in text
    assert "[native-once-20261014-sec09iso]" not in text
