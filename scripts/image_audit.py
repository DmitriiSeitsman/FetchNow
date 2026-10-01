#!/usr/bin/env python3
"""SEC-03C minimal image audit gate (Trivy vulnerability scans only).

Scans exactly four final runtime targets (api, web, gateway, postgres) by local
Docker Image ID. Policy and completeness live in this wrapper; Trivy findings
exit codes are not used as the policy verdict (``--exit-code`` stays 0).

This is a dated advisory-DB result for specific Image IDs/platforms — not a
promise that a rebuild of the same git SHA yields the same digest, and not
coverage of secrets, egress, host Nginx, or lockfile metadata (SEC-03A/B2).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from fetchnow_release.bounded_subprocess import (  # noqa: E402
    BoundedCancelledError,
    BoundedResult,
    BoundedSubprocessError,
    BoundedTimeoutError,
    OutputLimitExceededError,
    run_bounded,
)

# --- pins / budgets ------------------------------------------------------------
TRIVY_REQUIRED_VERSION = "0.74.0"
OCI_REVISION_LABEL = "org.opencontainers.image.revision"
REQUIRED_TARGETS = ("api", "web", "gateway", "postgres")

# Official release assets (https://github.com/aquasecurity/trivy/releases/tag/v0.74.0)
# Checksums taken from trivy_0.74.0_checksums.txt on that release. Sigstore
# bundles for the tarballs and checksums file are published alongside; CI pins
# SHA-256 of the binary archive (not curl|sh, not floating actions).
TRIVY_RELEASE_URL = (
    "https://github.com/aquasecurity/trivy/releases/download/v0.74.0"
)
TRIVY_CHECKSUMS: dict[str, str] = {
    "trivy_0.74.0_Linux-64bit.tar.gz": (
        "2ae6fe3ee734b7fdf11335663e18c75ea12dccc76062f09f164a3b0f8be4371a"
    ),
    "trivy_0.74.0_Linux-ARM64.tar.gz": (
        "b94ce1976bbf3c15b514b605ee88be7c6d94a29be2302847ff01cb794d47aad5"
    ),
    "trivy_0.74.0_macOS-ARM64.tar.gz": (
        "1caada5e0e2091909357c7525d3aa76f4b660b13821bc143b190c7483e31cc11"
    ),
    "trivy_0.74.0_macOS-64bit.tar.gz": (
        "472816f6888dda689d075c30254d4210b4d1035acf365aa72332f584c2f60485"
    ),
}

DEFAULT_EXPECTED_PLATFORM = "linux/amd64"

DB_DOWNLOAD_TIMEOUT_SECONDS = 180.0
VERSION_TIMEOUT_SECONDS = 15.0
INSPECT_TIMEOUT_SECONDS = 20.0
SCAN_TIMEOUT_SECONDS = 300.0
STDERR_LIMIT_BYTES = 2 * 1024 * 1024
MAX_REPORT_BYTES = 32 * 1024 * 1024
# Freshness: UpdatedAt must exist; DownloadedAt must be within this window of now.
DB_MAX_AGE = timedelta(hours=48)

# Expected OS families (Trivy Metadata.OS.Family).
TARGET_OS_FAMILY: dict[str, str] = {
    "api": "debian",
    "web": "alpine",
    "gateway": "alpine",
    "postgres": "alpine",
}

GATE_SEVERITIES = frozenset({"HIGH", "CRITICAL"})
# Vendor statuses that are not treated as open vulnerabilities for the gate.
NON_ACTIONABLE_STATUS = frozenset({"not_affected"})

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_NEEDS_OWNER = "needs_owner_decision"
STATUS_TECHNICAL = "technical_failure"

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_NEEDS_OWNER = 2

Runner = Callable[..., BoundedResult]

_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_VERSION_RE = re.compile(
    r"Version:\s*(?P<ver>\d+\.\d+\.\d+)", re.IGNORECASE | re.MULTILINE
)


@dataclass(frozen=True)
class ImageIdentity:
    """Local scan identity.

    Fields are deliberately separate:

    - ``image_id`` / ``config_digest``: Docker ``inspect .Id`` (image config
      digest). This is the only value accepted as the local Image ID and the
      Trivy scan argument.
    - ``repo_digests``: raw ``RepoDigests`` entries from inspect (typically
      ``name@<manifest-digest>``). These are **not** Image IDs.
    - ``repo_manifest_digests``: digests extracted from ``repo_digests``.

    Registry **index** digest vs **platform manifest** digest are not invented
    here: Docker inspect does not label which RepoDigest is an index. Callers
    that need the index must obtain it from the registry and record it outside
    the Image ID field.
    """

    target: str
    reference: str
    image_id: str
    platform: str
    repo_digests: tuple[str, ...]
    revision_label: str | None
    config_digest: str
    repo_manifest_digests: tuple[str, ...]


@dataclass(frozen=True)
class Finding:
    target: str
    vulnerability_id: str
    pkg_name: str
    pkg_path: str | None
    installed_version: str | None
    fixed_version: str | None
    severity: str
    status: str | None
    title: str | None
    primary_url: str | None
    pkg_type: str | None
    class_name: str | None

    @property
    def has_fix(self) -> bool:
        return bool(self.fixed_version and str(self.fixed_version).strip())


@dataclass
class TargetResult:
    name: str
    status: str
    identity: dict[str, Any] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_rfc3339(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def classify_bounded_failure(exc: BaseException) -> tuple[str, str]:
    if isinstance(exc, BoundedTimeoutError):
        return STATUS_TECHNICAL, "scanner_timeout"
    if isinstance(exc, BoundedCancelledError):
        return STATUS_TECHNICAL, "scanner_cancelled"
    if isinstance(exc, OutputLimitExceededError):
        return STATUS_TECHNICAL, "scanner_output_limit"
    return STATUS_TECHNICAL, "bounded_error"


def trivy_asset_for_uname(machine: str, system: str) -> str:
    sys_name = system.lower()
    mach = machine.lower()
    if sys_name == "linux" and mach in {"x86_64", "amd64"}:
        return "trivy_0.74.0_Linux-64bit.tar.gz"
    if sys_name == "linux" and mach in {"aarch64", "arm64"}:
        return "trivy_0.74.0_Linux-ARM64.tar.gz"
    if sys_name == "darwin" and mach in {"arm64", "aarch64"}:
        return "trivy_0.74.0_macOS-ARM64.tar.gz"
    if sys_name == "darwin" and mach in {"x86_64", "amd64"}:
        return "trivy_0.74.0_macOS-64bit.tar.gz"
    raise ValueError(f"unsupported platform for Trivy pin: {system}/{machine}")


def parse_trivy_version(text: str) -> str | None:
    match = _VERSION_RE.search(text)
    return match.group("ver") if match else None


def verify_trivy_version(
    trivy_bin: str,
    *,
    runner: Runner = run_bounded,
) -> tuple[str | None, str | None]:
    try:
        result = runner(
            [trivy_bin, "version"],
            timeout=VERSION_TIMEOUT_SECONDS,
            max_stdout_bytes=64 * 1024,
            max_stderr_bytes=64 * 1024,
        )
    except (BoundedSubprocessError, OSError) as exc:
        _, code = classify_bounded_failure(exc)
        return None, code
    if result.returncode != 0:
        return None, "tool_version_unreadable"
    observed = parse_trivy_version(result.stdout_text + "\n" + result.stderr_text)
    if observed is None:
        return None, "tool_version_unreadable"
    if observed != TRIVY_REQUIRED_VERSION:
        return observed, "tool_version_mismatch"
    return observed, None


def db_metadata_path(cache_dir: Path) -> Path:
    return cache_dir / "db" / "metadata.json"


def load_db_metadata(cache_dir: Path) -> dict[str, Any] | None:
    path = db_metadata_path(cache_dir)
    if not path.is_file():
        return None
    try:
        size = path.stat().st_size
        if size <= 0 or size > 1024 * 1024:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def validate_db_metadata(
    meta: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
    max_age: timedelta = DB_MAX_AGE,
) -> tuple[dict[str, Any], str | None]:
    """Return sanitized DB evidence and an error code if unfit."""
    clock = now or _utc_now()
    if not meta:
        return {}, "db_metadata_missing"
    version = meta.get("Version")
    updated_raw = meta.get("UpdatedAt")
    downloaded_raw = meta.get("DownloadedAt")
    next_raw = meta.get("NextUpdate")
    updated = _parse_rfc3339(str(updated_raw)) if updated_raw else None
    downloaded = _parse_rfc3339(str(downloaded_raw)) if downloaded_raw else None
    next_update = _parse_rfc3339(str(next_raw)) if next_raw else None
    evidence: dict[str, Any] = {
        "version": version,
        "updated_at": updated.isoformat() if updated else None,
        "downloaded_at": downloaded.isoformat() if downloaded else None,
        "next_update": next_update.isoformat() if next_update else None,
    }
    if not isinstance(version, int) or version < 1:
        return evidence, "db_metadata_invalid"
    if updated is None:
        return evidence, "db_metadata_invalid"
    if downloaded is None:
        return evidence, "db_metadata_invalid"
    # DownloadedAt must be recent relative to this gate run (task-local cache).
    if downloaded > clock + timedelta(minutes=5):
        return evidence, "db_metadata_invalid"
    if clock - downloaded > max_age:
        return evidence, "db_stale"
    return evidence, None


def download_advisory_db(
    trivy_bin: str,
    cache_dir: Path,
    *,
    runner: Runner = run_bounded,
    timeout: float = DB_DOWNLOAD_TIMEOUT_SECONDS,
) -> tuple[dict[str, Any], str | None]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    argv = [
        trivy_bin,
        "--cache-dir",
        str(cache_dir),
        "--disable-telemetry",
        "--quiet",
        "image",
        "--download-db-only",
    ]
    try:
        result = runner(
            argv,
            timeout=timeout,
            max_stdout_bytes=STDERR_LIMIT_BYTES,
            max_stderr_bytes=STDERR_LIMIT_BYTES,
        )
    except BoundedSubprocessError as exc:
        _, code = classify_bounded_failure(exc)
        return {}, code
    if result.returncode != 0:
        return {}, "db_download_failed"
    meta = load_db_metadata(cache_dir)
    return validate_db_metadata(meta)


def inspect_local_image(
    reference: str,
    *,
    runner: Runner = run_bounded,
) -> tuple[dict[str, Any] | None, str | None]:
    argv = [
        "docker",
        "image",
        "inspect",
        reference,
        "--format",
        "{{json .}}",
    ]
    try:
        result = runner(
            argv,
            timeout=INSPECT_TIMEOUT_SECONDS,
            max_stdout_bytes=2 * 1024 * 1024,
            max_stderr_bytes=256 * 1024,
        )
    except BoundedSubprocessError as exc:
        _, code = classify_bounded_failure(exc)
        return None, code
    if result.returncode != 0:
        return None, "inspect_failed"
    try:
        payload = json.loads(result.stdout_text)
    except json.JSONDecodeError:
        return None, "inspect_malformed"
    if not isinstance(payload, dict):
        return None, "inspect_malformed"
    return payload, None


def normalize_digest(value: str) -> str | None:
    """Return ``sha256:<64 hex>`` or None when the token is not a digest."""
    text = value.strip()
    if not text:
        return None
    if not text.startswith("sha256:"):
        text = f"sha256:{text}"
    if not _IMAGE_ID_RE.fullmatch(text):
        return None
    return text


def digest_from_repo_digest_entry(entry: str) -> str | None:
    """Extract the digest from a ``name@sha256:…`` RepoDigest entry."""
    text = entry.strip()
    if "@" not in text:
        return normalize_digest(text)
    _, _, digest_part = text.rpartition("@")
    return normalize_digest(digest_part)


def identity_from_inspect(
    target: str,
    reference: str,
    inspect_data: Mapping[str, Any],
) -> tuple[ImageIdentity | None, str | None]:
    image_id = normalize_digest(str(inspect_data.get("Id") or ""))
    if image_id is None:
        return None, "image_id_invalid"
    os_name = str(inspect_data.get("Os") or "").strip()
    arch = str(inspect_data.get("Architecture") or "").strip()
    if not os_name or not arch:
        return None, "platform_missing"
    platform = f"{os_name}/{arch}"
    digests_raw = inspect_data.get("RepoDigests") or []
    if digests_raw is None:
        digests_raw = []
    if not isinstance(digests_raw, list):
        return None, "inspect_malformed"
    repo_digests = tuple(str(d) for d in digests_raw if isinstance(d, str) and d)
    repo_manifest_digests: list[str] = []
    for entry in repo_digests:
        digest = digest_from_repo_digest_entry(entry)
        if digest is None:
            return None, "repo_digest_invalid"
        if digest not in repo_manifest_digests:
            repo_manifest_digests.append(digest)
    # Note: for locally tagged images Docker often records RepoDigests as
    # ``name@<Id>`` where that digest equals the config Id. That is not an
    # error. A *different* registry manifest digest must never be used as
    # ``image_id`` (enforced when matching scanner Metadata.ImageID).
    labels = (inspect_data.get("Config") or {}).get("Labels") or {}
    if labels is None:
        labels = {}
    if not isinstance(labels, dict):
        return None, "inspect_malformed"
    revision = labels.get(OCI_REVISION_LABEL)
    revision_s = str(revision) if revision is not None else None
    return (
        ImageIdentity(
            target=target,
            reference=reference,
            image_id=image_id,
            platform=platform,
            repo_digests=repo_digests,
            revision_label=revision_s,
            config_digest=image_id,
            repo_manifest_digests=tuple(repo_manifest_digests),
        ),
        None,
    )


def trivy_scan_argv(
    *,
    trivy_bin: str,
    cache_dir: Path,
    image_id: str,
    output_path: Path,
    timeout_seconds: float,
) -> list[str]:
    """Build Trivy argv for a local-ID vulnerability-only offline scan.

    Network enrichment is disabled by combining:
    ``--skip-db-update``, ``--skip-java-db-update``, ``--skip-check-update``,
    ``--offline-scan``, and ``--image-src docker`` (no remote registry fallback).
    ``--offline-scan`` alone is not treated as a full network ban.
    """
    return [
        trivy_bin,
        "--cache-dir",
        str(cache_dir),
        "--timeout",
        f"{int(timeout_seconds)}s",
        "--disable-telemetry",
        "--quiet",
        "image",
        "--scanners",
        "vuln",
        "--pkg-types",
        "os,library",
        "--list-all-pkgs=false",
        "--skip-db-update",
        "--skip-java-db-update",
        "--skip-check-update",
        "--offline-scan",
        "--image-src",
        "docker",
        "--format",
        "json",
        "--output",
        str(output_path),
        # Findings must not flip Trivy exit — policy is applied in this wrapper.
        "--exit-code",
        "0",
        image_id,
    ]


def run_trivy_image_scan(
    *,
    trivy_bin: str,
    cache_dir: Path,
    image_id: str,
    output_path: Path,
    runner: Runner = run_bounded,
    timeout: float = SCAN_TIMEOUT_SECONDS,
) -> tuple[int | None, str | None]:
    argv = trivy_scan_argv(
        trivy_bin=trivy_bin,
        cache_dir=cache_dir,
        image_id=image_id,
        output_path=output_path,
        timeout_seconds=timeout,
    )
    try:
        result = runner(
            argv,
            timeout=timeout + 5.0,
            max_stdout_bytes=64 * 1024,
            max_stderr_bytes=STDERR_LIMIT_BYTES,
        )
    except BoundedSubprocessError as exc:
        _, code = classify_bounded_failure(exc)
        return None, code
    return result.returncode, None


def load_trivy_report(
    path: Path,
    *,
    max_bytes: int = MAX_REPORT_BYTES,
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        size = path.stat().st_size
    except OSError:
        return None, "report_missing"
    if size <= 0:
        return None, "report_empty"
    if size > max_bytes:
        return None, "report_too_large"
    # Read at most max_bytes + 1 to detect truncation without slurping unbounded.
    try:
        with path.open("rb") as fh:
            raw = fh.read(max_bytes + 1)
    except OSError:
        return None, "report_missing"
    if len(raw) > max_bytes:
        return None, "report_too_large"
    try:
        text = raw.decode("utf-8")
        payload = json.loads(text)
    except (UnicodeError, json.JSONDecodeError):
        return None, "report_malformed"
    if not isinstance(payload, dict):
        return None, "report_malformed"
    return payload, None


def _severity_of(vuln: Mapping[str, Any]) -> str:
    raw = vuln.get("Severity")
    if not isinstance(raw, str) or not raw.strip():
        return "UNKNOWN"
    return raw.strip().upper()


def extract_findings(target: str, report: Mapping[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    results = report.get("Results")
    if results is None:
        return findings
    if not isinstance(results, list):
        return findings
    for entry in results:
        if not isinstance(entry, dict):
            continue
        class_name = entry.get("Class") if isinstance(entry.get("Class"), str) else None
        pkg_type = entry.get("Type") if isinstance(entry.get("Type"), str) else None
        vulns = entry.get("Vulnerabilities")
        if vulns is None:
            continue
        if not isinstance(vulns, list):
            continue
        for vuln in vulns:
            if not isinstance(vuln, dict):
                continue
            status = vuln.get("Status")
            status_s = str(status).lower() if status is not None else None
            if status_s in NON_ACTIONABLE_STATUS:
                continue
            vid = vuln.get("VulnerabilityID")
            pkg = vuln.get("PkgName")
            if not isinstance(vid, str) or not vid.strip():
                continue
            if not isinstance(pkg, str) or not pkg.strip():
                continue
            fixed = vuln.get("FixedVersion")
            if isinstance(fixed, str) and fixed.strip():
                fixed_s: str | None = str(fixed).strip()
            else:
                fixed_s = None
            installed = vuln.get("InstalledVersion")
            installed_s = (
                str(installed) if isinstance(installed, str) and installed else None
            )
            pkg_path = vuln.get("PkgPath")
            if isinstance(pkg_path, str) and pkg_path:
                pkg_path_s: str | None = str(pkg_path)
            else:
                pkg_path_s = None
            title = vuln.get("Title")
            title_s = str(title) if isinstance(title, str) else None
            url = vuln.get("PrimaryURL")
            url_s = str(url) if isinstance(url, str) else None
            findings.append(
                Finding(
                    target=target,
                    vulnerability_id=vid.strip(),
                    pkg_name=pkg.strip(),
                    pkg_path=pkg_path_s,
                    installed_version=installed_s,
                    fixed_version=fixed_s,
                    severity=_severity_of(vuln),
                    status=status_s,
                    title=title_s,
                    primary_url=url_s,
                    pkg_type=pkg_type,
                    class_name=class_name,
                )
            )
    return findings


def check_report_completeness(
    target: str,
    report: Mapping[str, Any],
    *,
    expected_image_id: str,
    expected_platform: str,
    expected_repo_manifest_digests: Sequence[str] = (),
) -> tuple[dict[str, Any], str | None]:
    """Prove analysis ran. Empty Vulnerabilities is allowed; missing analysis is not.

    Local Docker ``inspect .Id`` is the sole Image ID authority. A scanner
    Metadata.ImageID that matches a RepoDigest/manifest digest but not the
    inspect Id is ``image_mismatch`` — never a guessed PASS.
    """
    details: dict[str, Any] = {}
    schema = report.get("SchemaVersion")
    details["schema_version"] = schema
    if not isinstance(schema, int) or schema < 2:
        return details, "report_incomplete"

    meta = report.get("Metadata")
    if not isinstance(meta, dict):
        return details, "report_incomplete"

    expected = normalize_digest(expected_image_id)
    if expected is None:
        return details, "image_id_invalid"

    image_id_raw = str(meta.get("ImageID") or "")
    observed = normalize_digest(image_id_raw)
    details["report_image_id"] = observed
    details["expected_image_id"] = expected
    if observed is None:
        return details, "image_mismatch"

    repo_manifests = {
        d
        for d in (normalize_digest(str(x)) for x in expected_repo_manifest_digests)
        if d is not None
    }
    # Manifest/index digests may coexist with config digest in evidence, but the
    # scanner ImageID must equal the local inspect Id — not a RepoDigest stand-in.
    if observed != expected:
        if observed in repo_manifests:
            details["image_id_error"] = "manifest_digest_not_image_id"
        return details, "image_mismatch"

    # Platform: prefer Metadata.ImageConfig when present.
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
        # Missing Vulnerabilities key is OK; null / non-list is not.
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
    if not os_pkg_seen:
        return details, "os_surface_missing"
    if target == "api" and not python_seen:
        return details, "python_surface_missing"
    return details, None


def count_findings(findings: Sequence[Finding]) -> dict[str, int]:
    counts = {
        "total": len(findings),
        "critical": 0,
        "high": 0,
        "medium": 0,
        "low": 0,
        "unknown": 0,
        "gate_fixable": 0,
        "gate_unfixed": 0,
    }
    for finding in findings:
        key = finding.severity.lower()
        if key in counts:
            counts[key] += 1
        else:
            counts["unknown"] += 1
        if finding.severity in GATE_SEVERITIES:
            if finding.has_fix:
                counts["gate_fixable"] += 1
            else:
                counts["gate_unfixed"] += 1
    return counts


def policy_verdict_for_findings(findings: Sequence[Finding]) -> str:
    """Return pass / fail / needs_owner_decision for one target's findings."""
    has_fixable = False
    has_unfixed_gate = False
    for finding in findings:
        if finding.severity not in GATE_SEVERITIES:
            continue
        if finding.has_fix:
            has_fixable = True
        else:
            has_unfixed_gate = True
    if has_fixable:
        return STATUS_FAIL
    if has_unfixed_gate:
        return STATUS_NEEDS_OWNER
    return STATUS_PASS


def finding_to_summary(finding: Finding) -> dict[str, Any]:
    return {
        "target": finding.target,
        "id": finding.vulnerability_id,
        "package": finding.pkg_name,
        "installed_version": finding.installed_version,
        "fixed_version": finding.fixed_version,
        "severity": finding.severity,
        "status": finding.status,
        "pkg_type": finding.pkg_type,
        "title": finding.title,
        "primary_url": finding.primary_url,
    }


def evaluate_trivy_report(
    target: str,
    report: Mapping[str, Any],
    *,
    expected_image_id: str,
    expected_platform: str,
    expected_repo_manifest_digests: Sequence[str] = (),
) -> TargetResult:
    details, err = check_report_completeness(
        target,
        report,
        expected_image_id=expected_image_id,
        expected_platform=expected_platform,
        expected_repo_manifest_digests=expected_repo_manifest_digests,
    )
    if err:
        return TargetResult(
            name=target,
            status=STATUS_TECHNICAL,
            details=details,
            error_code=err,
        )
    findings = extract_findings(target, report)
    counts = count_findings(findings)
    verdict = policy_verdict_for_findings(findings)
    return TargetResult(
        name=target,
        status=verdict,
        findings=findings,
        counts=counts,
        details=details,
    )


def merge_overall_status(statuses: Sequence[str]) -> str:
    if any(s == STATUS_TECHNICAL for s in statuses):
        return STATUS_TECHNICAL
    if any(s == STATUS_FAIL for s in statuses):
        return STATUS_FAIL
    if any(s == STATUS_NEEDS_OWNER for s in statuses):
        return STATUS_NEEDS_OWNER
    if statuses and all(s == STATUS_PASS for s in statuses):
        return STATUS_PASS
    return STATUS_TECHNICAL


def overall_exit_code(status: str) -> int:
    if status == STATUS_PASS:
        return EXIT_PASS
    if status == STATUS_NEEDS_OWNER:
        return EXIT_NEEDS_OWNER
    return EXIT_FAIL


def build_safe_summary(
    *,
    overall: str,
    trivy_version: str,
    expected_platform: str,
    host_platform_note: str | None,
    db: Mapping[str, Any],
    targets: Sequence[TargetResult],
    scanned_at: datetime,
) -> dict[str, Any]:
    """CI-publishable summary — no layer history, env, or raw logs."""
    target_rows: list[dict[str, Any]] = []
    all_findings: list[dict[str, Any]] = []
    for tr in targets:
        target_rows.append(
            {
                "name": tr.name,
                "status": tr.status,
                "error_code": tr.error_code,
                "identity": tr.identity,
                "counts": tr.counts,
                "details": {
                    k: tr.details.get(k)
                    for k in (
                        "schema_version",
                        "os_family",
                        "os_name",
                        "os_pkgs_seen",
                        "python_seen",
                        "result_types",
                        "report_platform",
                        "report_image_id",
                        "expected_image_id",
                        "image_id_error",
                    )
                    if k in tr.details
                },
            }
        )
        for finding in tr.findings:
            all_findings.append(finding_to_summary(finding))
    return {
        "gate": "sec-03c-image-audit",
        "scanned_at": scanned_at.isoformat(),
        "status": overall,
        "exit_code_contract": {
            "pass": EXIT_PASS,
            "fail_or_technical": EXIT_FAIL,
            "needs_owner_decision": EXIT_NEEDS_OWNER,
        },
        "scanner": {
            "name": "trivy",
            "version": trivy_version,
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
        "db": dict(db),
        "expected_platform": expected_platform,
        "host_platform_note": host_platform_note,
        "targets": target_rows,
        "findings": all_findings,
        "policy": {
            "gate_severities": sorted(GATE_SEVERITIES),
            "fixable_high_critical": "fail",
            "unfixed_high_critical": "needs_owner_decision",
            "other_severities": "report_only",
            "ignore_unfixed": False,
            "exceptions": [],
        },
    }


def redact_secrets(text: str) -> str:
    """Best-effort scrub of common secret-shaped tokens from free text."""
    patterns = [
        re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*\S+"),
        re.compile(r"(?i)bearer\s+[a-z0-9._\-]+"),
        re.compile(r"ghp_[A-Za-z0-9]{20,}"),
        re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    ]
    out = text
    for pat in patterns:
        out = pat.sub("[redacted]", out)
    return out


def parse_targets_file(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("targets file must be a JSON object")
    out: dict[str, str] = {}
    for name in REQUIRED_TARGETS:
        ref = data.get(name)
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError(f"targets file missing {name}")
        out[name] = ref.strip()
    extra = set(data) - set(REQUIRED_TARGETS)
    if extra:
        raise ValueError(f"unexpected targets: {sorted(extra)}")
    return out


def run_gate(
    *,
    trivy_bin: str,
    cache_dir: Path,
    targets: Mapping[str, str],
    expected_platform: str,
    work_dir: Path,
    runner: Runner = run_bounded,
    host_platform_note: str | None = None,
    scan_timeout: float = SCAN_TIMEOUT_SECONDS,
) -> tuple[dict[str, Any], int]:
    scanned_at = _utc_now()
    work_dir.mkdir(parents=True, exist_ok=True)

    if set(targets) != set(REQUIRED_TARGETS):
        summary: dict[str, Any] = {
            "gate": "sec-03c-image-audit",
            "status": STATUS_TECHNICAL,
            "error_code": "targets_invalid",
            "expected": list(REQUIRED_TARGETS),
            "observed": sorted(targets),
        }
        return summary, EXIT_FAIL

    observed_ver, ver_err = verify_trivy_version(trivy_bin, runner=runner)
    if ver_err:
        summary = {
            "gate": "sec-03c-image-audit",
            "status": STATUS_TECHNICAL,
            "error_code": ver_err,
            "expected_trivy": TRIVY_REQUIRED_VERSION,
            "observed_trivy": observed_ver,
        }
        return summary, EXIT_FAIL

    db_evidence, db_err = download_advisory_db(
        trivy_bin, cache_dir, runner=runner
    )
    if db_err:
        summary = {
            "gate": "sec-03c-image-audit",
            "status": STATUS_TECHNICAL,
            "error_code": db_err,
            "db": db_evidence,
            "scanner": {"name": "trivy", "version": observed_ver},
        }
        return summary, EXIT_FAIL

    target_results: list[TargetResult] = []
    for name in REQUIRED_TARGETS:
        reference = targets[name]
        inspect_data, insp_err = inspect_local_image(reference, runner=runner)
        if insp_err or inspect_data is None:
            target_results.append(
                TargetResult(
                    name=name,
                    status=STATUS_TECHNICAL,
                    identity={"reference": reference},
                    error_code=insp_err or "inspect_failed",
                )
            )
            continue
        identity, id_err = identity_from_inspect(name, reference, inspect_data)
        if id_err or identity is None:
            target_results.append(
                TargetResult(
                    name=name,
                    status=STATUS_TECHNICAL,
                    identity={"reference": reference},
                    error_code=id_err or "image_id_invalid",
                )
            )
            continue
        if identity.platform != expected_platform:
            target_results.append(
                TargetResult(
                    name=name,
                    status=STATUS_TECHNICAL,
                    identity=asdict(identity),
                    error_code="platform_mismatch",
                    details={
                        "expected_platform": expected_platform,
                        "observed_platform": identity.platform,
                    },
                )
            )
            continue

        report_path = work_dir / f"{name}.trivy.json"
        rc, scan_err = run_trivy_image_scan(
            trivy_bin=trivy_bin,
            cache_dir=cache_dir,
            image_id=identity.image_id,
            output_path=report_path,
            runner=runner,
            timeout=scan_timeout,
        )
        if scan_err:
            target_results.append(
                TargetResult(
                    name=name,
                    status=STATUS_TECHNICAL,
                    identity=asdict(identity),
                    error_code=scan_err,
                )
            )
            continue
        if rc != 0:
            target_results.append(
                TargetResult(
                    name=name,
                    status=STATUS_TECHNICAL,
                    identity=asdict(identity),
                    error_code="scanner_bad_exit",
                    details={"returncode": rc},
                )
            )
            continue

        report, load_err = load_trivy_report(report_path)
        if load_err or report is None:
            target_results.append(
                TargetResult(
                    name=name,
                    status=STATUS_TECHNICAL,
                    identity=asdict(identity),
                    error_code=load_err or "report_missing",
                )
            )
            continue

        evaluated = evaluate_trivy_report(
            name,
            report,
            expected_image_id=identity.image_id,
            expected_platform=expected_platform,
            expected_repo_manifest_digests=identity.repo_manifest_digests,
        )
        evaluated.identity = asdict(identity)
        target_results.append(evaluated)

    names = {t.name for t in target_results}
    if len(target_results) != len(REQUIRED_TARGETS) or names != set(REQUIRED_TARGETS):
        overall = STATUS_TECHNICAL
    else:
        overall = merge_overall_status([t.status for t in target_results])

    summary = build_safe_summary(
        overall=overall,
        trivy_version=observed_ver or TRIVY_REQUIRED_VERSION,
        expected_platform=expected_platform,
        host_platform_note=host_platform_note,
        db=db_evidence,
        targets=target_results,
        scanned_at=scanned_at,
    )
    # Defense: never emit obvious secret-shaped leftovers from titles/URLs.
    summary_text = redact_secrets(json.dumps(summary, ensure_ascii=True))
    summary = json.loads(summary_text)
    return summary, overall_exit_code(overall)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SEC-03C minimal Trivy image audit gate"
    )
    parser.add_argument(
        "--trivy-bin",
        default="trivy",
        help=f"Path to Trivy {TRIVY_REQUIRED_VERSION} binary",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        required=True,
        help="Task-local Trivy cache directory (DB + scan cache)",
    )
    parser.add_argument(
        "--targets-file",
        type=Path,
        required=True,
        help="JSON object mapping api/web/gateway/postgres → local image ref",
    )
    parser.add_argument(
        "--expected-platform",
        default=DEFAULT_EXPECTED_PLATFORM,
        help="Required docker inspect platform (default linux/amd64)",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        required=True,
        help="Directory for per-target Trivy JSON reports",
    )
    parser.add_argument(
        "--summary-out",
        type=Path,
        required=True,
        help="Path for sanitized JSON summary",
    )
    parser.add_argument(
        "--host-platform-note",
        default=None,
        help="Optional note when host arch differs from evidence platform claims",
    )
    parser.add_argument(
        "--scan-timeout-seconds",
        type=float,
        default=SCAN_TIMEOUT_SECONDS,
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
                    "gate": "sec-03c-image-audit",
                    "status": STATUS_TECHNICAL,
                    "error_code": "targets_invalid",
                    "message": str(exc),
                }
            ),
            file=sys.stderr,
        )
        return EXIT_FAIL

    summary, code = run_gate(
        trivy_bin=args.trivy_bin,
        cache_dir=args.cache_dir,
        targets=targets,
        expected_platform=args.expected_platform,
        work_dir=args.work_dir,
        host_platform_note=args.host_platform_note,
        scan_timeout=args.scan_timeout_seconds,
    )
    args.summary_out.parent.mkdir(parents=True, exist_ok=True)
    args.summary_out.write_text(
        json.dumps(summary, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": summary.get("status"), "exit_code": code}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
