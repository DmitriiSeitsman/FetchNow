"""Offline unit tests for candidate-7 image audit aggregate."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import image_audit as ia  # noqa: E402
import image_audit_candidate7 as c7  # noqa: E402

WORKFLOW = ROOT / ".github/workflows/sec03c-candidate7-image-audit.yml"

API_ID = "sha256:" + ("a" * 64)
WEB_ID = "sha256:" + ("c" * 64)
NET_ID = "sha256:" + ("d" * 64)
_DIFF = "sha256:" + ("b" * 64)


def _debian_report(
    image_id: str,
    *,
    with_python: bool = True,
    vulns: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = [
        {
            "Target": "img (debian 13)",
            "Class": "os-pkgs",
            "Type": "debian",
            "Vulnerabilities": vulns or [],
        }
    ]
    if with_python:
        results.append(
            {
                "Target": "Python",
                "Class": "lang-pkgs",
                "Type": "python-pkg",
                "Vulnerabilities": [],
            }
        )
    return {
        "SchemaVersion": 2,
        "Metadata": {
            "OS": {"Family": "debian", "Name": "13"},
            "ImageID": image_id,
            "DiffIDs": [_DIFF],
            "ImageConfig": {"architecture": "amd64", "os": "linux"},
        },
        "Results": results,
    }


def test_canonical_gate_still_requires_exactly_four_targets() -> None:
    assert ia.REQUIRED_TARGETS == ("api", "web", "gateway", "postgres")
    assert len(c7.CANDIDATE7_TARGETS) == 7
    assert "media-net-executor" in c7.CANDIDATE7_TARGETS
    assert "media-executor" in c7.CANDIDATE7_TARGETS
    assert "egress-proxy" in c7.CANDIDATE7_TARGETS


def test_parse_targets_file_requires_all_seven(tmp_path: Path) -> None:
    path = tmp_path / "t.json"
    path.write_text(json.dumps({"api": "x"}), encoding="utf-8")
    with pytest.raises(ValueError, match="missing"):
        c7.parse_targets_file(path)
    full = {name: f"ref-{name}" for name in c7.CANDIDATE7_TARGETS}
    path.write_text(json.dumps(full), encoding="utf-8")
    assert c7.parse_targets_file(path)["postgres"].startswith("ref-")


def test_parse_targets_rejects_extra(tmp_path: Path) -> None:
    full = {name: f"ref-{name}" for name in c7.CANDIDATE7_TARGETS}
    full["builder"] = "nope"
    path = tmp_path / "t.json"
    path.write_text(json.dumps(full), encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected"):
        c7.parse_targets_file(path)


def test_media_net_requires_python_surface() -> None:
    report = _debian_report(NET_ID, with_python=False)
    details, err = c7.check_candidate7_completeness(
        "media-net-executor",
        report,
        expected_image_id=NET_ID,
        expected_platform="linux/amd64",
    )
    assert err == "python_surface_missing"
    assert details["python_required"] is True


def test_media_executor_allows_missing_python_pkgs() -> None:
    report = _debian_report(API_ID, with_python=False)
    details, err = c7.check_candidate7_completeness(
        "media-executor",
        report,
        expected_image_id=API_ID,
        expected_platform="linux/amd64",
    )
    assert err is None
    assert details["python_seen"] is False


def test_api_still_requires_python() -> None:
    report = _debian_report(API_ID, with_python=False)
    _, err = c7.check_candidate7_completeness(
        "api",
        report,
        expected_image_id=API_ID,
        expected_platform="linux/amd64",
    )
    assert err == "python_surface_missing"


def test_image_mismatch_is_technical() -> None:
    report = _debian_report(API_ID)
    report["Metadata"]["ImageID"] = WEB_ID
    tr = c7.evaluate_candidate7_report(
        "api",
        report,
        expected_image_id=API_ID,
        expected_platform="linux/amd64",
    )
    assert tr.status == ia.STATUS_TECHNICAL
    assert tr.error_code == "image_mismatch"


def test_missing_results_is_technical() -> None:
    report = _debian_report(API_ID)
    report["Results"] = []
    tr = c7.evaluate_candidate7_report(
        "api",
        report,
        expected_image_id=API_ID,
        expected_platform="linux/amd64",
    )
    assert tr.status == ia.STATUS_TECHNICAL
    assert tr.error_code == "results_missing"


def test_fixable_high_fails_policy() -> None:
    report = _debian_report(
        API_ID,
        vulns=[
            {
                "VulnerabilityID": "CVE-2026-TEST1",
                "PkgName": "ffmpeg",
                "InstalledVersion": "1.0",
                "FixedVersion": "1.1",
                "Severity": "HIGH",
                "Status": "fixed",
            }
        ],
    )
    tr = c7.evaluate_candidate7_report(
        "api",
        report,
        expected_image_id=API_ID,
        expected_platform="linux/amd64",
    )
    assert tr.status == ia.STATUS_FAIL
    assert tr.counts["gate_fixable"] == 1


def test_unfixed_high_needs_owner() -> None:
    report = _debian_report(
        API_ID,
        vulns=[
            {
                "VulnerabilityID": "CVE-2026-TEST2",
                "PkgName": "openssl",
                "InstalledVersion": "1.0",
                "Severity": "CRITICAL",
                "Status": "affected",
            }
        ],
    )
    tr = c7.evaluate_candidate7_report(
        "api",
        report,
        expected_image_id=API_ID,
        expected_platform="linux/amd64",
    )
    assert tr.status == ia.STATUS_NEEDS_OWNER


def test_role_mapping_api_covers_worker_delivery() -> None:
    assert c7.ROLE_MAPPING["api"] == ("api", "worker", "delivery")
    assert len(set(c7.ROLE_MAPPING["api"])) == 3


def test_summarize_and_reconcile() -> None:
    findings = [
        ia.Finding(
            target="api",
            vulnerability_id="CVE-1",
            pkg_name="x",
            pkg_path=None,
            installed_version="1",
            fixed_version="2",
            severity="HIGH",
            status="fixed",
            title=None,
            primary_url=None,
            pkg_type="debian",
            class_name="os-pkgs",
        ),
        ia.Finding(
            target="web",
            vulnerability_id="CVE-1",
            pkg_name="x",
            pkg_path=None,
            installed_version="1",
            fixed_version="2",
            severity="HIGH",
            status="fixed",
            title=None,
            primary_url=None,
            pkg_type="alpine",
            class_name="os-pkgs",
        ),
    ]
    summary = c7.summarize_findings(findings)
    assert summary["high_critical_instances"] == 2
    assert summary["unique_count"] == 1
    recon = c7.reconcile_with_prior(
        current_ids=summary["high_critical_unique_ids"],
        prior_ids=["CVE-1", "CVE-OLD"],
        comparable=True,
        note="test",
    )
    assert recon["retained"] == ["CVE-1"]
    assert recon["gone"] == ["CVE-OLD"]
    assert recon["new"] == []


def test_workflow_marker_and_pins() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "[native-once-20261014-img7d0]" in text
    assert "fetch-depth: 0" in text
    assert "2ae6fe3ee734b7fdf11335663e18c75ea12dccc76062f09f164a3b0f8be4371a" in text
    assert "0.74.0" in text
    assert "postgres:16.15-alpine3.24" in text
    assert "github.run_attempt == 1" in text
    assert (
        "workflow_dispatch" not in text.split("on:", 1)[1].split("permissions:", 1)[0]
    )
    assert "686369bd085bd073cb44bd791cb1c0e5beba8323" in text
    assert "38627426197" in text
    assert "38631542113" in text
    assert "NOT_GRANTED" in text
    assert "image_audit_candidate7.py" in text
    assert "image_audit_candidate7_history.py" in text
    # Must not call canonical four-target CLI with seven targets.
    assert "scripts/image_audit.py \\" not in text


def test_trivy_scan_argv_still_offline_contract() -> None:
    argv = ia.trivy_scan_argv(
        trivy_bin="/usr/bin/trivy",
        cache_dir=Path("/tmp/cache"),
        image_id=API_ID,
        output_path=Path("/tmp/out.json"),
        timeout_seconds=60,
    )
    assert "--offline-scan" in argv
    assert "--skip-db-update" in argv
    assert "--image-src" in argv and "docker" in argv
    assert "--exit-code" in argv and "0" in argv


def test_gate_rejects_invalid_targets_without_scanning(tmp_path: Path) -> None:
    summary, code = c7.run_candidate7_gate(
        trivy_bin="trivy",
        cache_dir=tmp_path / "cache",
        targets={"api": "x"},  # type: ignore[arg-type]
        expected_platform="linux/amd64",
        work_dir=tmp_path / "work",
        repo_root=ROOT,
        audited_source_sha="a" * 40,
        audit_commit_sha="b" * 40,
        collect_inventories=False,
    )
    assert code == ia.EXIT_FAIL
    assert summary["error_code"] == "targets_invalid"


def test_postgres_boundary_fields_in_module() -> None:
    assert c7.POSTGRES_COMPOSE_REF == "postgres:16.15-alpine3.24"
    assert c7.BASELINE_NATIVE_SHA.startswith("686369bd")
