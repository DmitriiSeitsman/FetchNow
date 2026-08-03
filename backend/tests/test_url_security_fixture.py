"""Execute URL security fixture cases against the real validator."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fetchnow.core.config import Settings
from fetchnow.url.dns import FakeDnsResolver
from fetchnow.url.errors import URLValidationError
from fetchnow.url.providers import ProviderRegistry
from fetchnow.url.validate import URLValidator

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "url_security_cases.json"
MIN_CASES = 35
REQUIRED_CASE_FIELDS = ("id", "url", "expected", "reason_code", "rationale", "stage")
ALLOWED_EXPECTED = frozenset({"allow", "deny"})
ALLOWED_STAGES = frozenset({"validate", "network"})


@pytest.fixture(scope="module")
def fixture_payload() -> dict[str, Any]:
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_fixture_is_valid_json_object(fixture_payload: dict[str, Any]) -> None:
    assert "cases" in fixture_payload
    assert "schema" in fixture_payload
    assert fixture_payload.get("schema_version") >= 2


def test_fixture_has_minimum_case_count(fixture_payload: dict[str, Any]) -> None:
    assert len(fixture_payload["cases"]) >= 50


def test_case_ids_are_unique(fixture_payload: dict[str, Any]) -> None:
    ids = [case["id"] for case in fixture_payload["cases"]]
    assert len(ids) == len(set(ids))


def test_each_case_has_required_fields(fixture_payload: dict[str, Any]) -> None:
    for case in fixture_payload["cases"]:
        for field in REQUIRED_CASE_FIELDS:
            assert case.get(field), f"missing {field} in {case.get('id')}"
        assert case["expected"] in ALLOWED_EXPECTED
        assert case["stage"] in ALLOWED_STAGES
        assert len(case["rationale"].strip()) >= 10


def test_every_case_is_categorized(fixture_payload: dict[str, Any]) -> None:
    uncategorized = [
        case["id"]
        for case in fixture_payload["cases"]
        if case.get("stage") not in ALLOWED_STAGES
    ]
    assert not uncategorized


def test_redirect_future_cases_are_documented(fixture_payload: dict[str, Any]) -> None:
    deferred = [c for c in fixture_payload["cases"] if c["stage"] == "redirect_future"]
    assert not deferred, "redirect_future cases must be promoted to network in PR2"
    network = [c for c in fixture_payload["cases"] if c["stage"] == "network"]
    assert network, "expected network-stage cases"
    for case in network:
        assert case.get("rationale")


def _settings() -> Settings:
    return Settings(
        APP_ENV="test",
        LOG_LEVEL="WARNING",
        URL_ALLOWED_SCHEMES="http,https",
        URL_ALLOWED_PORTS="80,443",
        URL_MAX_LENGTH=4096,
        DNS_RESOLUTION_TIMEOUT_SECONDS=1,
        PROVIDER_VK_ENABLED=True,
        PROVIDER_RUTUBE_ENABLED=True,
    )


def _validator_for_case(case: dict[str, Any]) -> URLValidator:
    settings = _settings()
    registry = ProviderRegistry.from_settings(settings)
    extra_hosts = case.get("extra_allowlist_hosts") or []
    if extra_hosts:
        registry = registry.with_extra_exact_hosts(
            provider_id=case.get("expected_provider") or "fixture-extra",
            display_name="Fixture Extra",
            hostnames=extra_hosts,
        )

    records: dict[str, list[str]] = {}
    timeouts: set[str] = set()
    empty: set[str] = set()

    # Apply DNS stub by expected hostname / URL host when provided.
    host_hint = case.get("expected_hostname")
    if case.get("dns_behavior") == "timeout" and host_hint:
        timeouts.add(host_hint)
    elif case.get("dns_behavior") == "empty" and host_hint:
        empty.add(host_hint)
    elif case.get("dns_addresses") is not None:
        # Map addresses onto allowlisted hostnames that need resolution.
        for provider in registry.providers:
            for host in provider.exact_hostnames:
                records[host] = list(case["dns_addresses"])
        if host_hint:
            records[host_hint] = list(case["dns_addresses"])

    resolver = FakeDnsResolver(
        records=records,
        timeouts=timeouts,
        empty=empty,
        default_addresses=("8.8.8.8",),
    )
    return URLValidator(settings, registry=registry, resolver=resolver)


@pytest.mark.asyncio
async def test_validate_stage_cases_against_validator(
    fixture_payload: dict[str, Any],
) -> None:
    validate_cases = [c for c in fixture_payload["cases"] if c["stage"] == "validate"]
    assert len(validate_cases) >= 35

    for case in validate_cases:
        validator = _validator_for_case(case)
        if case["expected"] == "allow":
            result = await validator.validate(case["url"])
            if case.get("expected_hostname"):
                assert result.url.hostname == case["expected_hostname"]
            if case.get("expected_provider"):
                assert result.provider_id == case["expected_provider"]
        else:
            with pytest.raises(URLValidationError) as excinfo:
                await validator.validate(case["url"])
            assert excinfo.value.code == case["reason_code"], (
                f"{case['id']}: expected {case['reason_code']}, "
                f"got {excinfo.value.code}"
            )


def test_validator_module_exists() -> None:
    import importlib.util

    assert importlib.util.find_spec("fetchnow.url.validate") is not None
