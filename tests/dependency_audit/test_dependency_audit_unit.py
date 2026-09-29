"""Offline unit tests for SEC-03B2 dependency_audit acceptance remediation."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(SCRIPTS))

from fetchnow_release.bounded_subprocess import (  # noqa: E402
    BoundedCancelledError,
    BoundedResult,
    BoundedTimeoutError,
    OutputLimitExceededError,
)

import dependency_audit as da  # noqa: E402


def _load_json(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


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


def _pinned_tool_versions(argv: list[str] | tuple[str, ...]) -> BoundedResult | None:
    """Satisfy verify_audit_tool_versions for section/integration mocks."""
    s = [str(a) for a in argv]
    if len(s) >= 2 and s[1] == "--version":
        if s[0] == "uv" or s[0].endswith("/uv"):
            return _result(argv=argv, stdout="uv 0.12.19\n")
        if s[0].endswith("pip-audit"):
            return _result(argv=argv, stdout="pip-audit 2.10.1\n")
    return None


def _calls_contain(calls: list[list[str]], *, prefix: list[str]) -> bool:
    return any(c[: len(prefix)] == prefix for c in calls)


def _calls_pip_audit_scan(calls: list[list[str]]) -> bool:
    for c in calls:
        if c and str(c[0]).endswith("pip-audit") and "--version" not in c:
            return True
    return False


def _calls_uv_export(calls: list[list[str]]) -> bool:
    return any(len(c) >= 2 and c[0] in {"uv",} and c[1] == "export" for c in calls) or any(
        len(c) >= 2 and str(c[0]).endswith("/uv") and c[1] == "export" for c in calls
    )


def test_pip_audit_missing_vulns_not_clean() -> None:
    payload = _load_json("pip_audit_missing_vulns.json")
    findings, err, _ = da.parse_pip_audit_json(
        payload, expected={"packaging": "26.3"}, section="python-dev"
    )
    assert err == "json_incomplete"
    assert findings == []


def test_pip_audit_clean_completeness() -> None:
    payload = _load_json("pip_audit_clean.json")
    findings, err, details = da.parse_pip_audit_json(
        payload,
        expected={"packaging": "26.3", "requests": "2.32.3"},
        section="python-runtime",
    )
    assert err is None
    assert findings == []
    assert details["scanned_packages"] == 2


def test_pip_audit_findings_and_aliases() -> None:
    payload = _load_json("pip_audit_findings.json")
    findings, err, _ = da.parse_pip_audit_json(
        payload, expected={"urllib3": "1.26.18"}, section="python-dev"
    )
    assert err is None
    assert len(findings) == 1
    assert findings[0].section == "python-dev"
    assert findings[0].advisory_id == "PYSEC-2026-1995"
    assert "GHSA-34jh-p97f-mpxf" in findings[0].aliases


def test_pip_audit_skipped_incomplete() -> None:
    payload = _load_json("pip_audit_skipped.json")
    _, err, details = da.parse_pip_audit_json(
        payload, expected={"secretstuff": "1.0"}, section="python-dev"
    )
    assert err == "inventory_incomplete"
    assert details["skipped"]


def test_pip_audit_duplicate_after_normalization() -> None:
    payload = {
        "dependencies": [
            {"name": "PyYAML", "version": "6.0.3", "vulns": []},
            {"name": "pyyaml", "version": "6.0.3", "vulns": []},
        ],
        "fixes": [],
    }
    _, err, details = da.parse_pip_audit_json(
        payload, expected={"pyyaml": "6.0.3"}, section="python-runtime"
    )
    assert err == "inventory_incomplete"
    assert "duplicate" in details


def test_pip_audit_wrong_version() -> None:
    payload = _load_json("pip_audit_wrong_version.json")
    _, err, details = da.parse_pip_audit_json(
        payload, expected={"packaging": "26.3"}, section="python-runtime"
    )
    assert err == "inventory_incomplete"
    assert details["wrong_version"]["packaging"]["expected"] == "26.3"


def test_pip_audit_malformed_root() -> None:
    payload = _load_json("pip_audit_malformed_root.json")
    _, err, _ = da.parse_pip_audit_json(payload, expected={}, section="python-runtime")
    assert err == "json_malformed"


def test_npm_clean_schema() -> None:
    payload = _load_json("npm_audit_clean.json")
    findings, err, details = da.parse_npm_audit_json(payload, lock_data={})
    assert err is None
    assert findings == []
    assert details["vulnerability_counts"]["total"] == 0


def test_npm_findings_resolve_installed_version() -> None:
    payload = _load_json("npm_audit_findings.json")
    lock = _load_json("npm_package_lock_min.json")
    findings, err, _ = da.parse_npm_audit_json(payload, lock_data=lock)
    assert err is None
    assert len(findings) == 1
    assert findings[0].advisory_id == "GHSA-xxxx-yyyy-zzzz"
    assert findings[0].version == "1.2.2"  # not the advisory range


def test_npm_nonzero_empty_via_not_clean() -> None:
    payload = _load_json("npm_audit_nonzero_empty_via.json")
    _, err, _ = da.parse_npm_audit_json(payload, lock_data={})
    assert err == "inventory_incomplete"


def test_npm_string_via_only_with_nonzero_meta_not_clean() -> None:
    payload = _load_json("npm_audit_string_via_only.json")
    _, err, _ = da.parse_npm_audit_json(payload, lock_data={})
    assert err == "inventory_incomplete"


def test_npm_bad_report_version() -> None:
    payload = _load_json("npm_audit_bad_version.json")
    _, err, _ = da.parse_npm_audit_json(payload, lock_data={})
    assert err == "json_malformed"


def test_npm_meta_without_map() -> None:
    payload = _load_json("npm_audit_meta_without_map.json")
    _, err, _ = da.parse_npm_audit_json(payload, lock_data={})
    assert err == "inventory_incomplete"


def test_exceptions_empty_ok() -> None:
    rules, err = da.load_exceptions(FIXTURES / "exceptions_empty.json")
    assert err is None
    assert rules == []


def test_exceptions_scoped_match_and_preserve_raw() -> None:
    rules, err = da.load_exceptions(FIXTURES / "exceptions_valid_scoped.json")
    assert err is None
    finding = da.Finding(
        ecosystem="pypi",
        section="python-dev",
        package="urllib3",
        version="1.26.18",
        advisory_id="PYSEC-2026-1995",
        aliases=("GHSA-34jh-p97f-mpxf",),
    )
    remaining, accepted = da.apply_exceptions(
        [finding], rules, today=date(2026, 9, 30)
    )
    assert remaining == []
    assert accepted
    assert accepted[0]["finding"]["advisory_id"] == "PYSEC-2026-1995"
    assert accepted[0]["exception"]["scope"] == "python-dev"


def test_exceptions_wrong_scope_not_applied() -> None:
    rules, err = da.load_exceptions(FIXTURES / "exceptions_wrong_scope.json")
    assert err is None
    finding = da.Finding(
        ecosystem="pypi",
        section="python-dev",
        package="urllib3",
        version="1.26.18",
        advisory_id="PYSEC-2026-1995",
    )
    remaining, accepted = da.apply_exceptions(
        [finding], rules, today=date(2026, 9, 30)
    )
    assert remaining == [finding]
    assert accepted == []


def test_exceptions_expired() -> None:
    rules, err = da.load_exceptions(FIXTURES / "exceptions_expired.json")
    assert err is None
    finding = da.Finding(
        ecosystem="pypi",
        section="python-dev",
        package="urllib3",
        version="1.26.18",
        advisory_id="PYSEC-2026-1995",
    )
    remaining, accepted = da.apply_exceptions(
        [finding], rules, today=date(2026, 9, 30)
    )
    assert remaining and not accepted


def test_exceptions_wildcard_rejected() -> None:
    rules, err = da.load_exceptions(FIXTURES / "exceptions_malformed_wildcard.json")
    assert rules == []
    assert err == "exceptions_malformed"


def test_exceptions_runtime_dev_not_shared() -> None:
    rules, _ = da.load_exceptions(FIXTURES / "exceptions_valid_scoped.json")
    runtime_finding = da.Finding(
        ecosystem="pypi",
        section="python-runtime",
        package="urllib3",
        version="1.26.18",
        advisory_id="PYSEC-2026-1995",
    )
    remaining, accepted = da.apply_exceptions(
        [runtime_finding], rules, today=date(2026, 9, 30)
    )
    assert remaining == [runtime_finding]
    assert accepted == []


def test_marker_strip_and_runtime_vs_scanner_python() -> None:
    """Target is 3.14.6 linux; scanner Python is irrelevant to kept pins."""
    export = (
        "colorama==0.4.6 ; sys_platform == 'win32' \\\n"
        "    --hash=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        "uvloop==0.22.1 ; platform_python_implementation != 'PyPy' "
        "and sys_platform != 'cygwin' and sys_platform != 'win32' \\\n"
        "    --hash=sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n"
        "fetchnow==0.1.0 \\\n"
        "    --hash=sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc\n"
        "packaging==26.3 \\\n"
        "    --hash=sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd\n"
    )
    filtered, expected, err = da.filter_export_for_target(
        export, target=da.RUNTIME_TARGET
    )
    assert err is None
    assert "colorama" not in expected
    assert "fetchnow" not in expected
    assert expected["uvloop"] == "0.22.1"
    assert expected["packaging"] == "26.3"
    # Markers stripped from scanner input
    assert ";" not in filtered
    assert "sys_platform" not in filtered
    assert "uvloop==0.22.1" in filtered
    assert "--hash=sha256:bbbb" in filtered


def test_export_silent_loss_not_empty_clean() -> None:
    # Comment-only export is non-empty text but yields no requirement blocks.
    export2 = "# only comments\n"
    filtered, expected, err = da.filter_export_for_target(
        export2, target=da.RUNTIME_TARGET
    )
    assert filtered == ""
    assert expected == {}
    assert err in {"export_silent_loss", "export_empty"}


def test_orphan_continuation_parse_error() -> None:
    export = "    --hash=sha256:abcd\npackaging==26.3\n"
    _, _, err = da.filter_export_for_target(export, target=da.RUNTIME_TARGET)
    assert err == "export_parse_error"


def test_required_sections_validation() -> None:
    assert da.validate_required_sections([]) == "sections_invalid"
    one = [da.SectionResult(name="npm", status=da.STATUS_CLEAN)]
    assert da.validate_required_sections(one) == "sections_invalid"
    dup = [
        da.SectionResult(name="npm", status=da.STATUS_CLEAN),
        da.SectionResult(name="npm", status=da.STATUS_CLEAN),
        da.SectionResult(name="python-dev", status=da.STATUS_CLEAN),
    ]
    assert da.validate_required_sections(dup) == "sections_invalid"
    ok = [
        da.SectionResult(name="npm", status=da.STATUS_CLEAN),
        da.SectionResult(name="python-runtime", status=da.STATUS_CLEAN),
        da.SectionResult(name="python-dev", status=da.STATUS_CLEAN),
    ]
    assert da.validate_required_sections(ok) is None


def test_exit2_with_exception_covered_finding_still_fails(tmp_path: Path) -> None:
    repo = tmp_path
    backend = repo / "backend"
    backend.mkdir()
    (backend / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    pip_audit = tmp_path / "pip-audit"
    pip_audit.write_text("x", encoding="utf-8")
    export = (
        "urllib3==1.26.18 \\\n"
        "    --hash=sha256:34b97092d7e0a3a8cf7cd10e386f401b3737364026c45e622aa02903dffe0f07\n"
    )
    audit_json = (FIXTURES / "pip_audit_findings.json").read_text(encoding="utf-8")
    rules, _ = da.load_exceptions(FIXTURES / "exceptions_valid_scoped.json")

    def runner(argv, **kwargs):  # noqa: ANN001
        pinned = _pinned_tool_versions(argv)
        if pinned is not None:
            return pinned
        if list(argv[:3]) == ["uv", "lock", "--check"]:
            return _result(argv=argv, returncode=0)
        if list(argv[:2]) == ["uv", "export"]:
            return _result(argv=argv, returncode=0, stdout=export)
        if str(argv[0]).endswith("pip-audit"):
            # exit 2 technical failure even with findings JSON on stdout
            return _result(argv=argv, returncode=2, stdout=audit_json)
        return _result(argv=argv, stdout="ok\n")

    section = da.run_python_section(
        repo=repo,
        runner=runner,
        pip_audit_bin=pip_audit,
        uv_bin="uv",
        target=da.DEV_TARGET,
        exceptions=rules,
        today=date(2026, 9, 30),
    )
    assert section.status == da.STATUS_SCANNER_ERROR
    assert section.details.get("error_code") == "scanner_bad_exit"


def test_exit0_empty_stdout_fails(tmp_path: Path) -> None:
    repo = tmp_path
    backend = repo / "backend"
    backend.mkdir()
    (backend / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    pip_audit = tmp_path / "pip-audit"
    pip_audit.write_text("x", encoding="utf-8")
    export = (
        "packaging==26.3 \\\n"
        "    --hash=sha256:94edc256424af38762eb31306eed28beb9f0efc50a8837492c9d6fd6004aed79\n"
    )

    def runner(argv, **kwargs):  # noqa: ANN001
        pinned = _pinned_tool_versions(argv)
        if pinned is not None:
            return pinned
        if list(argv[:3]) == ["uv", "lock", "--check"]:
            return _result(argv=argv, returncode=0)
        if list(argv[:2]) == ["uv", "export"]:
            return _result(argv=argv, returncode=0, stdout=export)
        if str(argv[0]).endswith("pip-audit"):
            return _result(argv=argv, returncode=0, stdout="")
        return _result(argv=argv, stdout="ok\n")

    section = da.run_python_section(
        repo=repo,
        runner=runner,
        pip_audit_bin=pip_audit,
        uv_bin="uv",
        target=da.RUNTIME_TARGET,
        exceptions=[],
        today=date(2026, 9, 30),
    )
    assert section.status == da.STATUS_SCANNER_ERROR
    assert section.details.get("error_code") == "scanner_empty_output"


def test_exit0_truncated_json_fails(tmp_path: Path) -> None:
    repo = tmp_path
    backend = repo / "backend"
    backend.mkdir()
    (backend / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    pip_audit = tmp_path / "pip-audit"
    pip_audit.write_text("x", encoding="utf-8")
    export = (
        "packaging==26.3 \\\n"
        "    --hash=sha256:94edc256424af38762eb31306eed28beb9f0efc50a8837492c9d6fd6004aed79\n"
    )
    truncated = (FIXTURES / "pip_audit_truncated.json").read_text(encoding="utf-8")

    def runner(argv, **kwargs):  # noqa: ANN001
        pinned = _pinned_tool_versions(argv)
        if pinned is not None:
            return pinned
        if list(argv[:3]) == ["uv", "lock", "--check"]:
            return _result(argv=argv, returncode=0)
        if list(argv[:2]) == ["uv", "export"]:
            return _result(argv=argv, returncode=0, stdout=export)
        if str(argv[0]).endswith("pip-audit"):
            return _result(argv=argv, returncode=0, stdout=truncated)
        return _result(argv=argv, stdout="ok\n")

    section = da.run_python_section(
        repo=repo,
        runner=runner,
        pip_audit_bin=pip_audit,
        uv_bin="uv",
        target=da.RUNTIME_TARGET,
        exceptions=[],
        today=date(2026, 9, 30),
    )
    assert section.status == da.STATUS_MALFORMED


def test_timeout_not_clean(tmp_path: Path) -> None:
    repo = tmp_path
    web = repo / "web"
    web.mkdir()
    (web / "package-lock.json").write_text("{}\n", encoding="utf-8")

    def runner(argv, **kwargs):  # noqa: ANN001
        if argv[:2] == ["npm", "audit"]:
            raise BoundedTimeoutError("timed out")
        return _result(argv=argv, stdout="v1\n")

    section = da.run_npm_section(
        repo=repo, runner=runner, exceptions=[], today=date(2026, 9, 30)
    )
    assert section.status == da.STATUS_TIMEOUT


def test_cancelled_not_clean(tmp_path: Path) -> None:
    repo = tmp_path
    web = repo / "web"
    web.mkdir()
    (web / "package-lock.json").write_text("{}\n", encoding="utf-8")

    def runner(argv, **kwargs):  # noqa: ANN001
        if argv[:2] == ["npm", "audit"]:
            raise BoundedCancelledError("cancelled")
        return _result(argv=argv, stdout="v1\n")

    section = da.run_npm_section(
        repo=repo, runner=runner, exceptions=[], today=date(2026, 9, 30)
    )
    assert section.status == da.STATUS_CANCELLED


def test_output_limit_not_clean(tmp_path: Path) -> None:
    repo = tmp_path
    web = repo / "web"
    web.mkdir()
    (web / "package-lock.json").write_text("{}\n", encoding="utf-8")

    def runner(argv, **kwargs):  # noqa: ANN001
        if argv[:2] == ["npm", "audit"]:
            raise OutputLimitExceededError("stdout exceeded")
        return _result(argv=argv, stdout="v1\n")

    section = da.run_npm_section(
        repo=repo, runner=runner, exceptions=[], today=date(2026, 9, 30)
    )
    assert section.status == da.STATUS_INCOMPLETE


def test_no_file_fallback_clean_path_uses_stdout(tmp_path: Path) -> None:
    repo = tmp_path
    backend = repo / "backend"
    backend.mkdir()
    (backend / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    pip_audit = tmp_path / "pip-audit"
    pip_audit.write_text("x", encoding="utf-8")
    export = (
        "packaging==26.3 \\\n"
        "    --hash=sha256:94edc256424af38762eb31306eed28beb9f0efc50a8837492c9d6fd6004aed79\n"
    )
    audit_json = json.dumps(
        {
            "dependencies": [{"name": "packaging", "version": "26.3", "vulns": []}],
            "fixes": [],
        }
    )
    seen_argv: list[list[str]] = []

    def runner(argv, **kwargs):  # noqa: ANN001
        pinned = _pinned_tool_versions(argv)
        if pinned is not None:
            return pinned
        if list(argv[:3]) == ["uv", "lock", "--check"]:
            return _result(argv=argv, returncode=0)
        if list(argv[:2]) == ["uv", "export"]:
            return _result(argv=argv, returncode=0, stdout=export)
        if str(argv[0]).endswith("pip-audit"):
            seen_argv.append([str(a) for a in argv])
            assert "-o" not in argv
            return _result(argv=argv, returncode=0, stdout=audit_json)
        return _result(argv=argv, stdout="ok\n")

    section = da.run_python_section(
        repo=repo,
        runner=runner,
        pip_audit_bin=pip_audit,
        uv_bin="uv",
        target=da.RUNTIME_TARGET,
        exceptions=[],
        today=date(2026, 9, 30),
    )
    assert section.status == da.STATUS_CLEAN
    assert section.details.get("json_channel") == "stdout"
    assert seen_argv and "-o" not in seen_argv[0]


def test_sanitize_redacts_credentials_not_just_length() -> None:
    sample = (FIXTURES / "secrets_sample.txt").read_text(encoding="utf-8")
    out = da._sanitize_public_text(sample, limit=2000)
    assert "super-secret-value" not in out
    assert "abcdef123456" not in out
    assert "eyJhbGciOiJIUzI1NiJ9.fake" not in out
    assert "hunter2" not in out
    assert "tok_live_001" not in out
    assert "***" in out


def test_safe_error_allowlisted_only() -> None:
    err = da._safe_error(
        "scanner_bad_exit",
        context={
            "returncode": 2,
            "password": "should-not-appear",
            "stderr": "secret-token=abc",
        },
    )
    assert err["error_code"] == "scanner_bad_exit"
    assert "password" not in err.get("error_context", {})
    assert "stderr" not in err.get("error_context", {})


def test_full_gate_rejects_partial_sections(tmp_path: Path) -> None:
    """build_report must not be clean with missing required sections."""
    repo = tmp_path
    (repo / "web").mkdir()
    (repo / "backend").mkdir()
    sections = [
        da.SectionResult(name="npm", status=da.STATUS_CLEAN),
        da.SectionResult(name="python-runtime", status=da.STATUS_CLEAN),
        # missing python-dev
    ]
    report = da.build_report(
        repo=repo,
        sections=sections,
        runner=lambda *a, **k: _result(stdout="x\n"),
        pip_audit_bin=None,
        uv_bin="uv",
    )
    assert report["status"] != da.STATUS_CLEAN
    assert report["status"] == da.STATUS_INCOMPLETE


def test_cli_has_no_skip_flags() -> None:
    help_text = da.main.__doc__ or ""
    # argparse: parse known - ensure --skip-npm not in parser
    import argparse

    # Reconstruct by calling parser setup indirectly via main with --help is hard;
    # inspect source / ensure run_audit signature has no skip params.
    import inspect

    sig = inspect.signature(da.run_audit)
    assert "include_npm" not in sig.parameters
    assert "skip" not in str(sig).lower()
    src = Path(da.__file__).read_text(encoding="utf-8")
    assert "--skip-npm" not in src
    assert "--skip-python" not in src
    del argparse, help_text


def test_repo_exceptions_empty() -> None:
    path = ROOT / "docs" / "operations" / "dependency-exceptions.json"
    rules, err = da.load_exceptions(path)
    assert err is None
    assert rules == []


def test_accepted_exceptions_not_described_as_zero_vulns() -> None:
    finding = da.Finding(
        ecosystem="pypi",
        section="python-dev",
        package="urllib3",
        version="1.26.18",
        advisory_id="PYSEC-2026-1995",
        aliases=("GHSA-34jh-p97f-mpxf",),
    )
    rules, _ = da.load_exceptions(FIXTURES / "exceptions_valid_scoped.json")
    remaining, accepted = da.apply_exceptions(
        [finding], rules, today=date(2026, 9, 30)
    )
    assert remaining == []
    assert accepted
    disclaimer_src = Path(da.__file__).read_text(encoding="utf-8")
    assert "zero vulnerabilities" in disclaimer_src


def test_pip_audit_missing_expected_package() -> None:
    payload = {
        "dependencies": [{"name": "packaging", "version": "26.3", "vulns": []}],
        "fixes": [],
    }
    _, err, details = da.parse_pip_audit_json(
        payload,
        expected={"packaging": "26.3", "missingpkg": "1.0"},
        section="python-runtime",
    )
    assert err == "inventory_incomplete"
    assert details["missing"] == ["missingpkg"]


def test_pip_audit_unexpected_package() -> None:
    payload = {
        "dependencies": [
            {"name": "packaging", "version": "26.3", "vulns": []},
            {"name": "surprise", "version": "9.9.9", "vulns": []},
        ],
        "fixes": [],
    }
    _, err, details = da.parse_pip_audit_json(
        payload, expected={"packaging": "26.3"}, section="python-runtime"
    )
    assert err == "inventory_incomplete"
    assert details["unexpected"] == ["surprise"]


def test_stale_lock_fails_before_export_and_scanner(tmp_path: Path) -> None:
    repo = tmp_path
    backend = repo / "backend"
    backend.mkdir()
    (backend / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    pip_audit = tmp_path / "pip-audit"
    pip_audit.write_text("x", encoding="utf-8")
    calls: list[list[str]] = []

    def runner(argv, **kwargs):  # noqa: ANN001
        calls.append([str(a) for a in argv])
        pinned = _pinned_tool_versions(argv)
        if pinned is not None:
            return pinned
        if list(argv[:3]) == ["uv", "lock", "--check"]:
            return _result(argv=argv, returncode=1, stderr="lock out of date\n")
        raise AssertionError(f"forbidden call after stale lock: {argv!r}")

    section = da.run_python_section(
        repo=repo,
        runner=runner,
        pip_audit_bin=pip_audit,
        uv_bin="uv",
        target=da.RUNTIME_TARGET,
        exceptions=[],
        today=date(2026, 9, 30),
    )
    assert section.status == da.STATUS_SOURCE_UNAVAILABLE
    assert section.details.get("error_code") == "lock_stale"
    assert _calls_contain(calls, prefix=["uv", "lock", "--check"])
    assert not _calls_uv_export(calls)
    assert not _calls_pip_audit_scan(calls)


def test_missing_npm_lock_fails_before_scanner(tmp_path: Path) -> None:
    repo = tmp_path
    (repo / "web").mkdir()
    calls: list[list[str]] = []

    def runner(argv, **kwargs):  # noqa: ANN001
        calls.append([str(a) for a in argv])
        raise AssertionError(f"runner must not be called when lock missing: {argv!r}")

    section = da.run_npm_section(
        repo=repo, runner=runner, exceptions=[], today=date(2026, 9, 30)
    )
    assert section.status == da.STATUS_SOURCE_UNAVAILABLE
    assert section.details.get("error_code") == "missing_lock"
    assert calls == []
    assert not any(c[:2] == ["npm", "audit"] for c in calls)


def test_tool_versions_ok(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    pip_audit = tmp_path / "pip-audit"
    pip_audit.write_text("x", encoding="utf-8")

    def runner(argv, **kwargs):  # noqa: ANN001
        calls.append([str(a) for a in argv])
        pinned = _pinned_tool_versions(argv)
        assert pinned is not None
        return pinned

    details, err = da.verify_audit_tool_versions(
        pip_audit_bin=pip_audit,
        uv_bin="uv",
        runner=runner,
    )
    assert err is None
    assert details["uv_version_observed"] == "0.12.19"
    assert details["pip_audit_version_observed"] == "2.10.1"
    assert any(c[:2] == ["uv", "--version"] for c in calls)
    assert any(str(c[0]).endswith("pip-audit") and c[1] == "--version" for c in calls)

def test_tool_version_uv_mismatch_blocks_scan(tmp_path: Path) -> None:
    repo = tmp_path
    backend = repo / "backend"
    backend.mkdir()
    (backend / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    pip_audit = tmp_path / "pip-audit"
    pip_audit.write_text("x", encoding="utf-8")
    calls: list[list[str]] = []

    def runner(argv, **kwargs):  # noqa: ANN001
        calls.append([str(a) for a in argv])
        s = [str(a) for a in argv]
        if len(s) >= 2 and s[1] == "--version" and s[0] == "uv":
            return _result(argv=argv, stdout="uv 0.11.0\n")
        if len(s) >= 2 and s[1] == "--version" and s[0].endswith("pip-audit"):
            return _result(argv=argv, stdout="pip-audit 2.10.1\n")
        raise AssertionError(f"scan must not start after version failure: {argv!r}")

    section = da.run_python_section(
        repo=repo,
        runner=runner,
        pip_audit_bin=pip_audit,
        uv_bin="uv",
        target=da.RUNTIME_TARGET,
        exceptions=[],
        today=date(2026, 9, 30),
    )
    assert section.status == da.STATUS_SCANNER_ERROR
    assert section.details.get("error_code") == "tool_version_mismatch"
    assert section.details.get("error_context", {}).get("tool") == "uv"
    assert not _calls_contain(calls, prefix=["uv", "lock", "--check"])
    assert not _calls_uv_export(calls)
    assert not _calls_pip_audit_scan(calls)


def test_tool_version_pip_audit_mismatch_blocks_scan(tmp_path: Path) -> None:
    repo = tmp_path
    backend = repo / "backend"
    backend.mkdir()
    (backend / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    pip_audit = tmp_path / "pip-audit"
    pip_audit.write_text("x", encoding="utf-8")
    calls: list[list[str]] = []

    def runner(argv, **kwargs):  # noqa: ANN001
        calls.append([str(a) for a in argv])
        s = [str(a) for a in argv]
        if len(s) >= 2 and s[1] == "--version" and s[0] == "uv":
            return _result(argv=argv, stdout="uv 0.12.19\n")
        if len(s) >= 2 and s[1] == "--version" and s[0].endswith("pip-audit"):
            return _result(argv=argv, stdout="pip-audit 2.9.0\n")
        raise AssertionError(f"scan must not start after version failure: {argv!r}")

    section = da.run_python_section(
        repo=repo,
        runner=runner,
        pip_audit_bin=pip_audit,
        uv_bin="uv",
        target=da.DEV_TARGET,
        exceptions=[],
        today=date(2026, 9, 30),
    )
    assert section.status == da.STATUS_SCANNER_ERROR
    assert section.details.get("error_code") == "tool_version_mismatch"
    assert section.details.get("error_context", {}).get("tool") == "pip-audit"
    assert not _calls_uv_export(calls)
    assert not _calls_pip_audit_scan(calls)


def test_tool_version_empty_output_blocks_scan(tmp_path: Path) -> None:
    repo = tmp_path
    backend = repo / "backend"
    backend.mkdir()
    (backend / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    pip_audit = tmp_path / "pip-audit"
    pip_audit.write_text("x", encoding="utf-8")
    calls: list[list[str]] = []

    def runner(argv, **kwargs):  # noqa: ANN001
        calls.append([str(a) for a in argv])
        s = [str(a) for a in argv]
        if len(s) >= 2 and s[1] == "--version":
            return _result(argv=argv, returncode=0, stdout="")
        raise AssertionError(f"scan must not start: {argv!r}")

    section = da.run_python_section(
        repo=repo,
        runner=runner,
        pip_audit_bin=pip_audit,
        uv_bin="uv",
        target=da.RUNTIME_TARGET,
        exceptions=[],
        today=date(2026, 9, 30),
    )
    assert section.status == da.STATUS_SCANNER_ERROR
    assert section.details.get("error_code") == "tool_version_unreadable"
    assert not _calls_pip_audit_scan(calls)


def test_tool_version_garbage_output_blocks_scan(tmp_path: Path) -> None:
    repo = tmp_path
    backend = repo / "backend"
    backend.mkdir()
    (backend / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    pip_audit = tmp_path / "pip-audit"
    pip_audit.write_text("x", encoding="utf-8")
    calls: list[list[str]] = []

    def runner(argv, **kwargs):  # noqa: ANN001
        calls.append([str(a) for a in argv])
        s = [str(a) for a in argv]
        if len(s) >= 2 and s[1] == "--version" and s[0] == "uv":
            return _result(argv=argv, stdout="not-a-version-line\n")
        raise AssertionError(f"scan must not start: {argv!r}")

    section = da.run_python_section(
        repo=repo,
        runner=runner,
        pip_audit_bin=pip_audit,
        uv_bin="uv",
        target=da.RUNTIME_TARGET,
        exceptions=[],
        today=date(2026, 9, 30),
    )
    assert section.status == da.STATUS_SCANNER_ERROR
    assert section.details.get("error_code") == "tool_version_unreadable"
    assert not _calls_contain(calls, prefix=["uv", "lock", "--check"])


def test_tool_version_probe_error_blocks_scan(tmp_path: Path) -> None:
    repo = tmp_path
    backend = repo / "backend"
    backend.mkdir()
    (backend / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    pip_audit = tmp_path / "pip-audit"
    pip_audit.write_text("x", encoding="utf-8")
    calls: list[list[str]] = []

    def runner(argv, **kwargs):  # noqa: ANN001
        calls.append([str(a) for a in argv])
        s = [str(a) for a in argv]
        if len(s) >= 2 and s[1] == "--version":
            raise BoundedTimeoutError("version probe timed out")
        raise AssertionError(f"scan must not start: {argv!r}")

    section = da.run_python_section(
        repo=repo,
        runner=runner,
        pip_audit_bin=pip_audit,
        uv_bin="uv",
        target=da.RUNTIME_TARGET,
        exceptions=[],
        today=date(2026, 9, 30),
    )
    assert section.status == da.STATUS_SCANNER_ERROR
    assert section.details.get("error_code") == "tool_version_unreadable"
    assert not _calls_uv_export(calls)
    assert not _calls_pip_audit_scan(calls)


def test_run_audit_version_failure_skips_all_scanners(tmp_path: Path) -> None:
    repo = tmp_path
    (repo / "web").mkdir()
    (repo / "backend").mkdir()
    (repo / "backend" / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (repo / "web" / "package-lock.json").write_text("{}\n", encoding="utf-8")
    exceptions = tmp_path / "exceptions.json"
    exceptions.write_text(
        '{"version": 1, "exceptions": []}\n', encoding="utf-8"
    )
    pip_audit = tmp_path / "pip-audit"
    pip_audit.write_text("x", encoding="utf-8")
    calls: list[list[str]] = []

    def runner(argv, **kwargs):  # noqa: ANN001
        calls.append([str(a) for a in argv])
        s = [str(a) for a in argv]
        if len(s) >= 2 and s[1] == "--version" and s[0] == "uv":
            return _result(argv=argv, stdout="uv 9.9.9\n")
        if len(s) >= 2 and s[1] == "--version" and s[0].endswith("pip-audit"):
            return _result(argv=argv, stdout="pip-audit 2.10.1\n")
        if len(s) >= 2 and s[1] == "--version":
            return _result(argv=argv, stdout="v1\n")
        if s[:2] == ["git", "rev-parse"]:
            return _result(argv=argv, stdout="a" * 40 + "\n")
        if s[:2] == ["npm", "audit"] or (
            len(s) >= 2 and s[0] == "uv" and s[1] in {"export", "lock"}
        ):
            raise AssertionError(f"no section scanners after pin failure: {argv!r}")
        if s and str(s[0]).endswith("pip-audit") and "--version" not in s:
            raise AssertionError(f"no pip-audit scan after pin failure: {argv!r}")
        return _result(argv=argv, stdout="ok\n")

    report, code = da.run_audit(
        repo=repo,
        exceptions_path=exceptions,
        pip_audit_bin=pip_audit,
        uv_bin="uv",
        runner=runner,
        today=date(2026, 9, 30),
    )
    assert code == 1
    assert report["status"] == da.STATUS_SCANNER_ERROR
    assert not any(c[:2] == ["npm", "audit"] for c in calls)
    assert not _calls_uv_export(calls)
    assert not _calls_pip_audit_scan(calls)


def test_npm_transitive_chain_resolves_without_reassigning_parent() -> None:
    payload = _load_json("npm_audit_transitive_chain.json")
    lock = _load_json("npm_package_lock_chain.json")
    findings, err, details = da.parse_npm_audit_json(payload, lock_data=lock)
    assert err is None
    assert {f.package for f in findings} == {"child-pkg"}
    assert findings[0].advisory_id == "GHSA-1111-2222-3333"
    assert findings[0].version == "0.9.0"
    # Counts are distinct concepts — do not require equality.
    assert details["vulnerable_package_entries"] == 2
    assert details["unique_advisory_ids"] == 1
    assert details["package_instance_findings"] == 1


def test_npm_mixed_via() -> None:
    payload = _load_json("npm_audit_mixed_via.json")
    lock = _load_json("npm_package_lock_chain.json")
    findings, err, _ = da.parse_npm_audit_json(payload, lock_data=lock)
    assert err is None
    by_pkg = {(f.package, f.advisory_id, f.version) for f in findings}
    assert ("example-pkg", "GHSA-aaaa-bbbb-cccc", "1.2.2") in by_pkg
    assert ("child-pkg", "GHSA-dddd-eeee-ffff", "0.9.0") in by_pkg
    # Parent must not inherit child advisory id as its own object finding.
    assert ("example-pkg", "GHSA-dddd-eeee-ffff", "1.2.2") not in by_pkg


def test_npm_multi_installed_versions() -> None:
    payload = _load_json("npm_audit_multi_version.json")
    lock = _load_json("npm_package_lock_chain.json")
    findings, err, details = da.parse_npm_audit_json(payload, lock_data=lock)
    assert err is None
    versions = sorted(f.version for f in findings if f.version)
    assert versions == ["1.0.0", "1.1.0"]
    assert details["unique_advisory_ids"] == 1
    assert details["package_instance_findings"] == 2


def test_npm_missing_string_target() -> None:
    payload = _load_json("npm_audit_string_via_only.json")
    _, err, _ = da.parse_npm_audit_json(payload, lock_data={})
    assert err == "inventory_incomplete"


def test_npm_cycle_string_only_incomplete() -> None:
    payload = _load_json("npm_audit_cycle_string_only.json")
    _, err, _ = da.parse_npm_audit_json(payload, lock_data={})
    assert err == "inventory_incomplete"


def test_npm_good_branch_plus_broken_independent_branch_fails() -> None:
    payload = _load_json("npm_audit_good_plus_broken_branch.json")
    lock = _load_json("npm_package_lock_chain.json")
    findings, err, _ = da.parse_npm_audit_json(payload, lock_data=lock)
    assert err == "inventory_incomplete"
    # Parser may have collected the good finding before failing the broken branch;
    # either way the section must not be accepted as complete.
    assert err is not None


def _npm_repo(tmp_path: Path) -> Path:
    repo = tmp_path
    web = repo / "web"
    web.mkdir()
    (web / "package-lock.json").write_text(
        (FIXTURES / "npm_package_lock_min.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return repo


def test_npm_decode_failure_distinct_from_schema(tmp_path: Path) -> None:
    repo = _npm_repo(tmp_path)

    def decode_runner(argv, **kwargs):  # noqa: ANN001
        if argv[:2] == ["npm", "audit"]:
            return _result(
                argv=argv,
                returncode=1,
                stdout="{not-json",
                stderr="token=super-secret-value\n",
            )
        return _result(argv=argv, stdout="v1\n")

    decode = da.run_npm_section(
        repo=repo, runner=decode_runner, exceptions=[], today=date(2026, 9, 30)
    )
    assert decode.status == da.STATUS_MALFORMED
    assert decode.details["error_code"] == "json_malformed"
    assert decode.details["failure_stage"] == da.NPM_STAGE_DECODE
    assert decode.details["diagnostic_reason"] == da.NPM_REASON_JSON_DECODE
    assert decode.details["scanner_returncode"] == 1
    assert decode.details["stdout_bytes"] == len(b"{not-json")
    assert decode.details["stderr_bytes"] > 0
    assert isinstance(decode.details["stdout_sha256"], str)
    assert len(decode.details["stdout_sha256"]) == 64

    def schema_runner(argv, **kwargs):  # noqa: ANN001
        if argv[:2] == ["npm", "audit"]:
            # Valid JSON but wrong audit report version → schema malformed.
            body = json.dumps(
                {
                    "auditReportVersion": 1,
                    "metadata": {
                        "vulnerabilities": {
                            "info": 0,
                            "low": 0,
                            "moderate": 0,
                            "high": 0,
                            "critical": 0,
                            "total": 0,
                        },
                        "dependencies": {
                            "prod": 1,
                            "dev": 0,
                            "optional": 0,
                            "total": 1,
                        },
                    },
                    "vulnerabilities": {},
                    "password": "should-never-appear",
                    "error": {
                        "code": "ENOAUDIT",
                        "summary": "secret=leak-me",
                        "detail": "https://user:pass@registry.example/x",
                    },
                }
            )
            return _result(argv=argv, returncode=1, stdout=body)
        return _result(argv=argv, stdout="v1\n")

    schema = da.run_npm_section(
        repo=repo, runner=schema_runner, exceptions=[], today=date(2026, 9, 30)
    )
    assert schema.status == da.STATUS_MALFORMED
    assert schema.details["error_code"] == "json_malformed"
    assert schema.details["failure_stage"] == da.NPM_STAGE_SCHEMA
    assert schema.details["diagnostic_reason"] == da.NPM_REASON_SCHEMA_MALFORMED
    assert schema.details["scanner_returncode"] == 1
    assert schema.details["json_root_type"] == "dict"
    assert "auditReportVersion" in schema.details["expected_fields_present"]
    assert schema.details["audit_report_version"] == 1
    assert schema.details["has_npm_error_object"] is True
    assert schema.details["npm_error_code"] == "ENOAUDIT"
    # Distinct stages for the same high-level error_code.
    assert decode.details["failure_stage"] != schema.details["failure_stage"]
    assert decode.details["diagnostic_reason"] != schema.details["diagnostic_reason"]


def test_npm_failure_diagnostics_preserve_returncode(tmp_path: Path) -> None:
    repo = _npm_repo(tmp_path)

    def runner(argv, **kwargs):  # noqa: ANN001
        if argv[:2] == ["npm", "audit"]:
            return _result(argv=argv, returncode=7, stdout="{")
        return _result(argv=argv, stdout="v1\n")

    section = da.run_npm_section(
        repo=repo, runner=runner, exceptions=[], today=date(2026, 9, 30)
    )
    assert section.details["scanner_returncode"] == 7
    assert section.details["error_context"]["returncode"] == 7


def test_npm_failure_diagnostics_redact_secrets_from_report(tmp_path: Path) -> None:
    repo = _npm_repo(tmp_path)
    secret = "tok_live_SHOULD_NOT_LEAK"
    body = json.dumps(
        {
            "auditReportVersion": "not-an-int",
            "token": secret,
            "url": f"https://user:{secret}@example.com/p",
            "error": {
                "code": "not valid code!!",
                "summary": f"password={secret}",
            },
            "metadata": "nope",
        }
    )

    def runner(argv, **kwargs):  # noqa: ANN001
        if argv[:2] == ["npm", "audit"]:
            return _result(argv=argv, returncode=1, stdout=body, stderr=secret)
        return _result(argv=argv, stdout="v1\n")

    section = da.run_npm_section(
        repo=repo, runner=runner, exceptions=[], today=date(2026, 9, 30)
    )
    report = da.build_report(
        repo=repo,
        sections=[section],
        runner=lambda *a, **k: _result(stdout="x\n"),
        pip_audit_bin=None,
        uv_bin="uv",
    )
    dumped = json.dumps(report)
    assert secret not in dumped
    assert "user:" not in dumped
    assert "password=" not in dumped
    # Invalid npm error code shape must not be copied through.
    assert "npm_error_code" not in section.details
    assert section.details["has_npm_error_object"] is True
    assert section.status != da.STATUS_CLEAN


def test_npm_diagnostics_do_not_convert_failure_to_clean(tmp_path: Path) -> None:
    repo = _npm_repo(tmp_path)

    def runner(argv, **kwargs):  # noqa: ANN001
        if argv[:2] == ["npm", "audit"]:
            return _result(argv=argv, returncode=0, stdout="not-json")
        return _result(argv=argv, stdout="v1\n")

    section = da.run_npm_section(
        repo=repo, runner=runner, exceptions=[], today=date(2026, 9, 30)
    )
    assert section.status == da.STATUS_MALFORMED
    assert section.details["failure_stage"] == da.NPM_STAGE_DECODE
    assert section.findings == []
    assert section.details.get("note") != "clean"
