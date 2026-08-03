"""Unit tests for SafeHTTPClient redirect and response policies."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from ipaddress import ip_address

import httpx
import pytest

from fetchnow.core.config import Settings
from fetchnow.network.client import SafeHTTPClient, normalize_content_type
from fetchnow.url.dns import FakeDnsResolver
from fetchnow.url.errors import URLValidationError
from fetchnow.url.providers import ProviderDescriptor, ProviderRegistry
from fetchnow.url.validate import URLValidator


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "APP_ENV": "test",
        "LOG_LEVEL": "INFO",
        "URL_ALLOWED_SCHEMES": "http,https",
        "URL_ALLOWED_PORTS": "80,443",
        "URL_MAX_LENGTH": 4096,
        "URL_MAX_REDIRECTS": 3,
        "DNS_RESOLUTION_TIMEOUT_SECONDS": 1,
        "PROVIDER_VK_ENABLED": True,
        "PROVIDER_RUTUBE_ENABLED": True,
        "OUTBOUND_CONNECT_TIMEOUT_SECONDS": 1,
        "OUTBOUND_READ_TIMEOUT_SECONDS": 1,
        "OUTBOUND_TOTAL_TIMEOUT_SECONDS": 5,
        "OUTBOUND_MAX_RESPONSE_BYTES": 64,
        "OUTBOUND_PROBE_BODY_BYTES": 64,
        "OUTBOUND_USER_AGENT": "FetchNow-Test/1.0",
        "OUTBOUND_ALLOWED_CONTENT_TYPES": "text/html,application/json,text/plain",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@dataclass
class FlappingDnsResolver:
    """Returns different address sets on successive resolve calls."""

    sequences: dict[str, list[list[str]]] = field(default_factory=dict)
    calls: dict[str, int] = field(default_factory=dict)

    async def resolve(self, hostname: str) -> list:
        host = hostname.lower().rstrip(".")
        seq = self.sequences.get(host) or [["8.8.8.8"]]
        idx = self.calls.get(host, 0)
        self.calls[host] = idx + 1
        values = seq[min(idx, len(seq) - 1)]
        return [ip_address(v) for v in values]


def _handler_map(
    routes: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]],
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method.upper(), str(request.url))
        # Match without forcing trailing slash differences for simple paths.
        if key in routes:
            return routes[key](request)
        # Try without query for lookup of redirect targets configured without query.
        bare = (request.method.upper(), str(request.url.copy_with(query=None)))
        if bare in routes:
            return routes[bare](request)
        return httpx.Response(404, text="missing mock route")

    return handler


def _client(
    routes: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]],
    *,
    settings: Settings | None = None,
    resolver: FakeDnsResolver | FlappingDnsResolver | None = None,
    registry: ProviderRegistry | None = None,
) -> SafeHTTPClient:
    settings = settings or _settings()
    resolver = resolver or FakeDnsResolver(default_addresses=("8.8.8.8",))
    registry = registry or ProviderRegistry.from_settings(settings)
    validator = URLValidator(
        settings,
        registry=registry,
        resolver=resolver,  # type: ignore[arg-type]
    )
    transport = httpx.MockTransport(_handler_map(routes))
    return SafeHTTPClient(
        settings,
        validator=validator,
        resolver=resolver,  # type: ignore[arg-type]
        transport=transport,
    )


@pytest.mark.asyncio
async def test_no_automatic_redirects_follow() -> None:
    calls = {"n": 0}

    def redirect(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            302, headers={"Location": "https://vk.com/final"}, text=""
        )

    def final(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, headers={"content-type": "text/html"}, text="ok")

    client = _client(
        {
            ("HEAD", "https://vk.com/start"): redirect,
            ("HEAD", "https://vk.com/final"): final,
        }
    )
    result = await client.probe("https://vk.com/start", method="HEAD")
    assert result.redirect_count == 1
    assert result.final_path == "/final"
    assert calls["n"] == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_successful_head() -> None:
    client = _client(
        {
            ("HEAD", "https://vk.com/video-1"): lambda r: httpx.Response(
                200,
                headers={
                    "content-type": "text/html; charset=utf-8",
                    "content-length": "12",
                },
            )
        }
    )
    result = await client.probe("https://vk.com/video-1", method="HEAD")
    assert result.status_code == 200
    assert result.content_type == "text/html"
    assert result.content_length == 12
    assert result.body_bytes_read == 0
    await client.aclose()


@pytest.mark.asyncio
async def test_limited_get_body() -> None:
    client = _client(
        {
            ("GET", "https://vk.com/video-1"): lambda r: httpx.Response(
                200,
                headers={"content-type": "text/plain"},
                content=b"hello",
            )
        }
    )
    result = await client.probe("https://vk.com/video-1", method="GET")
    assert result.body_bytes_read == 5
    await client.aclose()


@pytest.mark.asyncio
async def test_relative_redirect() -> None:
    client = _client(
        {
            ("HEAD", "https://vk.com/a"): lambda r: httpx.Response(
                302, headers={"Location": "/b"}
            ),
            ("HEAD", "https://vk.com/b"): lambda r: httpx.Response(
                200, headers={"content-type": "text/html"}
            ),
        }
    )
    result = await client.probe("https://vk.com/a", method="HEAD")
    assert result.final_path == "/b"
    assert result.redirect_count == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_same_provider_subdomain_redirect() -> None:
    client = _client(
        {
            ("HEAD", "https://vk.com/a"): lambda r: httpx.Response(
                302, headers={"Location": "https://m.vk.com/a"}
            ),
            ("HEAD", "https://m.vk.com/a"): lambda r: httpx.Response(
                200, headers={"content-type": "text/html"}
            ),
        }
    )
    result = await client.probe("https://vk.com/a", method="HEAD")
    assert result.final_hostname == "m.vk.com"
    await client.aclose()


@pytest.mark.asyncio
async def test_cross_provider_redirect_blocked() -> None:
    client = _client(
        {
            ("HEAD", "https://vk.com/a"): lambda r: httpx.Response(
                302, headers={"Location": "https://rutube.ru/video/1"}
            ),
        }
    )
    with pytest.raises(URLValidationError) as exc:
        await client.probe("https://vk.com/a", method="HEAD")
    assert exc.value.code == "UNSUPPORTED_PROVIDER"
    await client.aclose()


@pytest.mark.asyncio
async def test_redirect_to_private_address() -> None:
    client = _client(
        {
            ("HEAD", "https://vk.com/a"): lambda r: httpx.Response(
                302, headers={"Location": "https://127.0.0.1/secret"}
            ),
        }
    )
    with pytest.raises(URLValidationError) as exc:
        await client.probe("https://vk.com/a", method="HEAD")
    assert exc.value.code == "BLOCKED_DESTINATION"
    await client.aclose()


@pytest.mark.asyncio
async def test_redirect_loop() -> None:
    client = _client(
        {
            ("HEAD", "https://vk.com/a"): lambda r: httpx.Response(
                302, headers={"Location": "https://vk.com/b"}
            ),
            ("HEAD", "https://vk.com/b"): lambda r: httpx.Response(
                302, headers={"Location": "https://vk.com/a"}
            ),
        }
    )
    with pytest.raises(URLValidationError) as exc:
        await client.probe("https://vk.com/a", method="HEAD")
    assert exc.value.code == "REDIRECT_LIMIT_EXCEEDED"
    await client.aclose()


@pytest.mark.asyncio
async def test_max_redirects() -> None:
    settings = _settings(URL_MAX_REDIRECTS=1)
    client = _client(
        {
            ("HEAD", "https://vk.com/a"): lambda r: httpx.Response(
                302, headers={"Location": "https://vk.com/b"}
            ),
            ("HEAD", "https://vk.com/b"): lambda r: httpx.Response(
                302, headers={"Location": "https://vk.com/c"}
            ),
            ("HEAD", "https://vk.com/c"): lambda r: httpx.Response(
                200, headers={"content-type": "text/html"}
            ),
        },
        settings=settings,
    )
    with pytest.raises(URLValidationError) as exc:
        await client.probe("https://vk.com/a", method="HEAD")
    assert exc.value.code == "REDIRECT_LIMIT_EXCEEDED"
    await client.aclose()


@pytest.mark.asyncio
async def test_https_downgrade_blocked() -> None:
    client = _client(
        {
            ("HEAD", "https://vk.com/a"): lambda r: httpx.Response(
                302, headers={"Location": "http://vk.com/b"}
            ),
        }
    )
    with pytest.raises(URLValidationError) as exc:
        await client.probe("https://vk.com/a", method="HEAD")
    assert exc.value.code == "INVALID_REDIRECT"
    await client.aclose()


@pytest.mark.asyncio
async def test_missing_location() -> None:
    client = _client(
        {
            ("HEAD", "https://vk.com/a"): lambda r: httpx.Response(302),
        }
    )
    with pytest.raises(URLValidationError) as exc:
        await client.probe("https://vk.com/a", method="HEAD")
    assert exc.value.code == "INVALID_REDIRECT"
    await client.aclose()


@pytest.mark.asyncio
async def test_body_size_limit() -> None:
    client = _client(
        {
            ("GET", "https://vk.com/a"): lambda r: httpx.Response(
                200,
                headers={"content-type": "text/plain"},
                content=b"x" * 200,
            )
        }
    )
    with pytest.raises(URLValidationError) as exc:
        await client.probe("https://vk.com/a", method="GET")
    assert exc.value.code == "RESPONSE_TOO_LARGE"
    await client.aclose()


@pytest.mark.asyncio
async def test_unsupported_mime() -> None:
    client = _client(
        {
            ("GET", "https://vk.com/a"): lambda r: httpx.Response(
                200,
                headers={"content-type": "video/mp4"},
                content=b"data",
            )
        }
    )
    with pytest.raises(URLValidationError) as exc:
        await client.probe("https://vk.com/a", method="GET")
    assert exc.value.code == "UNSUPPORTED_CONTENT_TYPE"
    await client.aclose()


@pytest.mark.asyncio
async def test_missing_mime_on_get_denied() -> None:
    client = _client(
        {("GET", "https://vk.com/a"): lambda r: httpx.Response(200, content=b"data")}
    )
    with pytest.raises(URLValidationError) as exc:
        await client.probe("https://vk.com/a", method="GET")
    assert exc.value.code == "UNSUPPORTED_CONTENT_TYPE"
    await client.aclose()


@pytest.mark.asyncio
async def test_dns_change_between_stages() -> None:
    resolver = FlappingDnsResolver(
        sequences={"vk.com": [["8.8.8.8"], ["1.1.1.1"], ["8.8.8.8"], ["1.1.1.1"]]}
    )
    client = _client(
        {
            ("HEAD", "https://vk.com/a"): lambda r: httpx.Response(
                200, headers={"content-type": "text/html"}
            )
        },
        resolver=resolver,
    )
    with pytest.raises(URLValidationError) as exc:
        await client.probe("https://vk.com/a", method="HEAD")
    assert exc.value.code == "BLOCKED_DESTINATION"
    assert exc.value.internal_reason == "DNS_SET_MISMATCH"
    await client.aclose()


@pytest.mark.asyncio
async def test_dns_public_intersection_allowed() -> None:
    # validate + preflight + connect-time: overlapping public sets are OK.
    resolver = FlappingDnsResolver(
        sequences={
            "vk.com": [
                ["8.8.8.8", "1.1.1.1"],
                ["1.1.1.1", "9.9.9.9"],
                ["1.1.1.1", "8.8.4.4"],
            ]
        }
    )
    client = _client(
        {
            ("HEAD", "https://vk.com/a"): lambda r: httpx.Response(
                200, headers={"content-type": "text/html"}
            )
        },
        resolver=resolver,
    )
    result = await client.probe("https://vk.com/a", method="HEAD")
    assert result.status_code == 200
    assert result.method_used == "HEAD"
    await client.aclose()


@pytest.mark.asyncio
async def test_dns_mixed_public_private_still_blocked() -> None:
    resolver = FakeDnsResolver(default_addresses=("8.8.8.8", "10.0.0.1"))
    client = _client(
        {
            ("HEAD", "https://vk.com/a"): lambda r: httpx.Response(
                200, headers={"content-type": "text/html"}
            )
        },
        resolver=resolver,
    )
    with pytest.raises(URLValidationError) as exc:
        await client.probe("https://vk.com/a", method="HEAD")
    assert exc.value.code == "BLOCKED_DESTINATION"
    await client.aclose()


@pytest.mark.asyncio
async def test_head_200_no_fallback() -> None:
    calls: list[str] = []

    def head_ok(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, headers={"content-type": "text/html"})

    client = _client({("HEAD", "https://vk.com/a"): head_ok})
    result = await client.probe("https://vk.com/a", method="HEAD")
    assert result.method_used == "HEAD"
    assert calls == ["HEAD"]
    await client.aclose()


@pytest.mark.asyncio
async def test_head_405_fallback_get() -> None:
    client = _client(
        {
            ("HEAD", "https://vk.com/a"): lambda r: httpx.Response(405),
            ("GET", "https://vk.com/a"): lambda r: httpx.Response(
                200, headers={"content-type": "text/html"}, content=b"<html>ok</html>"
            ),
        }
    )
    result = await client.probe("https://vk.com/a", method="HEAD")
    assert result.method_used == "GET"
    assert result.status_code == 200
    assert result.body_bytes_read > 0
    await client.aclose()


@pytest.mark.asyncio
async def test_head_501_fallback_get() -> None:
    client = _client(
        {
            ("HEAD", "https://vk.com/a"): lambda r: httpx.Response(501),
            ("GET", "https://vk.com/a"): lambda r: httpx.Response(
                200, headers={"content-type": "text/html"}, content=b"x"
            ),
        }
    )
    result = await client.probe("https://vk.com/a", method="HEAD")
    assert result.method_used == "GET"
    await client.aclose()


@pytest.mark.asyncio
async def test_vk_head_418_fallback_get() -> None:
    client = _client(
        {
            ("HEAD", "https://vk.com/a"): lambda r: httpx.Response(418),
            ("GET", "https://vk.com/a"): lambda r: httpx.Response(
                200, headers={"content-type": "text/html"}, content=b"<html>"
            ),
        }
    )
    result = await client.probe("https://vk.com/a", method="HEAD")
    assert result.method_used == "GET"
    assert result.status_code == 200
    await client.aclose()


@pytest.mark.asyncio
async def test_unsupported_418_without_provider_policy() -> None:
    registry = ProviderRegistry.from_descriptors(
        (
            ProviderDescriptor(
                id="vk",
                display_name="VK",
                exact_hostnames=frozenset({"vk.com", "www.vk.com", "m.vk.com"}),
                enabled=True,
                head_fallback_statuses=frozenset({405, 501}),  # no 418
            ),
        )
    )
    client = _client(
        {("HEAD", "https://vk.com/a"): lambda r: httpx.Response(418)},
        registry=registry,
    )
    with pytest.raises(URLValidationError) as exc:
        await client.probe("https://vk.com/a", method="HEAD")
    assert exc.value.code == "SOURCE_UNAVAILABLE"
    await client.aclose()


@pytest.mark.asyncio
async def test_head_403_no_fallback() -> None:
    calls: list[str] = []

    def track(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(403)

    client = _client({("HEAD", "https://vk.com/a"): track})
    with pytest.raises(URLValidationError) as exc:
        await client.probe("https://vk.com/a", method="HEAD")
    assert exc.value.code == "SOURCE_UNAVAILABLE"
    assert calls == ["HEAD"]
    await client.aclose()


@pytest.mark.asyncio
async def test_get_fallback_respects_probe_body_limit() -> None:
    client = _client(
        {
            ("HEAD", "https://vk.com/a"): lambda r: httpx.Response(418),
            ("GET", "https://vk.com/a"): lambda r: httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=b"x" * 200,
            ),
        },
        settings=_settings(
            OUTBOUND_MAX_RESPONSE_BYTES=1000, OUTBOUND_PROBE_BODY_BYTES=32
        ),
    )
    result = await client.probe("https://vk.com/a", method="HEAD")
    assert result.method_used == "GET"
    # Chunked reads with bounded chunk_size should stop near the soft probe cap.
    assert result.body_bytes_read <= 32 + 16_384
    assert result.body_bytes_read >= 32
    await client.aclose()


@pytest.mark.asyncio
async def test_get_fallback_closes_response_stream() -> None:
    closes = {"n": 0}
    real_send = None

    client = _client(
        {
            ("HEAD", "https://vk.com/a"): lambda r: httpx.Response(418),
            ("GET", "https://vk.com/a"): lambda r: httpx.Response(
                200, headers={"content-type": "text/html"}, content=b"hello"
            ),
        }
    )
    real_send = client._client.send

    async def wrapped_send(*args: object, **kwargs: object) -> httpx.Response:
        response = await real_send(*args, **kwargs)  # type: ignore[misc]
        original = response.aclose

        async def tracked() -> None:
            closes["n"] += 1
            await original()

        response.aclose = tracked  # type: ignore[method-assign]
        return response

    client._client.send = wrapped_send  # type: ignore[method-assign]
    result = await client.probe("https://vk.com/a", method="HEAD")
    assert result.method_used == "GET"
    # HEAD + GET responses both closed in finally.
    assert closes["n"] >= 2
    await client.aclose()


@pytest.mark.asyncio
async def test_fallback_relative_redirect() -> None:
    client = _client(
        {
            ("HEAD", "https://vk.com/start"): lambda r: httpx.Response(418),
            ("GET", "https://vk.com/start"): lambda r: httpx.Response(
                302, headers={"Location": "/done"}
            ),
            ("GET", "https://vk.com/done"): lambda r: httpx.Response(
                200, headers={"content-type": "text/html"}, content=b"ok"
            ),
        }
    )
    result = await client.probe("https://vk.com/start", method="HEAD")
    assert result.method_used == "GET"
    assert result.final_path == "/done"
    assert result.redirect_count == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_fallback_redirect_private_blocked() -> None:
    client = _client(
        {
            ("HEAD", "https://vk.com/start"): lambda r: httpx.Response(418),
            ("GET", "https://vk.com/start"): lambda r: httpx.Response(
                302, headers={"Location": "https://127.0.0.1/x"}
            ),
        }
    )
    with pytest.raises(URLValidationError) as exc:
        await client.probe("https://vk.com/start", method="HEAD")
    assert exc.value.code == "BLOCKED_DESTINATION"
    await client.aclose()


@pytest.mark.asyncio
async def test_fallback_https_downgrade_blocked() -> None:
    client = _client(
        {
            ("HEAD", "https://vk.com/start"): lambda r: httpx.Response(418),
            ("GET", "https://vk.com/start"): lambda r: httpx.Response(
                302, headers={"Location": "http://vk.com/done"}
            ),
        }
    )
    with pytest.raises(URLValidationError) as exc:
        await client.probe("https://vk.com/start", method="HEAD")
    assert exc.value.code == "INVALID_REDIRECT"
    await client.aclose()


@pytest.mark.asyncio
async def test_fallback_mime_checked_on_get_response() -> None:
    client = _client(
        {
            ("HEAD", "https://vk.com/a"): lambda r: httpx.Response(418),
            ("GET", "https://vk.com/a"): lambda r: httpx.Response(
                200, headers={"content-type": "video/mp4"}, content=b"data"
            ),
        }
    )
    with pytest.raises(URLValidationError) as exc:
        await client.probe("https://vk.com/a", method="HEAD")
    assert exc.value.code == "UNSUPPORTED_CONTENT_TYPE"
    await client.aclose()


@pytest.mark.asyncio
async def test_fallback_secret_query_not_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "fallback-secret-token-xyz"
    client = _client(
        {
            ("HEAD", f"https://vk.com/a?token={secret}"): lambda r: httpx.Response(418),
            ("GET", f"https://vk.com/a?token={secret}"): lambda r: httpx.Response(
                200, headers={"content-type": "text/html"}, content=b"ok"
            ),
        }
    )
    with caplog.at_level(logging.INFO):
        result = await client.probe(f"https://vk.com/a?token={secret}", method="HEAD")
    assert result.method_used == "GET"
    blob = "\n".join(
        f"{r.getMessage()} {r.__dict__!s}"
        for r in caplog.records
        if r.name.startswith("fetchnow.")
    )
    assert secret not in blob
    await client.aclose()


@pytest.mark.asyncio
async def test_redirect_secret_query_not_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "redirect-secret-token-abc"
    client = _client(
        {
            ("HEAD", f"https://vk.com/a?token={secret}"): lambda r: httpx.Response(
                302, headers={"Location": f"https://vk.com/b?token={secret}"}
            ),
            ("HEAD", f"https://vk.com/b?token={secret}"): lambda r: httpx.Response(
                200, headers={"content-type": "text/html"}
            ),
        }
    )
    with caplog.at_level(logging.INFO):
        result = await client.probe(f"https://vk.com/a?token={secret}", method="HEAD")
    assert result.final_path == "/b"
    # Only FetchNow loggers are in scope; httpx is silenced to WARNING.
    fn_records = [r for r in caplog.records if r.name.startswith("fetchnow.")]
    blob = "\n".join(f"{rec.getMessage()} {rec.__dict__!s}" for rec in fn_records)
    assert secret not in blob
    assert all(secret not in r.getMessage() for r in caplog.records)
    await client.aclose()


@pytest.mark.asyncio
async def test_timeout_mapping() -> None:
    client = _client(
        {
            ("HEAD", "https://vk.com/a"): lambda r: httpx.Response(200),
        }
    )

    async def boom(*_a: object, **_k: object) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    client._client.send = boom  # type: ignore[method-assign]
    with pytest.raises(URLValidationError) as exc:
        await client.probe("https://vk.com/a", method="HEAD")
    assert exc.value.code == "SOURCE_TIMEOUT"
    assert "slow" not in exc.value.message
    await client.aclose()


@pytest.mark.asyncio
async def test_tls_and_connection_error_mapping() -> None:
    client = _client(
        {
            ("HEAD", "https://vk.com/a"): lambda r: httpx.Response(200),
        }
    )

    async def tls_boom(*_a: object, **_k: object) -> httpx.Response:
        raise httpx.ConnectError("CERTIFICATE_VERIFY_FAILED peer=10.0.0.1")

    client._client.send = tls_boom  # type: ignore[method-assign]
    with pytest.raises(URLValidationError) as exc:
        await client.probe("https://vk.com/a", method="HEAD")
    assert exc.value.code == "SOURCE_UNAVAILABLE"
    assert "10.0.0.1" not in exc.value.message
    assert "CERTIFICATE" not in exc.value.message
    await client.aclose()


@pytest.mark.asyncio
async def test_malformed_location_protocol_error() -> None:
    client = _client(
        {
            ("HEAD", "https://vk.com/a"): lambda r: httpx.Response(
                302, headers={"Location": ":::not-a-url"}
            ),
        }
    )
    with pytest.raises(URLValidationError) as exc:
        await client.probe("https://vk.com/a", method="HEAD")
    assert exc.value.code == "INVALID_REDIRECT"
    await client.aclose()


def test_normalize_content_type() -> None:
    assert normalize_content_type("text/html; charset=utf-8") == "text/html"
    assert normalize_content_type("Application/JSON") == "application/json"
    assert normalize_content_type(None) is None
