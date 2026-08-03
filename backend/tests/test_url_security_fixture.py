"""Structural tests for URL security fixture (no validator execution)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "url_security_cases.json"
MIN_CASES = 35
REQUIRED_CASE_FIELDS = ("id", "url", "expected", "reason_code", "rationale")
ALLOWED_EXPECTED = frozenset({"allow", "deny"})


@pytest.fixture(scope="module")
def fixture_payload() -> dict[str, Any]:
    raw = FIXTURE_PATH.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert isinstance(payload, dict)
    return payload


def test_fixture_is_valid_json_object(fixture_payload: dict[str, Any]) -> None:
    assert "cases" in fixture_payload
    assert "schema" in fixture_payload
    assert fixture_payload.get("schema_version") == 1


def test_fixture_has_minimum_case_count(fixture_payload: dict[str, Any]) -> None:
    cases = fixture_payload["cases"]
    assert isinstance(cases, list)
    assert len(cases) >= MIN_CASES


def test_case_ids_are_unique(fixture_payload: dict[str, Any]) -> None:
    ids = [case["id"] for case in fixture_payload["cases"]]
    assert len(ids) == len(set(ids))


def test_each_case_has_required_fields_and_decision(
    fixture_payload: dict[str, Any],
) -> None:
    for case in fixture_payload["cases"]:
        assert isinstance(case, dict)
        for field in REQUIRED_CASE_FIELDS:
            assert field in case, f"missing {field} in {case.get('id')}"
            assert case[field], f"empty {field} in {case.get('id')}"
        assert case["expected"] in ALLOWED_EXPECTED
        assert isinstance(case["rationale"], str)
        assert len(case["rationale"].strip()) >= 10


def test_fixture_includes_required_deny_and_allow_coverage(
    fixture_payload: dict[str, Any],
) -> None:
    ids = {case["id"] for case in fixture_payload["cases"]}
    required_ids = {
        "allow-vk-https",
        "allow-vk-path-query",
        "allow-mixed-case-host",
        "allow-explicit-subdomain",
        "allow-unicode-idna",
        "deny-localhost-name",
        "deny-127-0-0-1",
        "deny-127-1",
        "deny-0-0-0-0",
        "deny-10-0-0-1",
        "deny-172-16-0-1",
        "deny-192-168-1-1",
        "deny-cgnat-100-64",
        "deny-metadata-169-254-169-254",
        "deny-ipv6-loopback",
        "deny-ipv6-link-local",
        "deny-ipv4-mapped-ipv6-private",
        "deny-embedded-credentials",
        "deny-file-scheme",
        "deny-ftp-scheme",
        "deny-scheme-relative",
        "deny-unsupported-port",
        "deny-deceptive-suffix",
        "deny-deceptive-prefix",
        "deny-percent-encoded-hostname-trick",
        "deny-userinfo-trusted-hostname",
        "deny-decimal-ip",
        "deny-hex-ip",
        "deny-redirect-to-private",
        "deny-dns-private-answer",
        "deny-dns-mixed-public-private",
    }
    missing = required_ids - ids
    assert not missing, f"missing required case ids: {sorted(missing)}"


def test_loader_does_not_invoke_url_validator() -> None:
    """PR0B guard: no fetchnow URL validator module should exist yet."""
    import importlib.util

    def _spec_exists(name: str) -> bool:
        try:
            return importlib.util.find_spec(name) is not None
        except ModuleNotFoundError:
            return False

    assert not _spec_exists("fetchnow.security.url_validator")
    assert not _spec_exists("fetchnow.url_validator")
    # Ensure this test file itself does not import a validator implementation.
    assert "fetchnow.security" not in globals()

