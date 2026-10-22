"""Offline regressions for native media storage/resource acceptance gate."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "native_media_storage_resource_acceptance.py"
HARNESS = ROOT / "scripts" / "native_media_storage_acceptance_harness.sh"
WORKFLOW = (
    ROOT / ".github" / "workflows" / "sec08-09-media-storage-resource-acceptance.yml"
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
    cases = [mod.CaseResult(name=n, status="PASS") for n in mod.REQUIRED_CASES[:-1]]
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
    rest = mod.mark_remaining_not_run(mod.REQUIRED_CASES, recorded, reason="stopped")
    names = {c.name for c in recorded + rest}
    assert names == set(mod.REQUIRED_CASES)
    assert all(c.status == "NOT_RUN" for c in rest)
    blocked = mod.mark_remaining_blocked(mod.REQUIRED_CASES, recorded, reason="disk")
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


def test_cleanup_tracks_both_projects_and_stops_before_removing_volumes(
    tmp_path, monkeypatch
):
    mod = _load_mod()
    evidence = tmp_path / "ev"
    evidence.mkdir()
    orch = mod.AcceptanceOrchestrator(evidence)
    projects = ["fetchnow-rollout-test-11111111", "fetchnow-rollout-test-22222222"]
    orch.project = projects[0]
    for project in projects:
        orch.track("compose_project", project)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        stdout = ""
        if argv[:2] == ["docker", "ps"]:
            stdout = "container-" + argv[-1].split("=")[-1]
        elif argv[:3] == ["docker", "volume", "ls"]:
            prefix = argv[-1].removeprefix("name=")
            stdout = prefix + "tmp\nunrelated_tmp\n"
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr(mod, "run", run)
    orch.cleanup()
    for project in projects:
        stop = calls.index(["docker", "rm", "-f", "container-" + project])
        remove = calls.index(["docker", "volume", "rm", project + "_tmp"])
        assert stop < remove
    assert not any(
        argv == ["docker", "volume", "rm", "unrelated_tmp"] for argv in calls
    )
    assert orch.cleanup_ok


def test_cleanup_refuses_unvalidated_project_before_any_commands(tmp_path, monkeypatch):
    mod = _load_mod()
    evidence = tmp_path / "ev"
    evidence.mkdir()
    orch = mod.AcceptanceOrchestrator(evidence)
    orch.track("compose_project", "fetchnow-production")
    calls = []
    monkeypatch.setattr(mod, "run", lambda *a, **kw: calls.append(a))
    with pytest.raises(ValueError, match="forbidden project"):
        orch.cleanup()
    assert not calls and not orch.cleanup_ok
    assert next(c for c in orch.cases if c.name == "S9_cleanup").status == "FAIL"


def test_cleanup_inventory_error_is_not_empty_success(tmp_path, monkeypatch):
    mod = _load_mod()
    evidence = tmp_path / "ev"
    evidence.mkdir()
    orch = mod.AcceptanceOrchestrator(evidence)
    orch.project = "fetchnow-rollout-test-11111111"
    calls = []

    def fail_inventory(argv, **kwargs):
        calls.append(argv)
        assert kwargs["check"] is True
        raise RuntimeError("docker inventory unavailable")

    monkeypatch.setattr(mod, "run", fail_inventory)
    with pytest.raises(RuntimeError, match="inventory unavailable"):
        orch.cleanup()
    assert len(calls) == 1 and calls[0][:2] == ["docker", "ps"]
    assert not orch.cleanup_ok
    assert next(c for c in orch.cases if c.name == "S9_cleanup").status == "FAIL"


@pytest.mark.parametrize(
    "case",
    [
        "S3_container_mounts",
        "S4_missing_or_wrong_fs",
        "S5_legacy_volume_refusal",
        "S6_sidecar_resources",
        "S7_media_operations",
        "S8_transaction_rollback",
    ],
)
def test_orchestrator_failure_is_attributed_to_actual_release_case(
    tmp_path, monkeypatch, case
):
    """Real orchestrator/error export, with ALL native execution stubbed."""
    mod = _load_mod()
    evidence, work = tmp_path / "evidence", tmp_path / "work"
    work.mkdir()
    orch = mod.AcceptanceOrchestrator(evidence)
    monkeypatch.setattr(
        orch, "case_s1", lambda: orch.record("S1_environment_identity", "PASS")
    )
    monkeypatch.setattr(
        orch, "case_s2_fault", lambda: orch.record("S2_fault_injection_enospc", "PASS")
    )

    def contract_ready():
        orch.record("S2_contract_geometry_fs", "PASS")
        return True

    monkeypatch.setattr(orch, "case_s2_contract", contract_ready)
    monkeypatch.setattr(mod.tempfile, "mkdtemp", lambda **kw: str(work))

    def git_only(argv, **kwargs):
        assert argv == ["git", "status", "--porcelain"]
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(mod, "run", git_only)
    primary = "expected migration refusal; got: EGRESS_DENY_CIDRS/EGRESS_DENY_IPS empty"

    def fail():
        orch.begin_case(case)
        raise RuntimeError(primary)

    monkeypatch.setattr(orch, "_run_release_path", fail)
    monkeypatch.setattr(orch, "cleanup", lambda: orch.record("S9_cleanup", "PASS"))
    assert orch.run() == 1
    result = json.loads((evidence / "result.json").read_text())
    failure = json.loads((evidence / "primary_failure.json").read_text())
    statuses = {c["name"]: c["status"] for c in result["cases"]}
    assert failure == {
        "stage": "release_path",
        "case": case,
        "error_type": "RuntimeError",
        "error_detail": primary,
    }
    assert statuses[case] == "FAIL"
    assert all(
        statuses[n] == "NOT_RUN"
        for n in mod.REQUIRED_CASES
        if n.startswith(("S3", "S4", "S5", "S6", "S7", "S8")) and n != case
    )
    assert result["case_counts"] == {"PASS": 4, "FAIL": 1, "NOT_RUN": 5, "BLOCKED": 0}
    assert result["current_release_case"] == case
    assert result["status"] == "FAIL"


@pytest.mark.parametrize("status", ["PASS", "FAIL"])
def test_release_failure_cannot_hide_behind_old_case_result(tmp_path, status):
    mod = _load_mod()
    evidence = tmp_path / "ev"
    evidence.mkdir()
    orch = mod.AcceptanceOrchestrator(evidence)
    orch.begin_case("S5_legacy_volume_refusal")
    orch.record("S5_legacy_volume_refusal", status, "original evidence")
    orch._record_release_failure(RuntimeError("primary"))
    assert len(orch.cases) == 1 and orch.cases[0].status == "FAIL"
    if status == "FAIL":
        assert orch.cases[0].detail == "original evidence"
    else:
        assert "primary" in orch.cases[0].detail


def test_image_evidence_serializes_id_platform_revision_binding(tmp_path):
    from fetchnow_release.manifest import ImageRecord, ReleaseManifest, write_manifest
    from fetchnow_release.release_mode import (
        expected_image_services,
        image_reference_for_service,
    )
    from native_media_storage_resource_release_path import record_prepared_images

    mod = _load_mod()
    evidence = tmp_path / "ev"
    evidence.mkdir()
    orch = mod.AcceptanceOrchestrator(evidence)
    for rev in ("a" * 40, "b" * 40):
        images = tuple(
            ImageRecord(
                service,
                image_reference_for_service(service, rev),
                "sha256:" + rev[0] * 64,
                rev,
                "linux/amd64",
            )
            for service in sorted(expected_image_services("media"))
        )
        manifest = ReleaseManifest(
            schema_version=1,
            source_contract_version=4,
            revision=rev,
            tree_oid="c" * 40,
            created_at_utc="2026-01-01T00:00:00Z",
            archive_commit_verified=True,
            source_repository="FetchNow",
            compose_version="2.0.0",
            docker_version="29.0.0",
            build_platform="linux/amd64",
            images=images,
            contract_hashes={"compose.yaml": "d" * 64},
            tool_version="test",
            status="prepared",
            compose_overlay="compose.staging.yaml",
            release_mode="media",
        )
        path = tmp_path / (rev + ".json")
        write_manifest(path, manifest)
        record_prepared_images(path, rev, orch.image_ids, orch.track)
    orch.begin_case("S5_legacy_volume_refusal")
    partial = json.loads((evidence / "partial_result.json").read_text())
    final = json.loads(orch.write_final("FAIL").read_text())
    assert partial["tracked"] == final["tracked_identities"]
    rows = final["tracked_identities"]
    assert len(rows) == 12
    assert {r["meta"]["revision"] for r in rows} == {"a" * 40, "b" * 40}
    for row in rows:
        assert row["kind"] == "image"
        assert row["meta"]["platform"] == "linux/amd64"
        assert row["value"] == "sha256:" + row["meta"]["revision"][0] * 64
        assert row["meta"]["reference"] == image_reference_for_service(
            row["meta"]["service"],
            row["meta"]["revision"],
        )


def test_workflow_native_requires_dispatch_not_auto_push():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in text
    # Path-filtered push may exist for GitHub workflow indexing only.
    assert "registration/push event only; native not authorized" in text
    assert 'GITHUB_EVENT_NAME}" == "workflow_dispatch"' in text or (
        'GITHUB_EVENT_NAME" == "workflow_dispatch"' in text
    )
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
    for path in (
        SCRIPT,
        HARNESS,
        ROOT / "scripts" / "native_media_storage_resource_release_path.py",
    ):
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
    assert "S9_cleanup" not in mod.REQUIRED_BEFORE_CLEANUP


def test_s9_upsert_overrides_not_run_placeholder():
    mod = _load_mod()
    cases = [
        mod.CaseResult(name="S3_container_mounts", status="FAIL", detail="x"),
        mod.CaseResult(name="S9_cleanup", status="NOT_RUN", detail="placeholder"),
    ]
    mod.upsert_case(
        cases,
        mod.CaseResult(name="S9_cleanup", status="PASS", detail="cleaned"),
    )
    s9 = [c for c in cases if c.name == "S9_cleanup"]
    assert len(s9) == 1
    assert s9[0].status == "PASS"
    assert s9[0].detail == "cleaned"


def test_evidence_manifest_excludes_self_hash(tmp_path):
    mod = _load_mod()
    evidence = tmp_path / "ev"
    evidence.mkdir()
    (evidence / "result.json").write_text('{"status":"FAIL"}\n', encoding="utf-8")
    (evidence / "primary_failure.json").write_text("{}\n", encoding="utf-8")
    orch = mod.AcceptanceOrchestrator(evidence)
    orch._write_evidence_manifest()
    man = (evidence / "evidence_sha256.json").read_text(encoding="utf-8")
    assert "result.json" in man
    assert "primary_failure.json" in man
    assert '"evidence_sha256.json"' not in man
    digest = (evidence / "evidence_manifest.sha256").read_text(encoding="utf-8").strip()
    assert len(digest) == 64


def test_workflow_exports_evidence_owner_and_separate_host_logs():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "--evidence-owner" in text
    assert "media-storage-host-logs" in text
    assert "chmod 777" not in text
    assert "evidence export failed" in text
