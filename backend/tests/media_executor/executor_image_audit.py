"""SEC-08 executor image audit adapter.

Reuses SEC-03C Trivy 0.74.0 helpers and HIGH/CRITICAL policy without changing
the four-target canonical gate. Completeness for this image is debian OS
packages (no language-package Result required: pip/venv are absent by design).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import image_audit as ia  # type: ignore[import-not-found]  # noqa: E402


def check_executor_completeness(
    report: dict[str, Any],
    *,
    expected_image_id: str,
    expected_platform: str,
) -> tuple[dict[str, Any], str | None]:
    """Debian OS-package completeness for the media-executor runtime image."""
    details: dict[str, Any] = {
        "completeness_profile": "debian-os-pkgs-no-language-pkgs"
    }
    schema = report.get("SchemaVersion")
    details["schema_version"] = schema
    if not isinstance(schema, int) or schema < 2:
        return details, "report_schema"
    meta = report.get("Metadata")
    if not isinstance(meta, dict):
        return details, "metadata_missing"
    report_id = meta.get("ImageID")
    details["report_image_id"] = report_id
    if ia.normalize_digest(str(report_id or "")) != ia.normalize_digest(
        expected_image_id
    ):
        return details, "image_mismatch"
    image_config = meta.get("ImageConfig")
    report_platform = None
    if isinstance(image_config, dict):
        os_name = str(image_config.get("os") or image_config.get("Os") or "").strip()
        arch = str(
            image_config.get("architecture") or image_config.get("Architecture") or ""
        ).strip()
        if os_name and arch:
            report_platform = f"{os_name}/{arch}"
    details["report_platform"] = report_platform
    if report_platform and report_platform != expected_platform:
        return details, "platform_mismatch"
    os_meta = meta.get("OS")
    if not isinstance(os_meta, dict):
        return details, "os_surface_missing"
    family = str(os_meta.get("Family") or "").strip().lower()
    details["os_family"] = family or None
    details["os_name"] = str(os_meta.get("Name") or "").strip() or None
    if family != "debian":
        return details, "os_family_mismatch"
    results = report.get("Results")
    if not isinstance(results, list) or not results:
        return details, "results_missing"
    os_pkg_seen = False
    python_seen = False
    result_types: list[str] = []
    for entry in results:
        if not isinstance(entry, dict):
            return details, "report_malformed"
        if "Vulnerabilities" in entry:
            vulns_field = entry["Vulnerabilities"]
            if vulns_field is None or not isinstance(vulns_field, list):
                return details, "report_malformed"
        class_name = str(entry.get("Class") or "")
        type_name = str(entry.get("Type") or "")
        if type_name:
            result_types.append(type_name)
        if class_name == "os-pkgs" or type_name in {"debian", "alpine", "ubuntu"}:
            os_pkg_seen = True
        if type_name in {"python-pkg", "pip", "poetry", "uv"} or (
            class_name == "lang-pkgs" and "python" in type_name
        ):
            python_seen = True
    details["result_types"] = sorted(set(result_types))
    details["os_pkgs_seen"] = os_pkg_seen
    details["python_seen"] = python_seen
    if not os_pkg_seen:
        return details, "os_surface_missing"
    return details, None


def audit_image(
    *,
    image_ref: str,
    expected_image_id: str,
    trivy_bin: str,
    cache_dir: Path,
    work_dir: Path,
    expected_platform: str = "linux/amd64",
) -> tuple[dict[str, Any], int]:
    work_dir.mkdir(parents=True, exist_ok=True)
    scanned_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    observed_ver, ver_err = ia.verify_trivy_version(trivy_bin)
    if ver_err:
        summary = {
            "gate": "sec-08-executor-image-audit",
            "status": ia.STATUS_TECHNICAL,
            "error_code": ver_err,
            "scanned_at": scanned_at,
        }
        return summary, ia.EXIT_FAIL

    db_evidence, db_err = ia.download_advisory_db(trivy_bin, cache_dir)
    if db_err:
        summary = {
            "gate": "sec-08-executor-image-audit",
            "status": ia.STATUS_TECHNICAL,
            "error_code": db_err,
            "db": db_evidence,
            "scanner": {"name": "trivy", "version": observed_ver},
            "scanned_at": scanned_at,
        }
        return summary, ia.EXIT_FAIL

    inspect_data, insp_err = ia.inspect_local_image(image_ref)
    if insp_err or inspect_data is None:
        summary = {
            "gate": "sec-08-executor-image-audit",
            "status": ia.STATUS_TECHNICAL,
            "error_code": insp_err or "inspect_failed",
            "scanned_at": scanned_at,
        }
        return summary, ia.EXIT_FAIL
    identity, id_err = ia.identity_from_inspect(
        "media-executor", image_ref, inspect_data
    )
    if id_err or identity is None:
        summary = {
            "gate": "sec-08-executor-image-audit",
            "status": ia.STATUS_TECHNICAL,
            "error_code": id_err or "image_id_invalid",
            "scanned_at": scanned_at,
        }
        return summary, ia.EXIT_FAIL
    if identity.image_id != expected_image_id:
        summary = {
            "gate": "sec-08-executor-image-audit",
            "status": ia.STATUS_TECHNICAL,
            "error_code": "image_identity_drift",
            "expected_image_id": expected_image_id,
            "observed_image_id": identity.image_id,
            "scanned_at": scanned_at,
        }
        return summary, ia.EXIT_FAIL
    if identity.platform != expected_platform:
        summary = {
            "gate": "sec-08-executor-image-audit",
            "status": ia.STATUS_TECHNICAL,
            "error_code": "platform_mismatch",
            "expected_platform": expected_platform,
            "observed_platform": identity.platform,
            "scanned_at": scanned_at,
        }
        return summary, ia.EXIT_FAIL

    report_path = work_dir / "media-executor.trivy.json"
    rc, scan_err = ia.run_trivy_image_scan(
        trivy_bin=trivy_bin,
        cache_dir=cache_dir,
        image_id=identity.image_id,
        output_path=report_path,
    )
    if scan_err or rc != 0:
        summary = {
            "gate": "sec-08-executor-image-audit",
            "status": ia.STATUS_TECHNICAL,
            "error_code": scan_err or "scanner_bad_exit",
            "identity": asdict(identity),
            "scanned_at": scanned_at,
        }
        return summary, ia.EXIT_FAIL

    report, load_err = ia.load_trivy_report(report_path)
    if load_err or report is None:
        summary = {
            "gate": "sec-08-executor-image-audit",
            "status": ia.STATUS_TECHNICAL,
            "error_code": load_err or "report_missing",
            "identity": asdict(identity),
            "scanned_at": scanned_at,
        }
        return summary, ia.EXIT_FAIL

    details, comp_err = check_executor_completeness(
        report,
        expected_image_id=identity.image_id,
        expected_platform=expected_platform,
    )
    if comp_err:
        summary = {
            "gate": "sec-08-executor-image-audit",
            "status": ia.STATUS_TECHNICAL,
            "error_code": comp_err,
            "identity": asdict(identity),
            "completeness": details,
            "scanned_at": scanned_at,
        }
        return summary, ia.EXIT_FAIL

    findings = ia.extract_findings("media-executor", report)
    counts = ia.count_findings(findings)
    verdict = ia.policy_verdict_for_findings(findings)
    gate = [f for f in findings if f.severity in ia.GATE_SEVERITIES]
    fixable = [f for f in gate if f.has_fix]
    unfixed = [f for f in gate if not f.has_fix]

    def uniq(rows: list[Any]) -> list[str]:
        return sorted({f.vulnerability_id for f in rows})

    pcre = [
        ia.finding_to_summary(f)
        for f in findings
        if f.pkg_name == "libpcre2-8-0" and f.severity in ia.GATE_SEVERITIES
    ]
    summary = {
        "gate": "sec-08-executor-image-audit",
        "status": verdict,
        "scanned_at": scanned_at,
        "scanner": {
            "name": "trivy",
            "version": observed_ver,
            "required_version": ia.TRIVY_REQUIRED_VERSION,
            "scanners": ["vuln"],
            "telemetry": "disabled",
            "image_src": ["docker"],
            "offline_flags": [
                "--skip-db-update",
                "--skip-java-db-update",
                "--skip-check-update",
                "--offline-scan",
                "--image-src docker",
            ],
        },
        "db": db_evidence,
        "expected_platform": expected_platform,
        "policy": {
            "gate_severities": sorted(ia.GATE_SEVERITIES),
            "fixable_high_critical": "fail",
            "unfixed_high_critical": "needs_owner_decision",
            "other_severities": "report_only",
            "ignore_unfixed": False,
            "exceptions": [],
            "vex_applied": False,
        },
        "identity": asdict(identity),
        "completeness": details,
        "counts": counts,
        "gate_fixable_instances": len(fixable),
        "gate_unfixed_instances": len(unfixed),
        "gate_fixable_unique_ids": uniq(fixable),
        "gate_unfixed_unique_ids": uniq(unfixed),
        "libpcre2_gate_findings": pcre,
        "cve_2026_103111_present": any(
            f.vulnerability_id == "CVE-2026-103111" for f in findings
        ),
        "findings": [ia.finding_to_summary(f) for f in findings],
    }
    return summary, ia.overall_exit_code(verdict)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image-ref", required=True)
    parser.add_argument("--expected-image-id", required=True)
    parser.add_argument("--trivy-bin", required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--summary-out", type=Path, required=True)
    parser.add_argument("--expected-platform", default="linux/amd64")
    args = parser.parse_args(argv)
    summary, code = audit_image(
        image_ref=args.image_ref,
        expected_image_id=args.expected_image_id,
        trivy_bin=args.trivy_bin,
        cache_dir=args.cache_dir,
        work_dir=args.work_dir,
        expected_platform=args.expected_platform,
    )
    args.summary_out.parent.mkdir(parents=True, exist_ok=True)
    args.summary_out.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": summary.get("status"),
                "image_id": (summary.get("identity") or {}).get("image_id"),
                "fixable_unique": summary.get("gate_fixable_unique_ids"),
                "unfixed_unique_count": len(
                    summary.get("gate_unfixed_unique_ids") or []
                ),
                "cve_2026_103111_present": summary.get("cve_2026_103111_present"),
                "exit_code": code,
            },
            sort_keys=True,
        )
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
