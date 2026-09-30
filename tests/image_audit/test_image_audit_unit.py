"""Offline unit tests for SEC-03C image_audit gate."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fetchnow_release.bounded_subprocess import (  # noqa: E402
    BoundedResult,
    BoundedTimeoutError,
    OutputLimitExceededError,
)

import image_audit as ia  # noqa: E402

# ---------------------------------------------------------------------------
# Embedded synthetic fixtures
API_IMAGE_ID = "sha256:" + ("a" * 64)
_DIFF_ID = "sha256:" + ("b" * 64)
ALPINE_IMAGE_ID = "sha256:" + ("c" * 64)

CLEAN_DEBIAN_API_REPORT: dict[str, Any] = {
    "SchemaVersion": 2,
    "CreatedAt": "2026-09-30T12:00:00.000000Z",
    "ArtifactName": API_IMAGE_ID,
    "ArtifactType": "container_image",
    "Metadata": {
        "OS": {"Family": "debian", "Name": "12.11"},
        "ImageID": API_IMAGE_ID,
        "DiffIDs": [_DIFF_ID],
        "RepoTags": ["fetchnow-api:ci"],
        "RepoDigests": [],
        "ImageConfig": {
            "architecture": "amd64",
            "os": "linux",
        },
    },
    "Results": [
        {
            "Target": "fetchnow-api (debian 12.11)",
            "Class": "os-pkgs",
            "Type": "debian",
            "Vulnerabilities": [],
        },
        {
            "Target": "Python",
            "Class": "lang-pkgs",
            "Type": "python-pkg",
            "Vulnerabilities": [],
        },
    ],
}

FINDINGS_FIXABLE_HIGH: dict[str, Any] = {
    **CLEAN_DEBIAN_API_REPORT,
    "Results": [
        {
            "Target": "fetchnow-api (debian 12.11)",
            "Class": "os-pkgs",
            "Type": "debian",
            "Vulnerabilities": [
                {
                    "VulnerabilityID": "CVE-2026-0001",
                    "PkgName": "ffmpeg",
                    "InstalledVersion": "7:5.1.0-1",
                    "FixedVersion": "7:5.1.1-1",
                    "Severity": "HIGH",
                    "Status": "fixed",
                    "Title": "example fixable",
                    "PrimaryURL": "https://example.invalid/CVE-2026-0001",
                }
            ],
        },
        {
            "Target": "Python",
            "Class": "lang-pkgs",
            "Type": "python-pkg",
            "Vulnerabilities": [],
        },
    ],
}

FINDINGS_UNFIXED_CRITICAL: dict[str, Any] = {
    **CLEAN_DEBIAN_API_REPORT,
    "Results": [
        {
            "Target": "fetchnow-api (debian 12.11)",
            "Class": "os-pkgs",
            "Type": "debian",
            "Vulnerabilities": [
                {
                    "VulnerabilityID": "CVE-2026-0002",
                    "PkgName": "libssl3",
                    "InstalledVersion": "3.0.0",
                    "FixedVersion": "",
                    "Severity": "CRITICAL",
                    "Status": "affected",
                    "Title": "example unfixed",
                    "PrimaryURL": "https://example.invalid/CVE-2026-0002",
                }
            ],
        },
        {
            "Target": "Python",
            "Class": "lang-pkgs",
            "Type": "python-pkg",
            "Vulnerabilities": [],
        },
    ],
}

FINDINGS_NOT_AFFECTED: dict[str, Any] = {
    **CLEAN_DEBIAN_API_REPORT,
    "Results": [
        {
            "Target": "fetchnow-api (debian 12.11)",
            "Class": "os-pkgs",
            "Type": "debian",
            "Vulnerabilities": [
                {
                    "VulnerabilityID": "CVE-2026-0003",
                    "PkgName": "zlib1g",
                    "InstalledVersion": "1:1.2.13.dfsg-1",
                    "FixedVersion": "",
                    "Severity": "HIGH",
                    "Status": "not_affected",
                    "Title": "vendor not affected",
                }
            ],
        },
        {
            "Target": "Python",
            "Class": "lang-pkgs",
            "Type": "python-pkg",
            "Vulnerabilities": [],
        },
    ],
}

CLEAN_ALPINE_REPORT: dict[str, Any] = {
    "SchemaVersion": 2,
    "Metadata": {
        "OS": {"Family": "alpine", "Name": "3.22.0"},
        "ImageID": ALPINE_IMAGE_ID,
        "ImageConfig": {"architecture": "amd64", "os": "linux"},
    },
    "Results": [
        {
            "Target": "nginx (alpine 3.22.0)",
            "Class": "os-pkgs",
            "Type": "alpine",
            "Vulnerabilities": [],
        }
    ],
}

SECRET_TITLE_REPORT: dict[str, Any] = {
    **CLEAN_ALPINE_REPORT,
    "Results": [
        {
            "Target": "nginx (alpine 3.22.0)",
            "Class": "os-pkgs",
            "Type": "alpine",
            "Vulnerabilities": [
                {
                    "VulnerabilityID": "CVE-2026-0099",
                    "PkgName": "demo",
                    "InstalledVersion": "1.0",
                    "FixedVersion": "",
                    "Severity": "LOW",
                    "Status": "affected",
                    "Title": "token=SECRETVALUE should not leak raw",
                    "PrimaryURL": "https://example.invalid/x",
                }
            ],
        }
    ],
}

def _result(
    *,
    argv: list[str] | tuple[str, ...] = ("tool",),
    returncode: int = 0,
    stdout: str | bytes = "",
    stderr: str | bytes = "",
) -> BoundedResult:
    out = stdout if isinstance(stdout, bytes) else stdout.encode()
    err = stderr if isinstance(stderr, bytes) else stderr.encode()
    return BoundedResult(
        argv=tuple(argv), returncode=returncode, stdout=out, stderr=err
    )


def test_clean_report_pass() -> None:
    tr = ia.evaluate_trivy_report(
        "api",
        CLEAN_DEBIAN_API_REPORT,
        expected_image_id=API_IMAGE_ID,
        expected_platform="linux/amd64",
    )
    assert tr.status == ia.STATUS_PASS
    assert tr.error_code is None
    assert tr.counts["total"] == 0
    assert tr.details["python_seen"] is True
    assert tr.details["os_pkgs_seen"] is True


def test_fixable_high_fails() -> None:
    tr = ia.evaluate_trivy_report(
        "api",
        FINDINGS_FIXABLE_HIGH,
        expected_image_id=API_IMAGE_ID,
        expected_platform="linux/amd64",
    )
    assert tr.status == ia.STATUS_FAIL
    assert tr.counts["gate_fixable"] == 1
    assert tr.findings[0].has_fix is True


def test_unfixed_critical_needs_owner() -> None:
    tr = ia.evaluate_trivy_report(
        "api",
        FINDINGS_UNFIXED_CRITICAL,
        expected_image_id=API_IMAGE_ID,
        expected_platform="linux/amd64",
    )
    assert tr.status == ia.STATUS_NEEDS_OWNER
    assert tr.counts["gate_unfixed"] == 1
    assert tr.findings[0].has_fix is False


def test_not_affected_excluded_from_gate() -> None:
    tr = ia.evaluate_trivy_report(
        "api",
        FINDINGS_NOT_AFFECTED,
        expected_image_id=API_IMAGE_ID,
        expected_platform="linux/amd64",
    )
    assert tr.status == ia.STATUS_PASS
    assert tr.counts["total"] == 0


def test_malformed_root() -> None:
    tr = ia.evaluate_trivy_report(
        "api",
        {"SchemaVersion": "nope"},
        expected_image_id=API_IMAGE_ID,
        expected_platform="linux/amd64",
    )
    assert tr.status == ia.STATUS_TECHNICAL
    assert tr.error_code == "report_incomplete"


def test_empty_results_incomplete() -> None:
    report = {
        "SchemaVersion": 2,
        "Metadata": {
            "OS": {"Family": "debian", "Name": "12"},
            "ImageID": API_IMAGE_ID,
            "ImageConfig": {"architecture": "amd64", "os": "linux"},
        },
        "Results": [],
    }
    tr = ia.evaluate_trivy_report(
        "api",
        report,
        expected_image_id=API_IMAGE_ID,
        expected_platform="linux/amd64",
    )
    assert tr.error_code == "results_missing"


def test_missing_vulnerabilities_key_still_ok_for_os_entry() -> None:
    """Absence of Vulnerabilities key on a Result is allowed (no vulns object)."""
    report = {
        "SchemaVersion": 2,
        "Metadata": {
            "OS": {"Family": "alpine", "Name": "3.22.0"},
            "ImageID": ALPINE_IMAGE_ID,
            "ImageConfig": {"architecture": "amd64", "os": "linux"},
        },
        "Results": [
            {"Target": "x", "Class": "os-pkgs", "Type": "alpine"},
        ],
    }
    tr = ia.evaluate_trivy_report(
        "web",
        report,
        expected_image_id=ALPINE_IMAGE_ID,
        expected_platform="linux/amd64",
    )
    assert tr.status == ia.STATUS_PASS


def test_vulnerabilities_null_incomplete() -> None:
    report = {
        "SchemaVersion": 2,
        "Metadata": {
            "OS": {"Family": "alpine", "Name": "3.22.0"},
            "ImageID": ALPINE_IMAGE_ID,
            "ImageConfig": {"architecture": "amd64", "os": "linux"},
        },
        "Results": [
            {
                "Target": "x",
                "Class": "os-pkgs",
                "Type": "alpine",
                "Vulnerabilities": None,
            },
        ],
    }
    # None is treated as "key present but null" → malformed
    details, err = ia.check_report_completeness(
        "web",
        report,
        expected_image_id=ALPINE_IMAGE_ID,
        expected_platform="linux/amd64",
    )
    assert err == "report_malformed"
    assert details["os_family"] == "alpine"


def test_image_mismatch() -> None:
    tr = ia.evaluate_trivy_report(
        "api",
        CLEAN_DEBIAN_API_REPORT,
        expected_image_id="sha256:" + "b" * 64,
        expected_platform="linux/amd64",
    )
    assert tr.error_code == "image_mismatch"


def test_platform_mismatch_in_report() -> None:
    report = json.loads(json.dumps(CLEAN_DEBIAN_API_REPORT))
    report["Metadata"]["ImageConfig"]["architecture"] = "arm64"
    tr = ia.evaluate_trivy_report(
        "api",
        report,
        expected_image_id=API_IMAGE_ID,
        expected_platform="linux/amd64",
    )
    assert tr.error_code == "platform_mismatch"


def test_api_missing_python_surface() -> None:
    report = {
        "SchemaVersion": 2,
        "Metadata": {
            "OS": {"Family": "debian", "Name": "12"},
            "ImageID": API_IMAGE_ID,
            "ImageConfig": {"architecture": "amd64", "os": "linux"},
        },
        "Results": [
            {
                "Target": "debian",
                "Class": "os-pkgs",
                "Type": "debian",
                "Vulnerabilities": [],
            }
        ],
    }
    tr = ia.evaluate_trivy_report(
        "api",
        report,
        expected_image_id=API_IMAGE_ID,
        expected_platform="linux/amd64",
    )
    assert tr.error_code == "python_surface_missing"


def test_wrong_os_family() -> None:
    tr = ia.evaluate_trivy_report(
        "web",
        CLEAN_DEBIAN_API_REPORT,
        expected_image_id=API_IMAGE_ID,
        expected_platform="linux/amd64",
    )
    assert tr.error_code == "os_family_mismatch"


def test_load_report_empty_and_truncated(tmp_path: Path) -> None:
    empty = tmp_path / "empty.json"
    empty.write_bytes(b"")
    _, err = ia.load_trivy_report(empty)
    assert err == "report_empty"

    missing = tmp_path / "nope.json"
    _, err = ia.load_trivy_report(missing)
    assert err == "report_missing"

    big = tmp_path / "big.json"
    big.write_bytes(b"{" + b"a" * 100)
    _, err = ia.load_trivy_report(big, max_bytes=10)
    assert err == "report_too_large"

    bad = tmp_path / "bad.json"
    bad.write_text("{not-json", encoding="utf-8")
    _, err = ia.load_trivy_report(bad)
    assert err == "report_malformed"


def test_db_metadata_validation() -> None:
    now = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
    good = {
        "Version": 2,
        "UpdatedAt": "2026-09-30T10:00:00Z",
        "DownloadedAt": "2026-09-30T11:00:00Z",
        "NextUpdate": "2026-10-01T10:00:00Z",
    }
    evidence, err = ia.validate_db_metadata(good, now=now)
    assert err is None
    assert evidence["version"] == 2

    _, err = ia.validate_db_metadata(None, now=now)
    assert err == "db_metadata_missing"

    stale = {
        **good,
        "DownloadedAt": "2026-09-20T11:00:00Z",
    }
    _, err = ia.validate_db_metadata(stale, now=now)
    assert err == "db_stale"

    bad_ver = {**good, "Version": 0}
    _, err = ia.validate_db_metadata(bad_ver, now=now)
    assert err == "db_metadata_invalid"


def test_parse_trivy_version() -> None:
    assert ia.parse_trivy_version("Version: 0.74.0\n") == "0.74.0"
    assert ia.parse_trivy_version("nope") is None


def test_trivy_asset_for_uname() -> None:
    assert "Linux-64bit" in ia.trivy_asset_for_uname("x86_64", "Linux")
    assert "Linux-ARM64" in ia.trivy_asset_for_uname("aarch64", "Linux")
    assert "macOS-ARM64" in ia.trivy_asset_for_uname("arm64", "Darwin")
    with pytest.raises(ValueError):
        ia.trivy_asset_for_uname("riscv64", "Linux")


def test_identity_from_inspect() -> None:
    inspect_data = {
        "Id": API_IMAGE_ID,
        "Os": "linux",
        "Architecture": "amd64",
        "RepoDigests": ["postgres@sha256:" + "d" * 64],
        "Config": {"Labels": {ia.OCI_REVISION_LABEL: "abc"}},
    }
    identity, err = ia.identity_from_inspect("api", "fetchnow-api:ci", inspect_data)
    assert err is None
    assert identity is not None
    assert identity.platform == "linux/amd64"
    assert identity.revision_label == "abc"
    assert identity.repo_digests[0].startswith("postgres@sha256:")
    assert identity.config_digest == API_IMAGE_ID
    assert identity.image_id == API_IMAGE_ID
    assert identity.repo_manifest_digests == ("sha256:" + "d" * 64,)


def test_identity_allows_distinct_manifest_and_config_digests() -> None:
    """Different correct manifest vs config digests are not an error by themselves."""
    config_id = "sha256:" + "a" * 64
    manifest = "sha256:" + "b" * 64
    identity, err = ia.identity_from_inspect(
        "postgres",
        "postgres:16.15-alpine3.24",
        {
            "Id": config_id,
            "Os": "linux",
            "Architecture": "amd64",
            "RepoDigests": [f"postgres@{manifest}"],
            "Config": {"Labels": {}},
        },
    )
    assert err is None
    assert identity is not None
    assert identity.image_id == config_id
    assert identity.config_digest == config_id
    assert identity.repo_manifest_digests == (manifest,)
    assert identity.image_id != identity.repo_manifest_digests[0]


def test_identity_rejects_when_id_missing_platform() -> None:
    _, err = ia.identity_from_inspect(
        "postgres",
        "postgres:16.15-alpine3.24",
        {
            "Id": "sha256:" + "c" * 64,
            "Os": "",
            "Architecture": "",
            "RepoDigests": [],
            "Config": {"Labels": {}},
        },
    )
    assert err == "platform_missing"


def test_identity_local_repo_digest_may_equal_config_id() -> None:
    """Docker often records local RepoDigests as name@<Id>; that is allowed."""
    digest = "sha256:" + "c" * 64
    identity, err = ia.identity_from_inspect(
        "api",
        "fetchnow-api:local",
        {
            "Id": digest,
            "Os": "linux",
            "Architecture": "amd64",
            "RepoDigests": [f"fetchnow-api@{digest}"],
            "Config": {"Labels": {}},
        },
    )
    assert err is None
    assert identity is not None
    assert identity.image_id == digest
    assert identity.repo_manifest_digests == (digest,)


def test_report_rejects_manifest_digest_standing_in_for_image_id() -> None:
    config_id = "sha256:" + "a" * 64
    manifest = "sha256:" + "b" * 64
    report = {
        "SchemaVersion": 2,
        "Metadata": {
            "OS": {"Family": "alpine", "Name": "3.24.2"},
            # Scanner reported the manifest digest instead of inspect Id.
            "ImageID": manifest,
            "ImageConfig": {"architecture": "amd64", "os": "linux"},
        },
        "Results": [
            {
                "Target": "x",
                "Class": "os-pkgs",
                "Type": "alpine",
                "Vulnerabilities": [],
            },
        ],
    }
    details, err = ia.check_report_completeness(
        "postgres",
        report,
        expected_image_id=config_id,
        expected_platform="linux/amd64",
        expected_repo_manifest_digests=(manifest,),
    )
    assert err == "image_mismatch"
    assert details.get("image_id_error") == "manifest_digest_not_image_id"


def test_identity_rejects_bad_id() -> None:
    _, err = ia.identity_from_inspect(
        "api",
        "x",
        {"Id": "sha256:dead", "Os": "linux", "Architecture": "amd64"},
    )
    assert err == "image_id_invalid"


def test_merge_and_exit_codes() -> None:
    assert ia.merge_overall_status([ia.STATUS_PASS, ia.STATUS_PASS]) == ia.STATUS_PASS
    assert (
        ia.merge_overall_status([ia.STATUS_PASS, ia.STATUS_NEEDS_OWNER])
        == ia.STATUS_NEEDS_OWNER
    )
    assert (
        ia.merge_overall_status([ia.STATUS_FAIL, ia.STATUS_NEEDS_OWNER])
        == ia.STATUS_FAIL
    )
    assert (
        ia.merge_overall_status([ia.STATUS_TECHNICAL, ia.STATUS_FAIL])
        == ia.STATUS_TECHNICAL
    )
    assert ia.overall_exit_code(ia.STATUS_PASS) == 0
    assert ia.overall_exit_code(ia.STATUS_FAIL) == 1
    assert ia.overall_exit_code(ia.STATUS_NEEDS_OWNER) == 2
    assert ia.overall_exit_code(ia.STATUS_TECHNICAL) == 1


def test_parse_targets_file(tmp_path: Path) -> None:
    path = tmp_path / "targets.json"
    path.write_text(
        json.dumps(
            {
                "api": "fetchnow-api:ci",
                "web": "fetchnow-web:ci",
                "gateway": "fetchnow-gateway:ci",
                "postgres": "postgres:16.9-alpine",
            }
        ),
        encoding="utf-8",
    )
    targets = ia.parse_targets_file(path)
    assert set(targets) == set(ia.REQUIRED_TARGETS)

    path.write_text(json.dumps({"api": "x"}), encoding="utf-8")
    with pytest.raises(ValueError, match="missing"):
        ia.parse_targets_file(path)


def test_summary_redacts_secret_shaped_title() -> None:
    tr = ia.evaluate_trivy_report(
        "web",
        SECRET_TITLE_REPORT,
        expected_image_id=ALPINE_IMAGE_ID,
        expected_platform="linux/amd64",
    )
    assert tr.status == ia.STATUS_PASS
    summary = ia.build_safe_summary(
        overall=ia.STATUS_PASS,
        trivy_version="0.74.0",
        expected_platform="linux/amd64",
        host_platform_note=None,
        db={"version": 2},
        targets=[tr],
        scanned_at=datetime(2026, 9, 30, tzinfo=UTC),
    )
    blob = ia.redact_secrets(json.dumps(summary))
    assert "SECRETVALUE" not in blob
    assert "[redacted]" in blob


def test_trivy_scan_argv_contract() -> None:
    argv = ia.trivy_scan_argv(
        trivy_bin="/usr/local/bin/trivy",
        cache_dir=Path("/tmp/cache"),
        image_id=API_IMAGE_ID,
        output_path=Path("/tmp/out.json"),
        timeout_seconds=120,
    )
    assert "--scanners" in argv and "vuln" in argv
    assert "secret" not in argv
    assert "misconfig" not in argv
    assert "--offline-scan" in argv
    assert "--skip-db-update" in argv
    assert "--image-src" in argv and "docker" in argv
    assert "--disable-telemetry" in argv
    assert argv[argv.index("--exit-code") + 1] == "0"
    assert argv[-1] == API_IMAGE_ID


def test_run_gate_missing_target_technical(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append([str(a) for a in argv])
        raise AssertionError("should not run scanner when targets invalid")

    # Direct call with incomplete mapping
    summary, code = ia.run_gate(
        trivy_bin="trivy",
        cache_dir=tmp_path / "cache",
        targets={"api": "x"},  # type: ignore[arg-type]
        expected_platform="linux/amd64",
        work_dir=tmp_path / "work",
        runner=runner,
    )
    assert code == ia.EXIT_FAIL
    assert summary["status"] == ia.STATUS_TECHNICAL
    assert summary["error_code"] == "targets_invalid"
    assert calls == []


def test_run_gate_version_mismatch(tmp_path: Path) -> None:
    def runner(argv, **kwargs):  # type: ignore[no-untyped-def]
        return _result(argv=argv, stdout="Version: 0.73.0\n")

    targets = {name: f"{name}:t" for name in ia.REQUIRED_TARGETS}
    summary, code = ia.run_gate(
        trivy_bin="trivy",
        cache_dir=tmp_path / "cache",
        targets=targets,
        expected_platform="linux/amd64",
        work_dir=tmp_path / "work",
        runner=runner,
    )
    assert code == ia.EXIT_FAIL
    assert summary["error_code"] == "tool_version_mismatch"


def test_run_gate_db_download_timeout(tmp_path: Path) -> None:
    def runner(argv, **kwargs):  # type: ignore[no-untyped-def]
        s = [str(a) for a in argv]
        if "version" in s:
            return _result(argv=argv, stdout="Version: 0.74.0\n")
        if "--download-db-only" in s:
            raise BoundedTimeoutError("db timed out")
        raise AssertionError(s)

    targets = {name: f"{name}:t" for name in ia.REQUIRED_TARGETS}
    summary, code = ia.run_gate(
        trivy_bin="trivy",
        cache_dir=tmp_path / "cache",
        targets=targets,
        expected_platform="linux/amd64",
        work_dir=tmp_path / "work",
        runner=runner,
    )
    assert code == ia.EXIT_FAIL
    assert summary["error_code"] == "scanner_timeout"


def test_run_gate_output_limit_on_db(tmp_path: Path) -> None:
    def runner(argv, **kwargs):  # type: ignore[no-untyped-def]
        s = [str(a) for a in argv]
        if "version" in s:
            return _result(argv=argv, stdout="Version: 0.74.0\n")
        if "--download-db-only" in s:
            raise OutputLimitExceededError("stderr exceeded")
        raise AssertionError(s)

    targets = {name: f"{name}:t" for name in ia.REQUIRED_TARGETS}
    summary, code = ia.run_gate(
        trivy_bin="trivy",
        cache_dir=tmp_path / "cache",
        targets=targets,
        expected_platform="linux/amd64",
        work_dir=tmp_path / "work",
        runner=runner,
    )
    assert summary["error_code"] == "scanner_output_limit"
    assert code == ia.EXIT_FAIL


def test_medium_only_is_pass() -> None:
    report = json.loads(json.dumps(CLEAN_ALPINE_REPORT))
    report["Results"][0]["Vulnerabilities"] = [
        {
            "VulnerabilityID": "CVE-2026-0040",
            "PkgName": "busybox",
            "InstalledVersion": "1.0",
            "FixedVersion": "1.1",
            "Severity": "MEDIUM",
            "Status": "fixed",
            "Title": "medium only",
        }
    ]
    tr = ia.evaluate_trivy_report(
        "gateway",
        report,
        expected_image_id=ALPINE_IMAGE_ID,
        expected_platform="linux/amd64",
    )
    assert tr.status == ia.STATUS_PASS
    assert tr.counts["medium"] == 1
    assert tr.counts["gate_fixable"] == 0
