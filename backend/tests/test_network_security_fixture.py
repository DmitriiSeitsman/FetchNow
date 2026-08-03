"""Execute stage=network security fixture cases via MockTransport."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from fetchnow.core.config import Settings
from fetchnow.network.client import SafeHTTPClient
from fetchnow.url.dns import FakeDnsResolver
from fetchnow.url.errors import URLValidationError
from fetchnow.url.providers import ProviderRegistry
from fetchnow.url.validate import URLValidator

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "url_security_cases.json"


@pytest.fixture(scope="module")
def fixture_payload() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _settings(case: dict[str, Any]) -> Settings:
    network = case.get("network") or {}
    return Settings(
        APP_ENV="test",
        LOG_LEVEL="WARNING",
        URL_ALLOWED_SCHEMES="http,https",
        URL_ALLOWED_PORTS="80,443",
        URL_MAX_LENGTH=4096,
        URL_MAX_REDIRECTS=network.get("max_redirects", 5),
        DNS_RESOLUTION_TIMEOUT_SECONDS=1,
        PROVIDER_VK_ENABLED=True,
        PROVIDER_RUTUBE_ENABLED=True,
        OUTBOUND_MAX_RESPONSE_BYTES=network.get("max_bytes", 1_048_576),
        OUTBOUND_PROBE_BODY_BYTES=network.get(
            "probe_bytes", network.get("max_bytes", 1_048_576)
        ),
        OUTBOUND_ALLOWED_CONTENT_TYPES="text/html,application/json,text/plain",
        OUTBOUND_CONNECT_TIMEOUT_SECONDS=2,
        OUTBOUND_READ_TIMEOUT_SECONDS=2,
        OUTBOUND_TOTAL_TIMEOUT_SECONDS=5,
        OUTBOUND_USER_AGENT="FetchNow-Fixture/1.0",
    )


def _transport_for_case(case: dict[str, Any]) -> httpx.MockTransport:
    hops = (case.get("network") or {}).get("hops") or []
    table: dict[tuple[str, str], httpx.Response] = {}
    method = (case.get("network") or {}).get("method", "HEAD").upper()

    for hop in hops:
        url = hop["url"]
        status = hop["status"]
        headers = dict(hop.get("headers") or {})
        if "location" in hop and hop["location"] is not None:
            headers["Location"] = hop["location"]
        body = hop.get("body")
        content = body.encode() if isinstance(body, str) else body
        # Register both the case method and HEAD/GET variants used during fallback.
        table[(method, url)] = httpx.Response(
            status, headers=headers, content=content or b""
        )
        if method == "GET":
            table[("HEAD", url)] = httpx.Response(405)
        if status >= 400 and method == "HEAD":
            pass

    def handler(request: httpx.Request) -> httpx.Response:
        bare = f"{request.url.scheme}://{request.url.host}{request.url.path}"
        key = (request.method.upper(), bare)
        if key in table:
            # Rebuild response because httpx responses are single-use when streamed.
            template = table[key]
            return httpx.Response(
                template.status_code,
                headers=template.headers,
                content=template.content,
            )
        return httpx.Response(404, text="no mock")

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_network_stage_cases(fixture_payload: dict[str, Any]) -> None:
    cases = [c for c in fixture_payload["cases"] if c.get("stage") == "network"]
    assert len(cases) >= 10

    for case in cases:
        settings = _settings(case)
        resolver = FakeDnsResolver(
            default_addresses=tuple(case.get("dns_addresses") or ["8.8.8.8"])
        )
        # Seed records for all allowlisted hosts.
        records = {
            host: list(case.get("dns_addresses") or ["8.8.8.8"])
            for provider in ProviderRegistry.from_settings(settings).providers
            for host in provider.exact_hostnames
        }
        resolver = FakeDnsResolver(records=records, default_addresses=("8.8.8.8",))
        validator = URLValidator(
            settings,
            registry=ProviderRegistry.from_settings(settings),
            resolver=resolver,
        )
        client = SafeHTTPClient(
            settings,
            validator=validator,
            resolver=resolver,
            transport=_transport_for_case(case),
        )
        method = (case.get("network") or {}).get("method", "HEAD")
        try:
            if case["expected"] == "allow":
                result = await client.probe(case["url"], method=method)
                if case.get("expected_hostname"):
                    assert result.final_hostname == case["expected_hostname"]
                if case.get("expected_provider"):
                    assert result.provider_id == case["expected_provider"]
            else:
                with pytest.raises(URLValidationError) as excinfo:
                    await client.probe(case["url"], method=method)
                assert excinfo.value.code == case["reason_code"], (
                    f"{case['id']}: expected {case['reason_code']}, "
                    f"got {excinfo.value.code}"
                )
        finally:
            await client.aclose()
