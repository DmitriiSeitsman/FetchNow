#!/usr/bin/env python3
"""Candidate-7 aggregate image audit (SEC-03C/08/09).

Uses verified helpers from ``image_audit`` (Trivy 0.74.0 pins, offline scan argv,
DB download once, inspect/identity, findings/policy). Does **not** widen
``image_audit.REQUIRED_TARGETS`` (still exactly four). Scans seven distinct
final runtime images on one frozen advisory DB.

This is an audit snapshot for specific Image IDs — not residual acceptance,
not a production readiness claim, and not a live PostgreSQL identity proof.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import image_audit as ia  # noqa: E402
from fetchnow_release.bounded_subprocess import (  # noqa: E402
    BoundedSubprocessError,
    run_bounded,
)

GATE_NAME = "sec-03c-candidate7-image-audit"
BASELINE_NATIVE_SHA = "686369bd085bd073cb44bd791cb1c0e5beba8323"
NATIVE_ACCEPTANCE_RUN = "38627426197"

# Seven distinct final runtime images (api image also maps worker/delivery).
CANDIDATE7_TARGETS: tuple[str, ...] = (
    "api",
    "web",
    "gateway",
    "media-executor",
    "media-net-executor",
    "egress-proxy",
    "postgres",
)

TARGET_OS_FAMILY: dict[str, str] = {
    "api": "debian",
    "web": "alpine",
    "gateway": "alpine",
    "media-executor": "debian",
    "media-net-executor": "debian",
    "egress-proxy": "debian",
    "postgres": "alpine",
}

# Language packages must appear when inventory is known to carry them.
PYTHON_REQUIRED_TARGETS = frozenset({"api", "media-net-executor"})

ROLE_MAPPING: dict[str, tuple[str, ...]] = {
    "api": ("api", "worker", "delivery"),
    "web": ("web",),
    "gateway": ("gateway",),
    "media-executor": ("media-executor",),
    "media-net-executor": ("media-net-executor",),
    "egress-proxy": ("egress-proxy",),
    "postgres": ("postgres",),
}

POSTGRES_COMPOSE_REF = "postgres:16.15-alpine3.24"

RUNTIME_INPUT_PATHS: tuple[str, ...] = (
    "backend/Dockerfile",
    "backend/Dockerfile.media-executor",
    "backend/Dockerfile.media-net-executor",
    "backend/Dockerfile.media-egress-proxy",
    "web/Dockerfile",
    "deploy/nginx/Dockerfile",
    "compose.yaml",
)

INVENTORY_TIMEOUT_SECONDS = 45.0
Runner = Callable[..., Any]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_targets_file(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("targets file must be a JSON object")
    out: dict[str, str] = {}
    for name in CANDIDATE7_TARGETS:
        ref = data.get(name)
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError(f"targets file missing {name}")
        out[name] = ref.strip()
    extra = set(data) - set(CANDIDATE7_TARGETS)
    if extra:
        raise ValueError(f"unexpected targets: {sorted(extra)}")
    return out


def check_candidate7_completeness(
    target: str,
    report: Mapping[str, Any],
    *,
    expected_image_id: str,
    expected_platform: str,
    expected_repo_manifest_digests: Sequence[str] = (),
) -> tuple[dict[str, Any], str | None]:
    """Fail-closed completeness mirroring image_audit with candidate-7 profiles.

    Reuses identity/schema/OS-pkg detection patterns from ``image_audit`` but
    applies candidate-7 OS families and python requirements (api +
    media-net-executor). Does not mutate ``image_audit.TARGET_OS_FAMILY``.
    """
    details: dict[str, Any] = {}
    schema = report.get("SchemaVersion")
    details["schema_version"] = schema
    if not isinstance(schema, int) or schema < 2:
        return details, "report_incomplete"

    meta = report.get("Metadata")
    if not isinstance(meta, dict):
        return details, "report_incomplete"

    expected = ia.normalize_digest(expected_image_id)
    if expected is None:
        return details, "image_id_invalid"

    observed = ia.normalize_digest(str(meta.get("ImageID") or ""))
    details["report_image_id"] = observed
    details["expected_image_id"] = expected
    if observed is None:
        return details, "image_mismatch"

    repo_manifests = {
        d
        for d in (ia.normalize_digest(str(x)) for x in expected_repo_manifest_digests)
        if d is not None
    }
    if observed != expected:
        if observed in repo_manifests:
            details["image_id_error"] = "manifest_digest_not_image_id"
        return details, "image_mismatch"

    report_platform = None
    image_config = meta.get("ImageConfig")
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
    os_name = str(os_meta.get("Name") or "").strip()
    details["os_family"] = family or None
    details["os_name"] = os_name or None
    expected_family = TARGET_OS_FAMILY[target]
    details["expected_os_family"] = expected_family
    if family != expected_family:
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
        if class_name == "os-pkgs" or type_name in {
            "debian",
            "alpine",
            "ubuntu",
            "amazon",
            "centos",
            "rocky",
            "alma",
            "oracle",
            "redhat",
            "cbl-mariner",
            "photon",
            "wolfi",
            "chainguard",
            "busybox",
        }:
            os_pkg_seen = True
        if type_name in {"python-pkg", "pip", "poetry", "uv"} or (
            class_name == "lang-pkgs" and "python" in type_name
        ):
            python_seen = True

    details["result_types"] = sorted(set(result_types))
    details["os_pkgs_seen"] = os_pkg_seen
    details["python_seen"] = python_seen
    details["python_required"] = target in PYTHON_REQUIRED_TARGETS
    if not os_pkg_seen:
        return details, "os_surface_missing"
    if target in PYTHON_REQUIRED_TARGETS and not python_seen:
        return details, "python_surface_missing"
    return details, None


def evaluate_candidate7_report(
    target: str,
    report: Mapping[str, Any],
    *,
    expected_image_id: str,
    expected_platform: str,
    expected_repo_manifest_digests: Sequence[str] = (),
) -> ia.TargetResult:
    details, err = check_candidate7_completeness(
        target,
        report,
        expected_image_id=expected_image_id,
        expected_platform=expected_platform,
        expected_repo_manifest_digests=expected_repo_manifest_digests,
    )
    if err:
        return ia.TargetResult(
            name=target,
            status=ia.STATUS_TECHNICAL,
            details=details,
            error_code=err,
        )
    findings = ia.extract_findings(target, report)
    counts = ia.count_findings(findings)
    verdict = ia.policy_verdict_for_findings(findings)
    return ia.TargetResult(
        name=target,
        status=verdict,
        findings=findings,
        counts=counts,
        details=details,
    )


def db_metadata_hash(cache_dir: Path) -> str | None:
    path = ia.db_metadata_path(cache_dir)
    if not path.is_file():
        return None
    return sha256_file(path)


def collect_runtime_input_hashes(repo_root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in RUNTIME_INPUT_PATHS:
        path = repo_root / rel
        out[rel] = sha256_file(path) if path.is_file() else "MISSING"
    return out


def collect_inventory(
    image_id: str,
    target: str,
    *,
    runner: Runner = run_bounded,
) -> dict[str, Any]:
    """Bounded read-only probes; does not start production Compose services."""
    probes: dict[str, str] = {
        "uname": "uname -m",
        "python": "python3 --version 2>/dev/null || python --version 2>/dev/null || true",
        "openssl": "openssl version 2>/dev/null || true",
        "ffmpeg": "ffmpeg -version 2>/dev/null | head -n1 || true",
        "ffprobe": "ffprobe -version 2>/dev/null | head -n1 || true",
        "yt_dlp": "yt-dlp --version 2>/dev/null || /opt/venv/bin/yt-dlp --version 2>/dev/null || true",
        "nginx": "nginx -v 2>&1 || true",
        "postgres": "postgres --version 2>/dev/null || true",
        "pip": "python -m pip --version 2>/dev/null || pip --version 2>/dev/null || true",
        "ensurepip": "python -c 'import ensurepip' 2>/dev/null && echo present || echo absent",
    }
    script = " ; ".join(f'echo "__{key}__"; {cmd}' for key, cmd in probes.items())
    argv = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--entrypoint",
        "/bin/sh",
        image_id,
        "-c",
        script,
    ]
    try:
        result = runner(
            argv,
            timeout=INVENTORY_TIMEOUT_SECONDS,
            max_stdout_bytes=256 * 1024,
            max_stderr_bytes=64 * 1024,
        )
    except BoundedSubprocessError as exc:
        _, code = ia.classify_bounded_failure(exc)
        return {"ok": False, "error_code": code, "target": target}
    text = result.stdout_text
    parsed: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith("__") and line.endswith("__"):
            if current is not None:
                parsed[current] = "\n".join(buf).strip()
            current = line.strip("_")
            buf = []
        else:
            buf.append(line)
    if current is not None:
        parsed[current] = "\n".join(buf).strip()
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "target": target,
        "roles": list(ROLE_MAPPING.get(target, ())),
        "probes": {k: ia.redact_secrets(v)[:200] for k, v in parsed.items()},
    }


def summarize_findings(findings: Sequence[ia.Finding]) -> dict[str, Any]:
    instances = [
        ia.finding_to_summary(f) for f in findings if f.severity in ia.GATE_SEVERITIES
    ]
    unique_ids = sorted({f["id"] for f in instances})
    fixable = [f for f in instances if f.get("fixed_version")]
    unfixed = [f for f in instances if not f.get("fixed_version")]
    return {
        "high_critical_instances": len(instances),
        "high_critical_unique_ids": unique_ids,
        "unique_count": len(unique_ids),
        "fixable_instances": fixable,
        "unfixed_instances": unfixed,
        "fixable_count": len(fixable),
        "unfixed_count": len(unfixed),
    }


def reconcile_with_prior(
    *,
    current_ids: Sequence[str],
    prior_ids: Sequence[str] | None,
    comparable: bool,
    note: str,
) -> dict[str, Any]:
    if not comparable or prior_ids is None:
        return {
            "comparable": False,
            "note": note,
            "new": [],
            "retained": [],
            "gone": [],
        }
    cur = set(current_ids)
    prior = set(prior_ids)
    return {
        "comparable": True,
        "note": note,
        "new": sorted(cur - prior),
        "retained": sorted(cur & prior),
        "gone": sorted(prior - cur),
    }


def run_candidate7_gate(
    *,
    trivy_bin: str,
    cache_dir: Path,
    targets: Mapping[str, str],
    expected_platform: str,
    work_dir: Path,
    repo_root: Path,
    audited_source_sha: str,
    audit_commit_sha: str,
    baseline_sha: str = BASELINE_NATIVE_SHA,
    runner: Runner = run_bounded,
    host_platform_note: str | None = None,
    scan_timeout: float = ia.SCAN_TIMEOUT_SECONDS,
    collect_inventories: bool = True,
    prior_high_critical_ids: Sequence[str] | None = None,
) -> tuple[dict[str, Any], int]:
    scanned_at = _utc_now()
    work_dir.mkdir(parents=True, exist_ok=True)

    if set(targets) != set(CANDIDATE7_TARGETS):
        summary: dict[str, Any] = {
            "gate": GATE_NAME,
            "status": ia.STATUS_TECHNICAL,
            "error_code": "targets_invalid",
            "expected": list(CANDIDATE7_TARGETS),
            "observed": sorted(targets),
        }
        return summary, ia.EXIT_FAIL

    observed_ver, ver_err = ia.verify_trivy_version(trivy_bin, runner=runner)
    if ver_err:
        return (
            {
                "gate": GATE_NAME,
                "status": ia.STATUS_TECHNICAL,
                "error_code": ver_err,
                "expected_trivy": ia.TRIVY_REQUIRED_VERSION,
                "observed_trivy": observed_ver,
            },
            ia.EXIT_FAIL,
        )

    # One advisory DB download for the whole series.
    db_evidence, db_err = ia.download_advisory_db(trivy_bin, cache_dir, runner=runner)
    if db_err:
        return (
            {
                "gate": GATE_NAME,
                "status": ia.STATUS_TECHNICAL,
                "error_code": db_err,
                "db": db_evidence,
                "scanner": {"name": "trivy", "version": observed_ver},
            },
            ia.EXIT_FAIL,
        )
    db_hash = db_metadata_hash(cache_dir)
    db_row = dict(db_evidence)
    db_row["metadata_sha256"] = db_hash

    input_hashes = collect_runtime_input_hashes(repo_root)
    target_results: list[ia.TargetResult] = []
    inventories: dict[str, Any] = {}
    identity_before_after: dict[str, Any] = {}

    for name in CANDIDATE7_TARGETS:
        reference = targets[name]
        inspect_data, insp_err = ia.inspect_local_image(reference, runner=runner)
        if insp_err or inspect_data is None:
            target_results.append(
                ia.TargetResult(
                    name=name,
                    status=ia.STATUS_TECHNICAL,
                    identity={"reference": reference},
                    error_code=insp_err or "inspect_failed",
                )
            )
            continue
        identity, id_err = ia.identity_from_inspect(name, reference, inspect_data)
        if id_err or identity is None:
            target_results.append(
                ia.TargetResult(
                    name=name,
                    status=ia.STATUS_TECHNICAL,
                    identity={"reference": reference},
                    error_code=id_err or "image_id_invalid",
                )
            )
            continue
        if identity.platform != expected_platform:
            target_results.append(
                ia.TargetResult(
                    name=name,
                    status=ia.STATUS_TECHNICAL,
                    identity=asdict(identity),
                    error_code="platform_mismatch",
                    details={
                        "expected_platform": expected_platform,
                        "observed_platform": identity.platform,
                    },
                )
            )
            continue

        pre_id = identity.image_id
        report_path = work_dir / f"{name}.trivy.json"
        rc, scan_err = ia.run_trivy_image_scan(
            trivy_bin=trivy_bin,
            cache_dir=cache_dir,
            image_id=identity.image_id,
            output_path=report_path,
            runner=runner,
            timeout=scan_timeout,
        )
        # Re-inspect after scan — Image ID must be stable.
        inspect_after, after_err = ia.inspect_local_image(reference, runner=runner)
        post_id = None
        if inspect_after and not after_err:
            ident_after, _ = ia.identity_from_inspect(name, reference, inspect_after)
            post_id = None if ident_after is None else ident_after.image_id
        identity_before_after[name] = {
            "before": pre_id,
            "after": post_id,
            "stable": pre_id == post_id and post_id is not None,
        }
        if post_id != pre_id:
            target_results.append(
                ia.TargetResult(
                    name=name,
                    status=ia.STATUS_TECHNICAL,
                    identity=asdict(identity),
                    error_code="image_id_changed_during_scan",
                    details=identity_before_after[name],
                )
            )
            continue
        if scan_err:
            target_results.append(
                ia.TargetResult(
                    name=name,
                    status=ia.STATUS_TECHNICAL,
                    identity=asdict(identity),
                    error_code=scan_err,
                )
            )
            continue
        if rc != 0:
            target_results.append(
                ia.TargetResult(
                    name=name,
                    status=ia.STATUS_TECHNICAL,
                    identity=asdict(identity),
                    error_code="scanner_bad_exit",
                    details={"returncode": rc},
                )
            )
            continue

        report, load_err = ia.load_trivy_report(report_path)
        if load_err or report is None:
            target_results.append(
                ia.TargetResult(
                    name=name,
                    status=ia.STATUS_TECHNICAL,
                    identity=asdict(identity),
                    error_code=load_err or "report_missing",
                )
            )
            continue

        evaluated = evaluate_candidate7_report(
            name,
            report,
            expected_image_id=identity.image_id,
            expected_platform=expected_platform,
            expected_repo_manifest_digests=identity.repo_manifest_digests,
        )
        evaluated.identity = asdict(identity)
        evaluated.identity["roles"] = list(ROLE_MAPPING.get(name, ()))
        evaluated.details["report_path_basename"] = report_path.name
        evaluated.details["report_sha256"] = sha256_file(report_path)
        target_results.append(evaluated)

        if collect_inventories and evaluated.status != ia.STATUS_TECHNICAL:
            inventories[name] = collect_inventory(
                identity.image_id, name, runner=runner
            )

    names = {t.name for t in target_results}
    if len(target_results) != len(CANDIDATE7_TARGETS) or names != set(
        CANDIDATE7_TARGETS
    ):
        overall = ia.STATUS_TECHNICAL
    else:
        overall = ia.merge_overall_status([t.status for t in target_results])

    all_findings: list[ia.Finding] = []
    for tr in target_results:
        all_findings.extend(tr.findings)
    finding_summary = summarize_findings(all_findings)
    reconciliation = reconcile_with_prior(
        current_ids=finding_summary["high_critical_unique_ids"],
        prior_ids=list(prior_high_critical_ids) if prior_high_critical_ids else None,
        comparable=bool(prior_high_critical_ids),
        note=(
            "Prior HIGH/CRITICAL IDs supplied by caller"
            if prior_high_critical_ids
            else "No prior same-DB/same-Image-ID report pinned for this candidate7 run; "
            "not comparable without explanation"
        ),
    )

    base_summary = ia.build_safe_summary(
        overall=overall,
        trivy_version=observed_ver or ia.TRIVY_REQUIRED_VERSION,
        expected_platform=expected_platform,
        host_platform_note=host_platform_note,
        db=db_row,
        targets=target_results,
        scanned_at=scanned_at,
    )
    # Retarget gate identity and expand with candidate-7 evidence.
    base_summary["gate"] = GATE_NAME
    base_summary["artifact_kind"] = "NEW_AUDIT_ARTIFACT"
    base_summary["does_not_inherit_prior_image_ids"] = True
    base_summary["native_acceptance_history"] = {
        "run": NATIVE_ACCEPTANCE_RUN,
        "baseline_sha": baseline_sha,
        "verdict": "PASS",
        "note": "Native release matrix PASS; not image-security PASS",
    }
    base_summary["shas"] = {
        "baseline_native_sha": baseline_sha,
        "audited_source_sha": audited_source_sha,
        "audit_commit_sha": audit_commit_sha,
    }
    base_summary["role_mapping"] = {k: list(v) for k, v in ROLE_MAPPING.items()}
    base_summary["role_mapping_note"] = (
        "api Image ID is shared by worker and delivery roles; one scan covers all three"
    )
    base_summary["runtime_input_hashes"] = input_hashes
    base_summary["postgres"] = {
        "candidate_reference": POSTGRES_COMPOSE_REF,
        "candidate_scan": "included",
        "live_retained_postgresql": "NOT_VERIFIED",
        "note": (
            "Candidate Compose pin scanned only. Live retained PostgreSQL identity "
            "requires separate SSH/operator evidence; rollout previously assumed "
            "PostgreSQL unchanged."
        ),
    }
    base_summary["identity_before_after"] = identity_before_after
    base_summary["inventories"] = inventories
    base_summary["high_critical"] = finding_summary
    base_summary["reconciliation"] = reconciliation
    base_summary["vex_exceptions_applied"] = []
    base_summary["remediation_proposals"] = _remediation_proposals(finding_summary)
    base_summary["verdicts"] = {
        "native_acceptance_history": "PASS",
        "audit_execution_completeness": (
            "PASS" if overall != ia.STATUS_TECHNICAL else "FAIL"
        ),
        "image_security": overall,
        "residual_acceptance": "NOT_GRANTED",
        "production_readiness": "NO",
    }
    base_summary["policy"]["exceptions"] = []
    base_summary["policy"]["vex_applied"] = False

    text = ia.redact_secrets(json.dumps(base_summary, ensure_ascii=True))
    summary = json.loads(text)
    return summary, ia.overall_exit_code(overall)


def _remediation_proposals(finding_summary: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Minimal owner-facing proposals — not executed in this pass."""
    proposals: list[dict[str, Any]] = []
    for row in finding_summary.get("fixable_instances") or []:
        proposals.append(
            {
                "action": "upgrade_or_rebuild",
                "executed": False,
                "target": row.get("target"),
                "id": row.get("id"),
                "package": row.get("package"),
                "installed_version": row.get("installed_version"),
                "fixed_version": row.get("fixed_version"),
            }
        )
    for row in finding_summary.get("unfixed_instances") or []:
        proposals.append(
            {
                "action": "owner_decision_unfixed",
                "executed": False,
                "target": row.get("target"),
                "id": row.get("id"),
                "package": row.get("package"),
                "installed_version": row.get("installed_version"),
                "fixed_version": None,
            }
        )
    return proposals


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SEC-03C/08/09 candidate-7 Trivy image audit aggregate"
    )
    parser.add_argument("--trivy-bin", default="trivy")
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--targets-file", type=Path, required=True)
    parser.add_argument("--expected-platform", default=ia.DEFAULT_EXPECTED_PLATFORM)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--summary-out", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--audited-source-sha", required=True)
    parser.add_argument("--audit-commit-sha", required=True)
    parser.add_argument("--baseline-sha", default=BASELINE_NATIVE_SHA)
    parser.add_argument("--host-platform-note", default=None)
    parser.add_argument(
        "--scan-timeout-seconds", type=float, default=ia.SCAN_TIMEOUT_SECONDS
    )
    parser.add_argument(
        "--skip-inventory",
        action="store_true",
        help="Skip docker inventory probes (offline unit tests)",
    )
    parser.add_argument(
        "--prior-findings-file",
        type=Path,
        default=None,
        help="Optional JSON list of prior HIGH/CRITICAL vulnerability IDs",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        targets = parse_targets_file(args.targets_file)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {
                    "gate": GATE_NAME,
                    "status": ia.STATUS_TECHNICAL,
                    "error_code": "targets_invalid",
                    "message": str(exc),
                }
            ),
            file=sys.stderr,
        )
        return ia.EXIT_FAIL

    prior_ids: list[str] | None = None
    if args.prior_findings_file is not None:
        try:
            raw = json.loads(args.prior_findings_file.read_text(encoding="utf-8"))
            if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
                raise ValueError("prior findings must be a JSON string list")
            prior_ids = list(raw)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(
                json.dumps(
                    {
                        "gate": GATE_NAME,
                        "status": ia.STATUS_TECHNICAL,
                        "error_code": "prior_findings_invalid",
                        "message": str(exc),
                    }
                ),
                file=sys.stderr,
            )
            return ia.EXIT_FAIL

    summary, code = run_candidate7_gate(
        trivy_bin=args.trivy_bin,
        cache_dir=args.cache_dir,
        targets=targets,
        expected_platform=args.expected_platform,
        work_dir=args.work_dir,
        repo_root=args.repo_root,
        audited_source_sha=args.audited_source_sha,
        audit_commit_sha=args.audit_commit_sha,
        baseline_sha=args.baseline_sha,
        host_platform_note=args.host_platform_note,
        scan_timeout=args.scan_timeout_seconds,
        collect_inventories=not args.skip_inventory,
        prior_high_critical_ids=prior_ids,
    )
    args.summary_out.parent.mkdir(parents=True, exist_ok=True)
    args.summary_out.write_text(
        json.dumps(summary, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": summary.get("status"),
                "exit_code": code,
                "image_security": (summary.get("verdicts") or {}).get("image_security"),
                "residual_acceptance": "NOT_GRANTED",
                "production_readiness": "NO",
            }
        )
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
