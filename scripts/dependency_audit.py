#!/usr/bin/env python3
"""SEC-03B2 dependency audit gate (npm + Python).

Full gate always runs exactly three sections:
  * npm — web/package-lock.json via ``npm audit --json --ignore-scripts --package-lock-only``
  * python-runtime — backend/uv.lock → marker-filtered export → pip-audit 2.10.1 (PyPI)
  * python-dev — same with ``--extra dev`` for the documented 3.12 target

JSON for pip-audit is read only from bounded stdout (``-f json``), never from an
alternate file fallback. Exit 0 only when all three sections succeed and every
finding is covered by a currently valid, narrowly scoped exception.

This is a dated advisory-service result, not a guarantee that advisory databases
are complete or that unknown vulnerabilities are absent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

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

try:
    from packaging.markers import default_environment
    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.utils import canonicalize_name
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "dependency_audit requires the 'packaging' package "
        "(install scripts/requirements/dependency-audit.txt into an isolated venv)"
    ) from exc

# --- budgets -------------------------------------------------------------------
NPM_TIMEOUT_SECONDS = 60.0
PYTHON_AUDIT_TIMEOUT_SECONDS = 90.0
UV_LOCK_CHECK_TIMEOUT_SECONDS = 30.0
UV_EXPORT_TIMEOUT_SECONDS = 60.0
OUTPUT_LIMIT_BYTES = 2 * 1024 * 1024

PIP_AUDIT_VERSION = "2.10.1"
UV_REQUIRED_VERSION = "0.12.19"
LOCAL_PROJECT_NAMES = frozenset({"fetchnow"})

# Documented successful / findings exit codes (not technical failure).
NPM_OK_EXIT = frozenset({0, 1})
PIP_AUDIT_OK_EXIT = frozenset({0, 1})

REQUIRED_SECTIONS = ("npm", "python-runtime", "python-dev")
SECTION_SCOPES = frozenset(REQUIRED_SECTIONS)
ECOSYSTEMS = frozenset({"npm", "pypi"})

RUNTIME_TARGET: dict[str, Any] = {
    "label": "python-runtime",
    "sys_platform": "linux",
    "platform_system": "Linux",
    "platform_machine": "x86_64",
    "platform_python_implementation": "CPython",
    "python_version": "3.14",
    "python_full_version": "3.14.7",
    "implementation_name": "cpython",
    "implementation_version": "3.14.7",
    "extra_dev": False,
    # Export interpreter must match target when UV_PYTHON_DOWNLOADS=never.
    "uv_python": "3.14.7",
}
DEV_TARGET: dict[str, Any] = {
    "label": "python-dev",
    "sys_platform": "linux",
    "platform_system": "Linux",
    "platform_machine": "x86_64",
    "platform_python_implementation": "CPython",
    # Documented semantics: markers use 3.12.0; uv export requests 3.12
    # (CI setup-python installs 3.12.x; patch is not locked to a single build).
    "python_version": "3.12",
    "python_full_version": "3.12.0",
    "implementation_name": "cpython",
    "implementation_version": "3.12.0",
    "extra_dev": True,
    "uv_python": "3.12",
}

Runner = Callable[..., BoundedResult]

STATUS_CLEAN = "clean"
STATUS_FINDINGS = "findings"
STATUS_TIMEOUT = "timeout"
STATUS_CANCELLED = "cancelled"
STATUS_SOURCE_UNAVAILABLE = "source_unavailable"
STATUS_SCANNER_ERROR = "scanner_error"
STATUS_MALFORMED = "malformed"
STATUS_INCOMPLETE = "incomplete"

ERROR_CODES = frozenset(
    {
        "missing_lock",
        "lock_stale",
        "export_failed",
        "export_empty",
        "export_parse_error",
        "export_silent_loss",
        "scanner_timeout",
        "scanner_cancelled",
        "scanner_output_limit",
        "scanner_empty_output",
        "scanner_bad_exit",
        "scanner_exit_payload_mismatch",
        "json_malformed",
        "json_incomplete",
        "inventory_incomplete",
        "exceptions_malformed",
        "sections_invalid",
        "tool_missing",
        "tool_version_mismatch",
        "tool_version_unreadable",
        "bounded_error",
    }
)


@dataclass(frozen=True)
class Finding:
    ecosystem: str
    section: str
    package: str
    version: str | None
    advisory_id: str
    aliases: tuple[str, ...] = ()
    severity: str | None = None

    @property
    def match_ids(self) -> frozenset[str]:
        return frozenset({self.advisory_id, *self.aliases})


@dataclass(frozen=True)
class ExceptionRule:
    ecosystem: str
    scope: str
    advisory: str
    package: str
    versions: tuple[str, ...]
    reason: str
    owner: str
    review_deadline: date
    aliases: tuple[str, ...] = ()

    @property
    def match_ids(self) -> frozenset[str]:
        return frozenset({self.advisory, *self.aliases})

    def is_active(self, *, today: date) -> bool:
        return today <= self.review_deadline

    def covers(self, finding: Finding, *, today: date) -> bool:
        if not self.is_active(today=today):
            return False
        if finding.ecosystem != self.ecosystem:
            return False
        if finding.section != self.scope:
            return False
        if finding.ecosystem == "pypi":
            if _normalize_py_name(finding.package) != _normalize_py_name(self.package):
                return False
        else:
            if finding.package != self.package:
                return False
        if not self.versions:
            return False
        if finding.version is None or finding.version not in self.versions:
            return False
        return bool(finding.match_ids & self.match_ids)


@dataclass
class SectionResult:
    name: str
    status: str
    findings: list[Finding] = field(default_factory=list)
    raw_findings: list[Finding] = field(default_factory=list)
    accepted_exceptions: list[dict[str, Any]] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _normalize_py_name(name: str) -> str:
    return canonicalize_name(name)


def _safe_error(
    code: str,
    *,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if code not in ERROR_CODES:
        code = "bounded_error"
    out: dict[str, Any] = {"error_code": code}
    if context:
        # Only allowlisted scalar context keys; values sanitized.
        safe: dict[str, Any] = {}
        for key, value in context.items():
            if key not in {
                "returncode",
                "section",
                "package",
                "field",
                "path",
                "bytes",
                "limit",
                "reason",
                "tool",
                "expected",
                "observed",
                "failure_stage",
            }:
                continue
            if isinstance(value, (int, float, bool)) or value is None:
                safe[key] = value
            elif isinstance(value, str):
                safe[key] = _sanitize_public_text(value, limit=120)
            else:
                safe[key] = type(value).__name__
        out["error_context"] = safe
    return out


_SECRET_PATTERNS = (
    re.compile(r"(?i)(password|passwd|pwd|token|secret|api[_-]?key)\s*[:=]\s*\S+"),
    re.compile(r"(?i)(authorization:\s*bearer\s+)\S+"),
    re.compile(r"(?i)://([^/@\s]+):([^/@\s]+)@"),
    re.compile(r"(?i)([?&](?:token|access_token|auth|key|password)=)[^&\s]+"),
)


def _sanitize_public_text(text: str, *, limit: int = 240) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    for pat in _SECRET_PATTERNS:
        cleaned = pat.sub(_secret_repl, cleaned)
    cleaned = _redact_url_credentials(cleaned)
    if len(cleaned) > limit:
        return cleaned[: limit - 3] + "..."
    return cleaned


def _secret_repl(match: re.Match[str]) -> str:
    text = match.group(0)
    if "://" in text and "@" in text:
        scheme, _, rest = text.partition("://")
        after_at = rest.split("@", 1)[-1]
        return f"{scheme}://***:***@{after_at}"
    if match.lastindex and match.lastindex >= 1:
        return f"{match.group(1)}***"
    return "***"


def _redact_url_credentials(text: str) -> str:
    def redact_one(url: str) -> str:
        try:
            parts = urlsplit(url)
        except ValueError:
            return "***"
        if parts.username or parts.password:
            host = parts.hostname or ""
            if parts.port:
                host = f"{host}:{parts.port}"
            netloc = f"***:***@{host}" if host else "***"
            query = parts.query
            if query:
                q_parts = []
                for item in query.split("&"):
                    if "=" in item:
                        k, _, _v = item.partition("=")
                        if k.lower() in {
                            "token",
                            "access_token",
                            "auth",
                            "key",
                            "password",
                            "secret",
                        }:
                            q_parts.append(f"{k}=***")
                        else:
                            q_parts.append(item)
                    else:
                        q_parts.append(item)
                query = "&".join(q_parts)
            return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))
        return url

    return re.sub(r"https?://[^\s\"']+", lambda m: redact_one(m.group(0)), text)


def _git_head(repo: Path, runner: Runner) -> str | None:
    try:
        result = runner(
            ["git", "rev-parse", "HEAD"],
            timeout=10.0,
            cwd=repo,
            max_stdout_bytes=4096,
            max_stderr_bytes=4096,
        )
    except BoundedSubprocessError:
        return None
    if result.returncode != 0:
        return None
    text = result.stdout_text.strip()
    return text if re.fullmatch(r"[0-9a-f]{40}", text) else None


def _tool_version(
    argv: Sequence[str], *, runner: Runner, cwd: Path | None = None
) -> str | None:
    try:
        result = runner(
            list(argv),
            timeout=15.0,
            cwd=cwd,
            max_stdout_bytes=16_384,
            max_stderr_bytes=16_384,
        )
    except BoundedSubprocessError:
        return None
    if result.returncode != 0:
        return None
    text = (result.stdout_text or result.stderr_text).strip()
    return text.splitlines()[0] if text else None


def _extract_semver(text: str) -> str | None:
    """Extract the first dotted numeric version (X.Y.Z) from a tool --version line."""
    if not text:
        return None
    line = text.strip().splitlines()[0] if text.strip() else ""
    match = re.search(r"\b(\d+\.\d+\.\d+)\b", line)
    return match.group(1) if match else None


def verify_audit_tool_versions(
    *,
    pip_audit_bin: Path,
    uv_bin: str,
    runner: Runner,
) -> tuple[dict[str, Any], str | None]:
    """Require pinned uv / pip-audit versions before export or advisory scans.

    Returns ``(details, error_code)``. On failure the caller must not start
    export or scanner work. Report fields alone never satisfy this check.
    """
    details: dict[str, Any] = {
        "pip_audit_version_expected": PIP_AUDIT_VERSION,
        "uv_version_expected": UV_REQUIRED_VERSION,
    }

    def _probe(argv: list[str], *, tool: str) -> tuple[str | None, str | None]:
        try:
            result = runner(
                argv,
                timeout=15.0,
                max_stdout_bytes=16_384,
                max_stderr_bytes=16_384,
            )
        except BoundedSubprocessError:
            return None, "tool_version_unreadable"
        if result.returncode != 0:
            return None, "tool_version_unreadable"
        text = (result.stdout_text or result.stderr_text or "").strip()
        if not text:
            return None, "tool_version_unreadable"
        ver = _extract_semver(text)
        if ver is None:
            return None, "tool_version_unreadable"
        return ver, None

    if not pip_audit_bin.is_file():
        details.update(_safe_error("tool_missing", context={"tool": "pip-audit"}))
        return details, "tool_missing"

    uv_ver, uv_err = _probe([uv_bin, "--version"], tool="uv")
    details["uv_version_observed"] = uv_ver
    if uv_err is not None:
        details.update(
            _safe_error(uv_err, context={"tool": "uv", "expected": UV_REQUIRED_VERSION})
        )
        return details, uv_err
    if uv_ver != UV_REQUIRED_VERSION:
        details.update(
            _safe_error(
                "tool_version_mismatch",
                context={
                    "tool": "uv",
                    "expected": UV_REQUIRED_VERSION,
                    "observed": uv_ver or "",
                },
            )
        )
        return details, "tool_version_mismatch"

    pa_ver, pa_err = _probe([str(pip_audit_bin), "--version"], tool="pip-audit")
    details["pip_audit_version_observed"] = pa_ver
    if pa_err is not None:
        details.update(
            _safe_error(
                pa_err, context={"tool": "pip-audit", "expected": PIP_AUDIT_VERSION}
            )
        )
        return details, pa_err
    if pa_ver != PIP_AUDIT_VERSION:
        details.update(
            _safe_error(
                "tool_version_mismatch",
                context={
                    "tool": "pip-audit",
                    "expected": PIP_AUDIT_VERSION,
                    "observed": pa_ver or "",
                },
            )
        )
        return details, "tool_version_mismatch"

    return details, None


def _marker_environment(target: Mapping[str, Any]) -> dict[str, str]:
    env = default_environment()
    env.update(
        {
            "os_name": "posix",
            "sys_platform": str(target["sys_platform"]),
            "platform_system": str(target["platform_system"]),
            "platform_machine": str(target["platform_machine"]),
            "platform_python_implementation": str(
                target["platform_python_implementation"]
            ),
            "python_version": str(target["python_version"]),
            "python_full_version": str(target["python_full_version"]),
            "implementation_name": str(target.get("implementation_name", "cpython")),
            "implementation_version": str(
                target.get("implementation_version", target["python_full_version"])
            ),
        }
    )
    return env


def _parse_requirement_blocks(
    text: str,
) -> tuple[list[tuple[str, list[str], str]], list[str]]:
    """Parse export into (raw_line0, hash_lines, joined_req) and orphan errors."""
    lines = text.splitlines()
    blocks: list[tuple[str, list[str], str]] = []
    orphans: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if line[:1].isspace():
            orphans.append("orphan_continuation")
            i += 1
            continue
        head = line
        hash_lines: list[str] = []
        joined_parts: list[str] = []
        head_stripped = head.strip()
        if not head_stripped.startswith("--hash") and not head_stripped.startswith("#"):
            joined_parts.append(head_stripped.rstrip("\\").strip())
        i += 1
        while i < len(lines) and lines[i][:1].isspace():
            stripped = lines[i].strip()
            if stripped.startswith("--hash"):
                hash_lines.append(lines[i].rstrip())
            elif stripped.startswith("#"):
                pass
            else:
                joined_parts.append(stripped.rstrip("\\").strip())
            i += 1
        joined = " ".join(p for p in joined_parts if p)
        blocks.append((head, hash_lines, joined))
    return blocks, orphans


def _strip_marker_keep_hashes(head: str, hash_lines: list[str], req: Requirement) -> str:
    """Emit ``name==version`` plus hash continuations; markers already evaluated."""
    pin = f"{req.name}=={next(iter(req.specifier)).version}"
    if not hash_lines:
        return pin + "\n"
    out = [pin + " \\"]
    for idx, hl in enumerate(hash_lines):
        cleaned = hl.rstrip()
        if cleaned.endswith("\\"):
            cleaned = cleaned[:-1].rstrip()
        if idx < len(hash_lines) - 1:
            out.append(cleaned + " \\")
        else:
            out.append(cleaned)
    return "\n".join(out) + "\n"


def filter_export_for_target(
    export_text: str,
    *,
    target: Mapping[str, Any],
    exclude_names: frozenset[str] = LOCAL_PROJECT_NAMES,
) -> tuple[str, dict[str, str], str | None]:
    """Filter uv export for a synthetic target; strip markers from scanner input."""
    if not export_text.strip():
        return "", {}, "export_empty"
    env = _marker_environment(target)
    blocks, orphans = _parse_requirement_blocks(export_text)
    if orphans:
        return "", {}, "export_parse_error"
    if not blocks:
        return "", {}, "export_silent_loss"

    expected: dict[str, str] = {}
    kept: list[str] = []
    excluded = 0
    dropped_marker = 0
    for head, hash_lines, joined in blocks:
        if not joined:
            return "", {}, "export_parse_error"
        try:
            req = Requirement(joined)
        except InvalidRequirement:
            return "", {}, "export_parse_error"
        key = _normalize_py_name(req.name)
        if key in {_normalize_py_name(n) for n in exclude_names}:
            excluded += 1
            continue
        if req.marker is not None and not req.marker.evaluate(env):
            dropped_marker += 1
            continue
        if len(req.specifier) != 1:
            return "", {}, "export_parse_error"
        spec = next(iter(req.specifier))
        if spec.operator != "==":
            return "", {}, "export_parse_error"
        if key in expected:
            return "", {}, "export_parse_error"
        expected[key] = spec.version
        kept.append(_strip_marker_keep_hashes(head, hash_lines, req))

    # Non-empty export that loses every external package without markers/excludes
    # is treated as silent loss (malformed/incomplete), not clean empty inventory.
    if not expected and (len(blocks) - excluded - dropped_marker) > 0:
        return "", {}, "export_silent_loss"
    if not expected and not excluded and not dropped_marker and blocks:
        return "", {}, "export_silent_loss"
    return "".join(kept), expected, None


def load_exceptions(
    path: Path, *, today: date | None = None
) -> tuple[list[ExceptionRule], str | None]:
    del today
    if not path.is_file():
        return [], "exceptions_malformed"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return [], "exceptions_malformed"
    if not isinstance(raw, dict) or "exceptions" not in raw:
        return [], "exceptions_malformed"
    items = raw["exceptions"]
    if not isinstance(items, list):
        return [], "exceptions_malformed"
    rules: list[ExceptionRule] = []
    for item in items:
        if not isinstance(item, dict):
            return [], "exceptions_malformed"
        ecosystem = item.get("ecosystem")
        scope = item.get("scope")
        advisory = item.get("advisory")
        package = item.get("package")
        reason = item.get("reason")
        owner = item.get("owner")
        deadline_raw = item.get("review_deadline")
        versions_raw = item.get("versions")
        aliases_raw = item.get("aliases", [])
        if ecosystem not in ECOSYSTEMS:
            return [], "exceptions_malformed"
        if scope not in SECTION_SCOPES:
            return [], "exceptions_malformed"
        if not isinstance(advisory, str) or not advisory.strip():
            return [], "exceptions_malformed"
        if not isinstance(package, str) or not package.strip():
            return [], "exceptions_malformed"
        if not isinstance(reason, str) or not reason.strip():
            return [], "exceptions_malformed"
        if not isinstance(owner, str) or not owner.strip():
            return [], "exceptions_malformed"
        if not isinstance(deadline_raw, str):
            return [], "exceptions_malformed"
        try:
            deadline = date.fromisoformat(deadline_raw)
        except ValueError:
            return [], "exceptions_malformed"
        if not isinstance(versions_raw, list) or not versions_raw:
            return [], "exceptions_malformed"
        versions: list[str] = []
        for v in versions_raw:
            if not isinstance(v, str) or not v.strip():
                return [], "exceptions_malformed"
            if any(ch in v for ch in "*<>!") or v.strip().lower() in {"any", "*"}:
                return [], "exceptions_malformed"
            if "," in v or " " in v.strip():
                return [], "exceptions_malformed"
            versions.append(v.strip())
        aliases: list[str] = []
        if aliases_raw is None:
            aliases_raw = []
        if not isinstance(aliases_raw, list):
            return [], "exceptions_malformed"
        for a in aliases_raw:
            if not isinstance(a, str) or not a.strip():
                return [], "exceptions_malformed"
            aliases.append(a.strip())
        # Scope/ecosystem consistency: npm scope ↔ npm ecosystem; python_* ↔ pypi
        if scope == "npm" and ecosystem != "npm":
            return [], "exceptions_malformed"
        if scope.startswith("python-") and ecosystem != "pypi":
            return [], "exceptions_malformed"
        rules.append(
            ExceptionRule(
                ecosystem=ecosystem,
                scope=scope,
                advisory=advisory.strip(),
                package=package.strip(),
                versions=tuple(versions),
                reason=reason.strip(),
                owner=owner.strip(),
                review_deadline=deadline,
                aliases=tuple(aliases),
            )
        )
    return rules, None


def apply_exceptions(
    findings: Sequence[Finding],
    rules: Sequence[ExceptionRule],
    *,
    today: date,
) -> tuple[list[Finding], list[dict[str, Any]]]:
    remaining: list[Finding] = []
    accepted: list[dict[str, Any]] = []
    for finding in findings:
        matched: ExceptionRule | None = None
        for rule in rules:
            if rule.covers(finding, today=today):
                matched = rule
                break
        if matched is None:
            remaining.append(finding)
            continue
        accepted.append(
            {
                "finding": asdict(finding),
                "exception": {
                    "ecosystem": matched.ecosystem,
                    "scope": matched.scope,
                    "advisory": matched.advisory,
                    "aliases": list(matched.aliases),
                    "package": matched.package,
                    "versions": list(matched.versions),
                    "owner": matched.owner,
                    "review_deadline": matched.review_deadline.isoformat(),
                    "reason": matched.reason,
                },
            }
        )
    return remaining, accepted


def parse_pip_audit_json(
    payload: Any,
    *,
    expected: Mapping[str, str],
    section: str,
) -> tuple[list[Finding], str | None, dict[str, Any]]:
    """Parse pip-audit 2.10.1 JSON ``{dependencies: [...], fixes: [...]}``."""
    details: dict[str, Any] = {}
    if not isinstance(payload, dict):
        return [], "json_malformed", details
    if "dependencies" not in payload or "fixes" not in payload:
        return [], "json_incomplete", details
    deps = payload["dependencies"]
    fixes = payload["fixes"]
    if not isinstance(deps, list) or not isinstance(fixes, list):
        return [], "json_malformed", details

    findings: list[Finding] = []
    seen: dict[str, str] = {}
    skipped: list[dict[str, str]] = []

    for item in deps:
        if not isinstance(item, dict):
            return [], "json_malformed", details
        name = item.get("name")
        if not isinstance(name, str) or not name:
            return [], "json_incomplete", details
        key = _normalize_py_name(name)
        if "skip_reason" in item:
            skip_reason = item.get("skip_reason")
            if not isinstance(skip_reason, str) or not skip_reason:
                return [], "json_incomplete", details
            # Skipped entries must not also claim a resolved version+vulns cleanly.
            skipped.append({"name": name, "skip_reason": "skipped"})
            continue
        if "vulns" not in item:
            # Missing vulns is not an empty list — incomplete record.
            return [], "json_incomplete", details
        version = item.get("version")
        if not isinstance(version, str) or not version:
            return [], "json_incomplete", details
        if key in seen:
            details["duplicate"] = [key]
            return [], "inventory_incomplete", details
        seen[key] = version
        vulns = item["vulns"]
        if not isinstance(vulns, list):
            return [], "json_malformed", details
        seen_vuln: set[str] = set()
        for vuln in vulns:
            if not isinstance(vuln, dict):
                return [], "json_malformed", details
            vid = vuln.get("id")
            if not isinstance(vid, str) or not vid:
                return [], "json_incomplete", details
            if "fix_versions" not in vuln or not isinstance(
                vuln["fix_versions"], list
            ):
                return [], "json_incomplete", details
            aliases_raw = vuln.get("aliases", [])
            if aliases_raw is None:
                aliases_raw = []
            if not isinstance(aliases_raw, list):
                return [], "json_malformed", details
            aliases = tuple(
                a for a in aliases_raw if isinstance(a, str) and a and a != vid
            )
            if vid in seen_vuln:
                continue
            seen_vuln.add(vid)
            findings.append(
                Finding(
                    ecosystem="pypi",
                    section=section,
                    package=name,
                    version=version,
                    advisory_id=vid,
                    aliases=aliases,
                )
            )

    details["scanned_packages"] = len(seen)
    details["expected_packages"] = len(expected)
    if skipped:
        details["skipped"] = skipped
        return findings, "inventory_incomplete", details

    missing = sorted(set(expected) - set(seen))
    unexpected = sorted(set(seen) - set(expected))
    wrong_version = sorted(
        k for k in set(expected) & set(seen) if expected[k] != seen[k]
    )
    if missing or unexpected or wrong_version:
        details["missing"] = missing
        details["unexpected"] = unexpected
        details["wrong_version"] = {
            k: {"expected": expected[k], "actual": seen[k]} for k in wrong_version
        }
        return findings, "inventory_incomplete", details
    return findings, None, details


def _nonneg_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def resolve_npm_installed_versions(
    lock_data: Mapping[str, Any],
    *,
    package: str,
    nodes: Sequence[Any],
) -> tuple[tuple[str, ...], str | None]:
    """Resolve installed versions for vulnerability nodes from package-lock v2/v3."""
    if not nodes:
        return (), "unresolved_nodes"
    versions: list[str] = []
    packages = lock_data.get("packages")
    if not isinstance(packages, dict):
        return (), "unresolved_nodes"
    for node in nodes:
        if not isinstance(node, str) or not node:
            return (), "unresolved_nodes"
        # lockfile v3 keys are paths like "node_modules/foo" or ""
        entry = packages.get(node)
        if not isinstance(entry, dict):
            # Sometimes nodes omit node_modules prefix variants
            alt = node if node.startswith("node_modules/") else f"node_modules/{node}"
            entry = packages.get(alt)
        if not isinstance(entry, dict):
            return (), "unresolved_nodes"
        ver = entry.get("version")
        if not isinstance(ver, str) or not ver:
            return (), "unresolved_nodes"
        versions.append(ver)
    # Ambiguous multi-version instances: return all unique; caller must match exactly.
    uniq = tuple(sorted(set(versions)))
    if not uniq:
        return (), "unresolved_nodes"
    return uniq, None


def _parse_npm_via_object(
    item: Mapping[str, Any], entry: Mapping[str, Any]
) -> tuple[str, tuple[str, ...], str | None] | str:
    """Parse one advisory object from npm ``via``. Returns error code string on failure."""
    advisory: str | None = None
    aliases: list[str] = []
    url = item.get("url")
    if isinstance(url, str):
        match = re.search(r"GHSA-[a-z0-9-]+", url, re.I)
        if match:
            advisory = match.group(0)
    source = item.get("source")
    if isinstance(item.get("cve"), str) and item["cve"]:
        aliases.append(item["cve"])
    if advisory is None and isinstance(source, int):
        advisory = f"NPM-{source}"
    if advisory is None and isinstance(item.get("title"), str) and item["title"]:
        advisory = item["title"]
    if advisory is None:
        return "json_incomplete"
    if isinstance(source, int):
        aliases.append(f"NPM-{source}")
    severity = item.get("severity") if isinstance(item.get("severity"), str) else None
    if severity is None and isinstance(entry.get("severity"), str):
        severity = entry["severity"]
    alias_t = tuple(sorted({a for a in aliases if a and a != advisory}))
    return advisory, alias_t, severity


def _npm_via_target_reaches_advisory(
    target: str,
    vulns: Mapping[str, Any],
    *,
    visiting: frozenset[str],
) -> str | None:
    """Return None when ``target`` reaches extractable advisory evidence; else error code.

    Does not attribute the target's advisories to the referring package — only
    proves the string ``via`` branch is not silently lost.
    """
    if not target:
        return "json_incomplete"
    if target in visiting:
        return "inventory_incomplete"
    if target not in vulns:
        return "inventory_incomplete"
    entry = vulns[target]
    if not isinstance(entry, dict):
        return "json_malformed"
    if "via" not in entry:
        return "json_incomplete"
    via = entry["via"]
    if not isinstance(via, list):
        return "json_malformed"
    if not via:
        return "inventory_incomplete"
    saw_evidence = False
    next_visiting = visiting | {target}
    for item in via:
        if isinstance(item, str):
            err = _npm_via_target_reaches_advisory(
                item, vulns, visiting=next_visiting
            )
            if err is not None:
                return err
            saw_evidence = True
            continue
        if not isinstance(item, dict):
            return "json_malformed"
        parsed = _parse_npm_via_object(item, entry)
        if isinstance(parsed, str):
            return parsed
        saw_evidence = True
    return None if saw_evidence else "inventory_incomplete"


def parse_npm_audit_json(
    payload: Any,
    *,
    lock_data: Mapping[str, Any] | None,
    section: str = "npm",
) -> tuple[list[Finding], str | None, dict[str, Any]]:
    """Parse npm audit report v2 with per-entry ``via`` chain completeness."""
    details: dict[str, Any] = {}
    if not isinstance(payload, dict):
        return [], "json_malformed", details
    if payload.get("auditReportVersion") != 2:
        return [], "json_malformed", details
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return [], "json_incomplete", details
    dep_meta = metadata.get("dependencies")
    vuln_meta = metadata.get("vulnerabilities")
    if not isinstance(dep_meta, dict) or not isinstance(vuln_meta, dict):
        return [], "json_incomplete", details
    for key in ("prod", "dev", "optional", "total"):
        if key not in dep_meta or not _nonneg_int(dep_meta[key]):
            return [], "json_incomplete", details
    for key in ("info", "low", "moderate", "high", "critical", "total"):
        if key not in vuln_meta or not _nonneg_int(vuln_meta[key]):
            return [], "json_incomplete", details
    details["dependency_counts"] = {
        k: dep_meta[k] for k in ("prod", "dev", "optional", "peer", "total") if k in dep_meta
    }
    details["vulnerability_counts"] = {
        k: vuln_meta[k]
        for k in ("info", "low", "moderate", "high", "critical", "total")
    }

    if "vulnerabilities" not in payload:
        return [], "json_incomplete", details
    vulns = payload["vulnerabilities"]
    if not isinstance(vulns, dict):
        return [], "json_malformed", details

    meta_total = vuln_meta["total"]
    if meta_total > 0 and len(vulns) == 0:
        return [], "inventory_incomplete", details
    if meta_total == 0 and len(vulns) > 0:
        return [], "inventory_incomplete", details

    findings: list[Finding] = []
    advisory_rows = 0
    for pkg_name, entry in vulns.items():
        if not isinstance(pkg_name, str) or not pkg_name:
            return [], "json_malformed", details
        if not isinstance(entry, dict):
            return [], "json_malformed", details
        if "via" not in entry:
            return [], "json_incomplete", details
        via = entry["via"]
        if not isinstance(via, list):
            return [], "json_malformed", details
        if meta_total > 0 and not via:
            return [], "inventory_incomplete", details
        nodes = entry.get("nodes", [])
        if nodes is None:
            nodes = []
        if not isinstance(nodes, list):
            return [], "json_malformed", details

        has_object_via = any(isinstance(x, dict) for x in via)
        installed_versions: tuple[str, ...] = ()
        if lock_data is not None and has_object_via:
            installed_versions, resolve_err = resolve_npm_installed_versions(
                lock_data, package=pkg_name, nodes=nodes
            )
            if resolve_err:
                # Cannot prove installed version — keep findings without version
                # so exceptions cannot falsely clear them.
                installed_versions = ()

        # Every via branch must be valid. Object advisories attach only to this
        # package instance; string refs prove reachability without re-attributing
        # another package's advisory/version to the referrer.
        for item in via:
            if isinstance(item, str):
                if not item:
                    return [], "json_incomplete", details
                err = _npm_via_target_reaches_advisory(
                    item, vulns, visiting=frozenset({pkg_name})
                )
                if err is not None:
                    return [], err, details
                continue
            if not isinstance(item, dict):
                return [], "json_malformed", details
            parsed = _parse_npm_via_object(item, entry)
            if isinstance(parsed, str):
                return [], parsed, details
            advisory, alias_t, severity = parsed
            advisory_rows += 1
            if installed_versions:
                for ver in installed_versions:
                    findings.append(
                        Finding(
                            ecosystem="npm",
                            section=section,
                            package=pkg_name,
                            version=ver,
                            advisory_id=advisory,
                            aliases=alias_t,
                            severity=severity,
                        )
                    )
            else:
                findings.append(
                    Finding(
                        ecosystem="npm",
                        section=section,
                        package=pkg_name,
                        version=None,
                        advisory_id=advisory,
                        aliases=alias_t,
                        severity=severity,
                    )
                )

    if meta_total > 0 and advisory_rows == 0:
        return findings, "inventory_incomplete", details
    if meta_total == 0 and findings:
        return findings, "inventory_incomplete", details

    details["finding_count"] = len(findings)
    details["advisory_rows"] = advisory_rows
    details["vulnerable_package_entries"] = len(vulns)
    details["unique_advisory_ids"] = len({f.advisory_id for f in findings})
    details["package_instance_findings"] = len(findings)
    return findings, None, details


def _classify_bounded_failure(exc: BaseException) -> tuple[str, str]:
    if isinstance(exc, BoundedTimeoutError):
        return STATUS_TIMEOUT, "scanner_timeout"
    if isinstance(exc, BoundedCancelledError):
        return STATUS_CANCELLED, "scanner_cancelled"
    if isinstance(exc, OutputLimitExceededError):
        return STATUS_INCOMPLETE, "scanner_output_limit"
    return STATUS_SCANNER_ERROR, "bounded_error"


def _finalize_section_with_exceptions(
    *,
    name: str,
    returncode: int,
    ok_exits: frozenset[int],
    findings: list[Finding],
    parse_error: str | None,
    details: dict[str, Any],
    exceptions: Sequence[ExceptionRule],
    today: date,
) -> SectionResult:
    """Apply exit-code contract before exception clearing."""
    if parse_error:
        status = {
            "json_malformed": STATUS_MALFORMED,
            "json_incomplete": STATUS_MALFORMED,
            "inventory_incomplete": STATUS_INCOMPLETE,
            "export_parse_error": STATUS_MALFORMED,
            "export_silent_loss": STATUS_INCOMPLETE,
            "export_empty": STATUS_SOURCE_UNAVAILABLE,
        }.get(parse_error, STATUS_SCANNER_ERROR)
        ctx: dict[str, Any] = {"returncode": returncode}
        if isinstance(details.get("failure_stage"), str):
            ctx["failure_stage"] = details["failure_stage"]
        if isinstance(details.get("diagnostic_reason"), str):
            ctx["reason"] = details["diagnostic_reason"]
        return SectionResult(
            name=name,
            status=status,
            raw_findings=list(findings),
            findings=list(findings),
            details={**details, **_safe_error(parse_error, context=ctx)},
        )

    if returncode not in ok_exits:
        ctx = {"returncode": returncode}
        if isinstance(details.get("failure_stage"), str):
            ctx["failure_stage"] = details["failure_stage"]
        if isinstance(details.get("diagnostic_reason"), str):
            ctx["reason"] = details["diagnostic_reason"]
        return SectionResult(
            name=name,
            status=STATUS_SCANNER_ERROR,
            raw_findings=list(findings),
            findings=list(findings),
            details={
                **details,
                **_safe_error("scanner_bad_exit", context=ctx),
            },
        )

    # Exit/payload consistency.
    if returncode == 0 and findings:
        ctx = {"returncode": 0, "reason": details.get("diagnostic_reason") or "exit0_with_findings"}
        if isinstance(details.get("failure_stage"), str):
            ctx["failure_stage"] = details["failure_stage"]
        return SectionResult(
            name=name,
            status=STATUS_INCOMPLETE,
            raw_findings=list(findings),
            findings=list(findings),
            details={
                **details,
                **_safe_error(
                    "scanner_exit_payload_mismatch",
                    context=ctx,
                ),
            },
        )
    if returncode == 1 and not findings:
        ctx = {"returncode": 1, "reason": details.get("diagnostic_reason") or "exit1_without_findings"}
        if isinstance(details.get("failure_stage"), str):
            ctx["failure_stage"] = details["failure_stage"]
        return SectionResult(
            name=name,
            status=STATUS_INCOMPLETE,
            raw_findings=[],
            findings=[],
            details={
                **details,
                **_safe_error(
                    "scanner_exit_payload_mismatch",
                    context=ctx,
                ),
            },
        )

    remaining, accepted = apply_exceptions(findings, exceptions, today=today)
    details["raw_finding_count"] = len(findings)
    details["accepted_exception_count"] = len(accepted)
    details["remaining_finding_count"] = len(remaining)
    if remaining:
        return SectionResult(
            name=name,
            status=STATUS_FINDINGS,
            raw_findings=list(findings),
            findings=remaining,
            accepted_exceptions=accepted,
            details=details,
        )
    note = None
    if accepted:
        note = "findings_covered_by_exceptions"
    return SectionResult(
        name=name,
        status=STATUS_CLEAN,
        raw_findings=list(findings),
        findings=[],
        accepted_exceptions=accepted,
        details={**details, **({"note": note} if note else {})},
    )


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_NPM_EXPECTED_TOP_LEVEL = ("auditReportVersion", "metadata", "vulnerabilities", "error")
_NPM_ERROR_CODE_RE = re.compile(r"^E[A-Z0-9]{1,64}$")

# Fixed diagnostic reason codes for npm failure evidence (not free-form text).
NPM_REASON_JSON_DECODE = "npm_json_decode"
NPM_REASON_SCHEMA_MALFORMED = "npm_schema_malformed"
NPM_REASON_SCHEMA_INCOMPLETE = "npm_schema_incomplete"
NPM_REASON_SCHEMA_INVENTORY = "npm_schema_inventory_incomplete"
NPM_REASON_EMPTY_STDOUT = "npm_empty_stdout"
NPM_REASON_OUTPUT_LIMIT = "npm_output_limit"
NPM_REASON_BOUNDED_TIMEOUT = "npm_bounded_timeout"
NPM_REASON_BOUNDED_CANCELLED = "npm_bounded_cancelled"
NPM_REASON_BOUNDED_OUTPUT_LIMIT = "npm_bounded_output_limit"
NPM_REASON_BOUNDED_ERROR = "npm_bounded_error"
NPM_REASON_SCANNER_BAD_EXIT = "npm_scanner_bad_exit"
NPM_REASON_EXIT_PAYLOAD = "npm_exit_payload_mismatch"

NPM_STAGE_DECODE = "decode"
NPM_STAGE_SCHEMA = "schema"
NPM_STAGE_SCANNER_EXIT = "scanner_exit"
NPM_STAGE_BOUNDED = "bounded_execution"


def _npm_io_evidence(
    stdout: bytes,
    stderr: bytes,
    *,
    returncode: int | None,
) -> dict[str, Any]:
    """Safe I/O digests for npm failures — never includes stream bodies."""
    out: dict[str, Any] = {
        "stdout_bytes": len(stdout),
        "stderr_bytes": len(stderr),
        "stdout_sha256": _sha256_bytes(stdout) if stdout else None,
        "stderr_sha256": _sha256_bytes(stderr) if stderr else None,
    }
    if returncode is not None:
        out["scanner_returncode"] = returncode
    return out


def _npm_safe_error_code(value: Any) -> str | None:
    """Accept only narrowly shaped npm error codes (e.g. ENOAUDIT)."""
    if not isinstance(value, str):
        return None
    if not _NPM_ERROR_CODE_RE.fullmatch(value):
        return None
    return value


def _npm_parsed_shape_evidence(payload: Any) -> dict[str, Any]:
    """Structural facts from a decoded npm JSON object — names/flags only."""
    evidence: dict[str, Any] = {
        "json_root_type": type(payload).__name__,
    }
    if not isinstance(payload, dict):
        evidence["expected_fields_present"] = []
        evidence["has_npm_error_object"] = False
        return evidence
    present = [k for k in _NPM_EXPECTED_TOP_LEVEL if k in payload]
    evidence["expected_fields_present"] = present
    version = payload.get("auditReportVersion")
    if type(version) is int and not isinstance(version, bool):
        evidence["audit_report_version"] = version
    has_error = isinstance(payload.get("error"), dict)
    evidence["has_npm_error_object"] = has_error
    if has_error:
        code = _npm_safe_error_code(payload["error"].get("code"))
        if code is not None:
            evidence["npm_error_code"] = code
    return evidence


def _npm_schema_reason(parse_error: str) -> str:
    return {
        "json_malformed": NPM_REASON_SCHEMA_MALFORMED,
        "json_incomplete": NPM_REASON_SCHEMA_INCOMPLETE,
        "inventory_incomplete": NPM_REASON_SCHEMA_INVENTORY,
    }.get(parse_error, NPM_REASON_SCHEMA_MALFORMED)


def _npm_attach_failure_markers(
    details: dict[str, Any],
    *,
    stage: str,
    reason: str,
) -> None:
    details["failure_stage"] = stage
    details["diagnostic_reason"] = reason


def run_npm_section(
    *,
    repo: Path,
    runner: Runner,
    exceptions: Sequence[ExceptionRule],
    today: date,
) -> SectionResult:
    web = repo / "web"
    lock = web / "package-lock.json"
    details: dict[str, Any] = {"lock_path": "web/package-lock.json"}
    if not lock.is_file():
        return SectionResult(
            name="npm",
            status=STATUS_SOURCE_UNAVAILABLE,
            details={**details, **_safe_error("missing_lock")},
        )
    details["lock_sha256"] = _sha256_file(lock)
    try:
        lock_data = json.loads(lock.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return SectionResult(
            name="npm",
            status=STATUS_MALFORMED,
            details={**details, **_safe_error("json_malformed", context={"path": "package-lock"})},
        )
    if not isinstance(lock_data, dict):
        return SectionResult(
            name="npm",
            status=STATUS_MALFORMED,
            details={**details, **_safe_error("json_malformed", context={"path": "package-lock"})},
        )

    details["node_version"] = _tool_version(["node", "--version"], runner=runner)
    details["npm_version"] = _tool_version(["npm", "--version"], runner=runner)

    argv = ["npm", "audit", "--json", "--ignore-scripts", "--package-lock-only"]
    try:
        result = runner(
            argv,
            timeout=NPM_TIMEOUT_SECONDS,
            cwd=web,
            max_stdout_bytes=OUTPUT_LIMIT_BYTES,
            max_stderr_bytes=OUTPUT_LIMIT_BYTES,
        )
    except BoundedSubprocessError as exc:
        status, code = _classify_bounded_failure(exc)
        reason = {
            STATUS_TIMEOUT: NPM_REASON_BOUNDED_TIMEOUT,
            STATUS_CANCELLED: NPM_REASON_BOUNDED_CANCELLED,
            STATUS_INCOMPLETE: NPM_REASON_BOUNDED_OUTPUT_LIMIT,
        }.get(status, NPM_REASON_BOUNDED_ERROR)
        _npm_attach_failure_markers(
            details, stage=NPM_STAGE_BOUNDED, reason=reason
        )
        return SectionResult(
            name="npm",
            status=status,
            details={
                **details,
                **_safe_error(
                    code,
                    context={"failure_stage": NPM_STAGE_BOUNDED, "reason": reason},
                ),
            },
        )

    stdout = result.stdout or b""
    stderr = result.stderr or b""
    returncode = result.returncode if result.returncode is not None else 2
    details.update(_npm_io_evidence(stdout, stderr, returncode=returncode))

    if not stdout:
        _npm_attach_failure_markers(
            details, stage=NPM_STAGE_SCANNER_EXIT, reason=NPM_REASON_EMPTY_STDOUT
        )
        return SectionResult(
            name="npm",
            status=STATUS_SCANNER_ERROR,
            details={
                **details,
                **_safe_error(
                    "scanner_empty_output",
                    context={
                        "returncode": returncode,
                        "failure_stage": NPM_STAGE_SCANNER_EXIT,
                        "reason": NPM_REASON_EMPTY_STDOUT,
                    },
                ),
            },
        )
    if len(stdout) > OUTPUT_LIMIT_BYTES:
        _npm_attach_failure_markers(
            details, stage=NPM_STAGE_BOUNDED, reason=NPM_REASON_OUTPUT_LIMIT
        )
        return SectionResult(
            name="npm",
            status=STATUS_INCOMPLETE,
            details={
                **details,
                **_safe_error(
                    "scanner_output_limit",
                    context={
                        "bytes": len(stdout),
                        "limit": OUTPUT_LIMIT_BYTES,
                        "failure_stage": NPM_STAGE_BOUNDED,
                        "reason": NPM_REASON_OUTPUT_LIMIT,
                    },
                ),
            },
        )
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        _npm_attach_failure_markers(
            details, stage=NPM_STAGE_DECODE, reason=NPM_REASON_JSON_DECODE
        )
        return SectionResult(
            name="npm",
            status=STATUS_MALFORMED,
            details={
                **details,
                **_safe_error(
                    "json_malformed",
                    context={
                        "returncode": returncode,
                        "failure_stage": NPM_STAGE_DECODE,
                        "reason": NPM_REASON_JSON_DECODE,
                    },
                ),
            },
        )

    details.update(_npm_parsed_shape_evidence(payload))
    findings, err, parse_details = parse_npm_audit_json(
        payload, lock_data=lock_data, section="npm"
    )
    details.update(parse_details)
    if err is not None:
        reason = _npm_schema_reason(err)
        _npm_attach_failure_markers(
            details, stage=NPM_STAGE_SCHEMA, reason=reason
        )
    elif returncode not in NPM_OK_EXIT:
        _npm_attach_failure_markers(
            details,
            stage=NPM_STAGE_SCANNER_EXIT,
            reason=NPM_REASON_SCANNER_BAD_EXIT,
        )
    elif returncode == 0 and findings:
        _npm_attach_failure_markers(
            details,
            stage=NPM_STAGE_SCANNER_EXIT,
            reason=NPM_REASON_EXIT_PAYLOAD,
        )
    elif returncode == 1 and not findings:
        _npm_attach_failure_markers(
            details,
            stage=NPM_STAGE_SCANNER_EXIT,
            reason=NPM_REASON_EXIT_PAYLOAD,
        )
    return _finalize_section_with_exceptions(
        name="npm",
        returncode=returncode,
        ok_exits=NPM_OK_EXIT,
        findings=findings,
        parse_error=err,
        details=details,
        exceptions=exceptions,
        today=today,
    )


def _run_uv(
    argv: Sequence[str],
    *,
    runner: Runner,
    cwd: Path,
    timeout: float,
) -> BoundedResult:
    env = os.environ.copy()
    env["UV_PYTHON_DOWNLOADS"] = "never"
    return runner(
        list(argv),
        timeout=timeout,
        cwd=cwd,
        env=env,
        max_stdout_bytes=OUTPUT_LIMIT_BYTES,
        max_stderr_bytes=OUTPUT_LIMIT_BYTES,
    )


def run_python_section(
    *,
    repo: Path,
    runner: Runner,
    pip_audit_bin: Path,
    uv_bin: str,
    target: Mapping[str, Any],
    exceptions: Sequence[ExceptionRule],
    today: date,
) -> SectionResult:
    name = str(target["label"])
    backend = repo / "backend"
    lock = backend / "uv.lock"
    details: dict[str, Any] = {
        "lock_path": "backend/uv.lock",
        "target_platform": {
            "sys_platform": target["sys_platform"],
            "platform_machine": target["platform_machine"],
            "python_version": target["python_version"],
            "python_full_version": target["python_full_version"],
            "implementation_name": target.get("implementation_name"),
            "implementation_version": target.get("implementation_version"),
            "extra_dev": bool(target["extra_dev"]),
            "uv_python": target.get("uv_python"),
        },
        "pip_audit_version_expected": PIP_AUDIT_VERSION,
        "uv_version_expected": UV_REQUIRED_VERSION,
        "json_channel": "stdout",
    }

    # Pin check before lock export / pip-audit — report fields alone are not enough.
    version_details, version_err = verify_audit_tool_versions(
        pip_audit_bin=pip_audit_bin, uv_bin=uv_bin, runner=runner
    )
    details.update(version_details)
    if version_err is not None:
        status = (
            STATUS_SOURCE_UNAVAILABLE
            if version_err == "tool_missing"
            else STATUS_SCANNER_ERROR
        )
        return SectionResult(name=name, status=status, details=details)

    if not lock.is_file():
        return SectionResult(
            name=name,
            status=STATUS_SOURCE_UNAVAILABLE,
            details={**details, **_safe_error("missing_lock")},
        )
    details["lock_sha256"] = _sha256_file(lock)

    try:
        check = _run_uv(
            [uv_bin, "lock", "--check"],
            runner=runner,
            cwd=backend,
            timeout=UV_LOCK_CHECK_TIMEOUT_SECONDS,
        )
    except BoundedSubprocessError as exc:
        status, code = _classify_bounded_failure(exc)
        return SectionResult(
            name=name, status=status, details={**details, **_safe_error(code)}
        )
    if check.returncode != 0:
        return SectionResult(
            name=name,
            status=STATUS_SOURCE_UNAVAILABLE,
            details={
                **details,
                **_safe_error("lock_stale", context={"returncode": check.returncode}),
            },
        )

    export_argv = [
        uv_bin,
        "export",
        "--frozen",
        "--no-emit-project",
        "--no-header",
        "--no-annotate",
        f"--python={target['uv_python']}",
    ]
    if target["extra_dev"]:
        export_argv.extend(["--extra", "dev"])
    else:
        export_argv.append("--no-dev")

    try:
        exported = _run_uv(
            export_argv,
            runner=runner,
            cwd=backend,
            timeout=UV_EXPORT_TIMEOUT_SECONDS,
        )
    except BoundedSubprocessError as exc:
        status, code = _classify_bounded_failure(exc)
        return SectionResult(
            name=name, status=status, details={**details, **_safe_error(code)}
        )
    if exported.returncode != 0:
        return SectionResult(
            name=name,
            status=STATUS_SOURCE_UNAVAILABLE,
            details={
                **details,
                **_safe_error(
                    "export_failed", context={"returncode": exported.returncode}
                ),
            },
        )
    export_text = exported.stdout_text
    if not export_text.strip():
        return SectionResult(
            name=name,
            status=STATUS_SOURCE_UNAVAILABLE,
            details={**details, **_safe_error("export_empty")},
        )

    filtered, expected, filter_err = filter_export_for_target(
        export_text, target=target
    )
    if filter_err:
        status = {
            "export_empty": STATUS_SOURCE_UNAVAILABLE,
            "export_parse_error": STATUS_MALFORMED,
            "export_silent_loss": STATUS_INCOMPLETE,
        }.get(filter_err, STATUS_MALFORMED)
        return SectionResult(
            name=name,
            status=status,
            details={**details, **_safe_error(filter_err)},
        )
    details["expected_packages"] = len(expected)
    details["input_sha256"] = hashlib.sha256(filtered.encode("utf-8")).hexdigest()
    details["markers_stripped"] = True

    # Binary presence already enforced by verify_audit_tool_versions above.
    with tempfile.TemporaryDirectory(prefix="fetchnow-dep-audit-") as tmp:
        req_path = Path(tmp) / "requirements.txt"
        req_path.write_text(filtered, encoding="utf-8")
        # JSON only on stdout — no -o file channel / no fallback.
        argv = [
            str(pip_audit_bin),
            "-r",
            str(req_path),
            "--no-deps",
            "--disable-pip",
            "--require-hashes",
            "-f",
            "json",
            "--strict",
            "-s",
            "pypi",
        ]
        try:
            result = runner(
                argv,
                timeout=PYTHON_AUDIT_TIMEOUT_SECONDS,
                cwd=Path(tmp),
                max_stdout_bytes=OUTPUT_LIMIT_BYTES,
                max_stderr_bytes=OUTPUT_LIMIT_BYTES,
            )
        except BoundedSubprocessError as exc:
            status, code = _classify_bounded_failure(exc)
            return SectionResult(
                name=name, status=status, details={**details, **_safe_error(code)}
            )

    stdout = result.stdout
    if not stdout:
        return SectionResult(
            name=name,
            status=STATUS_SCANNER_ERROR,
            details={
                **details,
                **_safe_error(
                    "scanner_empty_output", context={"returncode": result.returncode}
                ),
            },
        )
    if len(stdout) > OUTPUT_LIMIT_BYTES:
        return SectionResult(
            name=name,
            status=STATUS_INCOMPLETE,
            details={
                **details,
                **_safe_error(
                    "scanner_output_limit",
                    context={"bytes": len(stdout), "limit": OUTPUT_LIMIT_BYTES},
                ),
            },
        )
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return SectionResult(
            name=name,
            status=STATUS_MALFORMED,
            details={
                **details,
                **_safe_error(
                    "json_malformed", context={"returncode": result.returncode}
                ),
            },
        )

    findings, err, parse_details = parse_pip_audit_json(
        payload, expected=expected, section=name
    )
    details.update(parse_details)
    return _finalize_section_with_exceptions(
        name=name,
        returncode=result.returncode if result.returncode is not None else 2,
        ok_exits=PIP_AUDIT_OK_EXIT,
        findings=findings,
        parse_error=err,
        details=details,
        exceptions=exceptions,
        today=today,
    )


def validate_required_sections(
    sections: Sequence[SectionResult],
) -> str | None:
    names = [s.name for s in sections]
    if len(names) != len(REQUIRED_SECTIONS):
        return "sections_invalid"
    if tuple(names) != REQUIRED_SECTIONS:
        return "sections_invalid"
    if len(set(names)) != len(names):
        return "sections_invalid"
    return None


def build_report(
    *,
    repo: Path,
    sections: Sequence[SectionResult],
    runner: Runner,
    pip_audit_bin: Path | None,
    uv_bin: str,
) -> dict[str, Any]:
    section_err = validate_required_sections(sections)
    if section_err:
        overall = STATUS_INCOMPLETE
    else:
        statuses = [s.status for s in sections]
        tech = [s for s in statuses if s not in {STATUS_CLEAN, STATUS_FINDINGS}]
        if tech:
            overall = tech[0]
        elif any(s == STATUS_FINDINGS for s in statuses):
            overall = STATUS_FINDINGS
        else:
            overall = STATUS_CLEAN

    raw_all = [asdict(f) for s in sections for f in s.raw_findings]
    remaining_all = [asdict(f) for s in sections for f in s.findings]
    accepted_all = [e for s in sections for e in s.accepted_exceptions]

    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at_utc": _utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commit": _git_head(repo, runner),
        "tooling": {
            "pip_audit_expected": PIP_AUDIT_VERSION,
            "pip_audit_bin": str(pip_audit_bin) if pip_audit_bin else None,
            "pip_audit_version": _tool_version(
                [str(pip_audit_bin), "--version"], runner=runner
            )
            if pip_audit_bin
            else None,
            "uv_expected": UV_REQUIRED_VERSION,
            "uv_version": _tool_version([uv_bin, "--version"], runner=runner),
            "node_version": _tool_version(["node", "--version"], runner=runner),
            "npm_version": _tool_version(["npm", "--version"], runner=runner),
            "python_scanner_version": sys.version.split()[0],
        },
        "inputs": {
            "web_package_lock_sha256": _sha256_file(repo / "web" / "package-lock.json")
            if (repo / "web" / "package-lock.json").is_file()
            else None,
            "backend_uv_lock_sha256": _sha256_file(repo / "backend" / "uv.lock")
            if (repo / "backend" / "uv.lock").is_file()
            else None,
        },
        "required_sections": list(REQUIRED_SECTIONS),
        "completeness": {
            section.name: {
                "status": section.status,
                **{
                    k: section.details[k]
                    for k in (
                        "expected_packages",
                        "scanned_packages",
                        "dependency_counts",
                        "vulnerability_counts",
                        "raw_finding_count",
                        "accepted_exception_count",
                        "remaining_finding_count",
                        "lock_sha256",
                        "input_sha256",
                        "target_platform",
                        "error_code",
                        "error_context",
                        "note",
                        "markers_stripped",
                        "json_channel",
                        "failure_stage",
                        "diagnostic_reason",
                        "stdout_bytes",
                        "stderr_bytes",
                        "stdout_sha256",
                        "stderr_sha256",
                        "scanner_returncode",
                        "json_root_type",
                        "expected_fields_present",
                        "audit_report_version",
                        "has_npm_error_object",
                        "npm_error_code",
                    )
                    if k in section.details
                },
            }
            for section in sections
        },
        "sections": [
            {
                "name": s.name,
                "status": s.status,
                "raw_findings": [asdict(f) for f in s.raw_findings],
                "findings": [asdict(f) for f in s.findings],
                "accepted_exceptions": s.accepted_exceptions,
                "details": {
                    k: v
                    for k, v in s.details.items()
                    if k
                    not in {
                        # never publish raw stderr / env
                        "stderr",
                        "stdout",
                        "env",
                    }
                },
            }
            for s in sections
        ],
        "raw_findings": raw_all,
        "findings": remaining_all,
        "accepted_exceptions": accepted_all,
        "status": overall
        if not section_err
        else STATUS_INCOMPLETE,
        "disclaimer": (
            "Dated advisory-service result for the listed inputs. Not a guarantee "
            "that advisory databases are complete or that unknown vulnerabilities "
            "are absent. Accepted exceptions (if any) are listed separately and "
            "must not be described as zero vulnerabilities."
        ),
    }
    if section_err:
        report["completeness_error"] = _safe_error(section_err)
    return report


def overall_exit_code(report: Mapping[str, Any]) -> int:
    return 0 if report.get("status") == STATUS_CLEAN else 1


def run_audit(
    *,
    repo: Path,
    exceptions_path: Path,
    pip_audit_bin: Path,
    uv_bin: str = "uv",
    runner: Runner | None = None,
    today: date | None = None,
) -> tuple[dict[str, Any], int]:
    """Run the full mandatory three-section gate."""
    runner = runner or run_bounded
    today = today or date.today()
    rules, exc_err = load_exceptions(exceptions_path, today=today)
    if exc_err:
        sections = [
            SectionResult(
                name=name,
                status=STATUS_MALFORMED,
                details=_safe_error("exceptions_malformed"),
            )
            for name in REQUIRED_SECTIONS
        ]
        report = build_report(
            repo=repo,
            sections=sections,
            runner=runner,
            pip_audit_bin=pip_audit_bin,
            uv_bin=uv_bin,
        )
        report["status"] = STATUS_MALFORMED
        return report, 1

    # Fail closed on tool pins before any section starts scanners/export.
    version_details, version_err = verify_audit_tool_versions(
        pip_audit_bin=pip_audit_bin, uv_bin=uv_bin, runner=runner
    )
    if version_err is not None:
        status = (
            STATUS_SOURCE_UNAVAILABLE
            if version_err == "tool_missing"
            else STATUS_SCANNER_ERROR
        )
        sections = [
            SectionResult(name=name, status=status, details=dict(version_details))
            for name in REQUIRED_SECTIONS
        ]
        report = build_report(
            repo=repo,
            sections=sections,
            runner=runner,
            pip_audit_bin=pip_audit_bin,
            uv_bin=uv_bin,
        )
        report["status"] = status
        return report, 1

    sections = [
        run_npm_section(repo=repo, runner=runner, exceptions=rules, today=today),
        run_python_section(
            repo=repo,
            runner=runner,
            pip_audit_bin=pip_audit_bin,
            uv_bin=uv_bin,
            target=RUNTIME_TARGET,
            exceptions=rules,
            today=today,
        ),
        run_python_section(
            repo=repo,
            runner=runner,
            pip_audit_bin=pip_audit_bin,
            uv_bin=uv_bin,
            target=DEV_TARGET,
            exceptions=rules,
            today=today,
        ),
    ]
    report = build_report(
        repo=repo,
        sections=sections,
        runner=runner,
        pip_audit_bin=pip_audit_bin,
        uv_bin=uv_bin,
    )
    return report, overall_exit_code(report)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="FetchNow SEC-03B2 dependency audit (full three-section gate)"
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root",
    )
    parser.add_argument(
        "--exceptions",
        type=Path,
        default=None,
        help="exceptions JSON (default: docs/operations/dependency-exceptions.json)",
    )
    parser.add_argument(
        "--pip-audit-bin",
        type=Path,
        required=True,
        help="path to pip-audit from the isolated tooling venv",
    )
    parser.add_argument("--uv-bin", default="uv", help="uv 0.12.19 executable")
    parser.add_argument(
        "--report-out",
        type=Path,
        default=None,
        help="optional path for sanitized JSON report",
    )
    args = parser.parse_args(argv)

    repo = args.repo.resolve()
    exceptions = (
        args.exceptions
        if args.exceptions is not None
        else repo / "docs" / "operations" / "dependency-exceptions.json"
    )
    report, code = run_audit(
        repo=repo,
        exceptions_path=exceptions,
        pip_audit_bin=args.pip_audit_bin.resolve(),
        uv_bin=args.uv_bin,
    )
    text = json.dumps(report, indent=2, sort_keys=False) + "\n"
    if args.report_out is not None:
        args.report_out.parent.mkdir(parents=True, exist_ok=True)
        args.report_out.write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
