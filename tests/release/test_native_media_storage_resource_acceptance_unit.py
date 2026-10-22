"""Offline regressions for native media storage/resource acceptance gate."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "native_media_storage_resource_acceptance.py"
HARNESS = ROOT / "scripts" / "native_media_storage_acceptance_harness.sh"
WORKFLOW = (
    ROOT
    / ".github"
    / "workflows"
    / "sec08-09-media-storage-resource-acceptance.yml"
)


def _load_mod():
    spec = importlib.util.spec_from_file_location(
        "native_media_storage_resource_acceptance", SCRIPT
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_default_guard_exits_78_without_execute(tmp_path):
    env = os.environ.copy()
    env.pop("FETCHNOW_NATIVE_MEDIA_STORAGE_RESOURCE_ACCEPTANCE_EXECUTE", None)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--evidence-dir", str(tmp_path / "ev")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 78
    assert "prepared-only" in proc.stderr
    assert not (tmp_path / "ev").exists()


def test_filesystem_harness_default_still_exits_78():
    env = os.environ.copy()
    env.pop("FETCHNOW_NATIVE_MEDIA_STORAGE_HARNESS_EXECUTE", None)
    proc = subprocess.run(
        ["bash", str(HARNESS)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 78
    assert "prepared-only" in proc.stderr


def test_overall_status_requires_all_pass():
    mod = _load_mod()
    cases = [
        mod.CaseResult(name=n, status="PASS") for n in mod.REQUIRED_CASES[:-1]
    ]
    cases.append(mod.CaseResult(name=mod.REQUIRED_CASES[-1], status="BLOCKED"))
    assert mod.overall_status(cases) == "BLOCKED"
    cases[-1] = mod.CaseResult(name=mod.REQUIRED_CASES[-1], status="NOT_RUN")
    assert mod.overall_status(cases) == "NOT_RUN"
    cases[-1] = mod.CaseResult(name=mod.REQUIRED_CASES[-1], status="FAIL")
    assert mod.overall_status(cases) == "FAIL"
    cases = [mod.CaseResult(name=n, status="PASS") for n in mod.REQUIRED_CASES]
    assert mod.overall_status(cases) == "PASS"


def test_mark_remaining_and_completeness():
    mod = _load_mod()
    recorded = [mod.CaseResult(name="S1_environment_identity", status="PASS")]
    rest = mod.mark_remaining_not_run(
        mod.REQUIRED_CASES, recorded, reason="stopped"
    )
    names = {c.name for c in recorded + rest}
    assert names == set(mod.REQUIRED_CASES)
    assert all(c.status == "NOT_RUN" for c in rest)
    blocked = mod.mark_remaining_blocked(
        mod.REQUIRED_CASES, recorded, reason="disk"
    )
    assert all(c.status == "BLOCKED" for c in blocked)


def test_no_fake_pass_when_required_missing():
    mod = _load_mod()
    # Empty / partial must never be PASS.
    assert mod.overall_status([]) == "FAIL"
    assert (
        mod.overall_status(
            [mod.CaseResult(name="S1_environment_identity", status="PASS")]
        )
        == "FAIL"
    )


def test_cleanup_refuses_wrong_workdir_identity(tmp_path, monkeypatch):
    mod = _load_mod()
    evidence = tmp_path / "ev"
    evidence.mkdir()
    orch = mod.AcceptanceOrchestrator(evidence)
    # Pretend cleanup sees nested mount remaining.
    orch.work = tmp_path / "work"
    orch.work.mkdir()
    orch.cleanup_ok = True

    def fake_run(argv, **kwargs):
        class R:
            returncode = 0
            stdout = str(orch.work) + "/mnt\n"
            stderr = ""

        if argv[:2] == ["findmnt", "-rn"]:
            return R()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(mod, "run", fake_run)
    orch.cleanup()
    assert orch.cleanup_ok is False
    assert "NO recursive deletion" in orch.cleanup_detail
    s9 = next(c for c in orch.cases if c.name == "S9_cleanup")
    assert s9.status == "FAIL"
    assert orch.work.exists()


def test_workflow_is_dispatch_only_no_push_trigger():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in text
    assert "\n  push:" not in text
    assert "pull_request:" not in text
    assert "\n    continue-on-error:" not in text
    assert "\n      continue-on-error:" not in text
    assert "|| true" not in text
    assert "FETCHNOW_NATIVE_MEDIA_STORAGE_RESOURCE_ACCEPTANCE_EXECUTE=1" in text
    assert "native_media_storage_acceptance_harness.sh" not in text
    assert "expected_sha:" in text
    assert "fetch-depth: 0" in text


def test_workflow_shell_syntax():
    # Extract indented run: | blocks and bash -n each.
    text = WORKFLOW.read_text(encoding="utf-8")
    blocks: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].rstrip().endswith("run: |"):
            i += 1
            chunk: list[str] = []
            while i < len(lines) and (
                lines[i].startswith("          ") or lines[i].strip() == ""
            ):
                chunk.append(lines[i][10:] if lines[i].startswith("          ") else "")
                i += 1
            blocks.append("\n".join(chunk))
            continue
        i += 1
    assert blocks, "expected at least one run block"
    for idx, script in enumerate(blocks):
        path = ROOT / f".tmp-workflow-step-{idx}.sh"
        path.write_text("#!/bin/bash\n" + script + "\n", encoding="utf-8")
        try:
            check = subprocess.run(
                ["bash", "-n", str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
            assert check.returncode == 0, f"block {idx}: {check.stderr}"
        finally:
            path.unlink(missing_ok=True)


def test_orchestrator_shell_syntax():
    for path in (SCRIPT, HARNESS, ROOT / "scripts" / "native_media_storage_resource_release_path.py"):
        if path.suffix == ".sh":
            proc = subprocess.run(
                ["bash", "-n", str(path)], capture_output=True, text=True, check=False
            )
            assert proc.returncode == 0, proc.stderr
        else:
            proc = subprocess.run(
                [sys.executable, "-m", "py_compile", str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
            assert proc.returncode == 0, proc.stderr


def test_required_cases_cover_s1_s9():
    mod = _load_mod()
    joined = " ".join(mod.REQUIRED_CASES)
    for token in ("S1_", "S2_", "S3_", "S4_", "S5_", "S6_", "S7_", "S8_", "S9_"):
        assert token in joined
    assert mod.EXECUTE_ENV.endswith("EXECUTE")
