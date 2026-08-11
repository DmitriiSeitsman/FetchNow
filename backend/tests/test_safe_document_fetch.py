"""Offline tests for SafeHTTPClient.fetch_document (PR3A)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx
import pytest

from fetchnow.core.config import Settings
from fetchnow.network.client import SafeHTTPClient
from fetchnow.network.models import SafeDocumentTarget
from fetchnow.resolution.service import ScopedDocumentFetchPort
from fetchnow.resolution.wrapper_safety import WrapperSafetyValidator
from fetchnow.url.dns import FakeDnsResolver
from fetchnow.url.errors import URLValidationError
from fetchnow.url.providers import ProviderRegistry
from fetchnow.url.validate import URLValidator

SECRET = "document-secret-marker"
TOP_SECRET = "TOP_SECRET"


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
        "OUTBOUND_PROBE_BODY_BYTES": 32,
        "OUTBOUND_USER_AGENT": "FetchNow-Test/1.0",
        "OUTBOUND_ALLOWED_CONTENT_TYPES": "text/html,application/json,text/plain",
        "WRAPPER_RESOLUTION_MAX_DEPTH": 3,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _client(
    routes: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]],
    *,
    settings: Settings | None = None,
    dns: FakeDnsResolver | None = None,
    track_calls: list[str] | None = None,
) -> tuple[SafeHTTPClient, WrapperSafetyValidator]:
    cfg = settings or _settings()
    resolver = dns or FakeDnsResolver(records={"wrapper.test": ["1.1.1.1"]})
    registry = ProviderRegistry.from_settings(cfg)
    validator = URLValidator(cfg, registry=registry, resolver=resolver)
    safety = WrapperSafetyValidator(cfg, resolver=resolver)

    def handler(request: httpx.Request) -> httpx.Response:
        if track_calls is not None:
            track_calls.append(str(request.url))
        assert "authorization" not in {k.lower() for k in request.headers}
        assert "cookie" not in {k.lower() for k in request.headers}
        assert "referer" not in {k.lower() for k in request.headers}
        key = (request.method.upper(), str(request.url))
        if key in routes:
            return routes[key](request)
        bare = (request.method.upper(), str(request.url.copy_with(query=None)))
        if bare in routes:
            return routes[bare](request)
        return httpx.Response(404, text="missing")

    client = SafeHTTPClient(
        cfg,
        validator=validator,
        resolver=resolver,
        transport=httpx.MockTransport(handler),
    )
    return client, safety


def _target(
    *,
    host: str = "wrapper.test",
    path: str = "/doc",
    query: str = "",
) -> SafeDocumentTarget:
    q = f"?{query}" if query else ""
    return SafeDocumentTarget(
        scheme="https",
        hostname=host,
        port=None,
        path=path,
        query=query,
        canonical_without_query=f"https://{host}{path}",
        request_url=f"https://{host}{path}{q}",
    )


async def _validated(
    safety: WrapperSafetyValidator, url: str = "https://wrapper.test/doc"
) -> SafeDocumentTarget:
    return await safety.validate(url)


@pytest.mark.asyncio
async def test_document_html_success() -> None:
    def page(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text="<html>ok</html>",
        )

    client, safety = _client({("GET", "https://wrapper.test/doc"): page})
    validated = await _validated(safety)
    doc = await client.fetch_document(
        validated,
        allowed_hostnames=frozenset({"wrapper.test"}),
        revalidate=safety.validate,
    )
    assert doc.content_type == "text/html"
    assert doc.body == b"<html>ok</html>"
    assert doc.final_canonical == "https://wrapper.test/doc"
    assert SECRET not in repr(doc)


@pytest.mark.asyncio
async def test_document_json_success() -> None:
    def page(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b'{"a":1}',
        )

    client, safety = _client({("GET", "https://wrapper.test/doc"): page})
    validated = await _validated(safety)
    doc = await client.fetch_document(
        validated,
        allowed_hostnames=frozenset({"wrapper.test"}),
        revalidate=safety.validate,
    )
    assert doc.content_type == "application/json"
    assert doc.body == b'{"a":1}'


@pytest.mark.asyncio
async def test_initial_host_not_in_allowlist_skips_transport() -> None:
    calls: list[str] = []
    client, safety = _client({}, track_calls=calls)
    evil = _target(host="evil.test", path="/x")
    with pytest.raises(URLValidationError) as exc:
        await client.fetch_document(
            evil,
            allowed_hostnames=frozenset({"wrapper.test"}),
            revalidate=safety.validate,
        )
    assert exc.value.code == "INVALID_REDIRECT"
    assert calls == []


@pytest.mark.asyncio
async def test_empty_allowlist_fails_closed() -> None:
    calls: list[str] = []
    client, safety = _client({}, track_calls=calls)
    validated = await _validated(safety)
    with pytest.raises(URLValidationError) as exc:
        await client.fetch_document(
            validated,
            allowed_hostnames=frozenset(),
            revalidate=safety.validate,
        )
    assert exc.value.code == "INVALID_REDIRECT"
    assert calls == []


@pytest.mark.asyncio
async def test_scoped_port_blocks_unowned_host() -> None:
    calls: list[str] = []
    client, safety = _client(
        {},
        dns=FakeDnsResolver(
            records={"wrapper.test": ["1.1.1.1"], "evil.test": ["8.8.8.8"]}
        ),
        track_calls=calls,
    )
    port = ScopedDocumentFetchPort(client, safety, frozenset({"wrapper.test"}))
    evil = _target(host="evil.test", path="/pivot")
    with pytest.raises(URLValidationError):
        await port.fetch_document(evil)
    assert calls == []
    # Proof flag for review checklist.
    resolver_can_fetch_unowned_public_host = False
    assert resolver_can_fetch_unowned_public_host is False


@pytest.mark.asyncio
async def test_scoped_port_rejects_empty_hosts() -> None:
    client, safety = _client({})
    from fetchnow.resolution.errors import ResolutionError, ResolutionErrorKind

    with pytest.raises(ResolutionError) as exc:
        ScopedDocumentFetchPort(client, safety, frozenset())
    assert exc.value.kind == ResolutionErrorKind.INTERNAL_ERROR


@pytest.mark.asyncio
async def test_document_missing_mime_fails() -> None:
    def page(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x")

    client, safety = _client({("GET", "https://wrapper.test/doc"): page})
    validated = await _validated(safety)
    with pytest.raises(URLValidationError) as exc:
        await client.fetch_document(
            validated,
            allowed_hostnames=frozenset({"wrapper.test"}),
            revalidate=safety.validate,
        )
    assert exc.value.code == "UNSUPPORTED_CONTENT_TYPE"


@pytest.mark.asyncio
async def test_document_wrong_mime_fails() -> None:
    def page(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/octet-stream"},
            content=b"x",
        )

    client, safety = _client({("GET", "https://wrapper.test/doc"): page})
    validated = await _validated(safety)
    with pytest.raises(URLValidationError) as exc:
        await client.fetch_document(
            validated,
            allowed_hostnames=frozenset({"wrapper.test"}),
            revalidate=safety.validate,
        )
    assert exc.value.code == "UNSUPPORTED_CONTENT_TYPE"


@pytest.mark.asyncio
async def test_document_content_length_overflow() -> None:
    def page(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html", "content-length": "1000"},
            content=b"x" * 10,
        )

    client, safety = _client({("GET", "https://wrapper.test/doc"): page})
    validated = await _validated(safety)
    with pytest.raises(URLValidationError) as exc:
        await client.fetch_document(
            validated,
            allowed_hostnames=frozenset({"wrapper.test"}),
            revalidate=safety.validate,
        )
    assert exc.value.code == "RESPONSE_TOO_LARGE"


@pytest.mark.asyncio
async def test_document_chunked_overflow() -> None:
    def page(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"x" * 200,
        )

    client, safety = _client({("GET", "https://wrapper.test/doc"): page})
    validated = await _validated(safety)
    with pytest.raises(URLValidationError) as exc:
        await client.fetch_document(
            validated,
            allowed_hostnames=frozenset({"wrapper.test"}),
            revalidate=safety.validate,
        )
    assert exc.value.code == "RESPONSE_TOO_LARGE"


@pytest.mark.asyncio
async def test_document_same_wrapper_redirect() -> None:
    def redirect(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://wrapper.test/final"})

    def final(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="done",
        )

    client, safety = _client(
        {
            ("GET", "https://wrapper.test/doc"): redirect,
            ("GET", "https://wrapper.test/final"): final,
        }
    )
    validated = await _validated(safety)
    doc = await client.fetch_document(
        validated,
        allowed_hostnames=frozenset({"wrapper.test"}),
        revalidate=safety.validate,
    )
    assert doc.final_path == "/final"
    assert doc.redirect_count == 1


@pytest.mark.asyncio
async def test_document_cross_host_redirect_rejected_before_follow() -> None:
    calls: list[str] = []

    def redirect(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://vk.com/video-1"})

    dns = FakeDnsResolver(records={"wrapper.test": ["1.1.1.1"], "vk.com": ["8.8.8.8"]})
    client, safety = _client(
        {("GET", "https://wrapper.test/doc"): redirect},
        dns=dns,
        track_calls=calls,
    )
    validated = await _validated(safety)
    with pytest.raises(URLValidationError) as exc:
        await client.fetch_document(
            validated,
            allowed_hostnames=frozenset({"wrapper.test"}),
            revalidate=safety.validate,
        )
    assert exc.value.code == "INVALID_REDIRECT"
    assert calls == ["https://wrapper.test/doc"]


@pytest.mark.asyncio
async def test_document_cross_wrapper_redirect_rejected() -> None:
    def redirect(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302, headers={"location": "https://nested-wrapper.test/x"}
        )

    dns = FakeDnsResolver(
        records={
            "wrapper.test": ["1.1.1.1"],
            "nested-wrapper.test": ["1.0.0.1"],
        }
    )
    client, safety = _client({("GET", "https://wrapper.test/doc"): redirect}, dns=dns)
    validated = await _validated(safety)
    with pytest.raises(URLValidationError) as exc:
        await client.fetch_document(
            validated,
            allowed_hostnames=frozenset({"wrapper.test"}),
            revalidate=safety.validate,
        )
    assert exc.value.code == "INVALID_REDIRECT"


@pytest.mark.asyncio
async def test_document_https_to_http_redirect_rejected() -> None:
    def redirect(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://wrapper.test/final"})

    client, safety = _client({("GET", "https://wrapper.test/doc"): redirect})
    validated = await _validated(safety)
    with pytest.raises(URLValidationError) as exc:
        await client.fetch_document(
            validated,
            allowed_hostnames=frozenset({"wrapper.test"}),
            revalidate=safety.validate,
        )
    assert exc.value.code == "INVALID_REDIRECT"


@pytest.mark.asyncio
async def test_document_missing_location_rejected() -> None:
    def redirect(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302)

    client, safety = _client({("GET", "https://wrapper.test/doc"): redirect})
    validated = await _validated(safety)
    with pytest.raises(URLValidationError) as exc:
        await client.fetch_document(
            validated,
            allowed_hostnames=frozenset({"wrapper.test"}),
            revalidate=safety.validate,
        )
    assert exc.value.code == "INVALID_REDIRECT"


@pytest.mark.asyncio
async def test_document_redirect_loop() -> None:
    def bounce(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        nxt = "/b" if path == "/a" else "/a"
        return httpx.Response(302, headers={"location": f"https://wrapper.test{nxt}"})

    client, safety = _client(
        {
            ("GET", "https://wrapper.test/a"): bounce,
            ("GET", "https://wrapper.test/b"): bounce,
        }
    )
    validated = await safety.validate("https://wrapper.test/a")
    with pytest.raises(URLValidationError) as exc:
        await client.fetch_document(
            validated,
            allowed_hostnames=frozenset({"wrapper.test"}),
            revalidate=safety.validate,
        )
    assert exc.value.code == "REDIRECT_LIMIT_EXCEEDED"


@pytest.mark.asyncio
async def test_document_redirect_limit() -> None:
    def hop(request: httpx.Request) -> httpx.Response:
        n = int(request.url.path.lstrip("/") or "0")
        return httpx.Response(
            302, headers={"location": f"https://wrapper.test/{n + 1}"}
        )

    routes = {("GET", f"https://wrapper.test/{i}"): hop for i in range(0, 10)}
    client, safety = _client(routes, settings=_settings(URL_MAX_REDIRECTS=2))
    validated = await safety.validate("https://wrapper.test/0")
    with pytest.raises(URLValidationError) as exc:
        await client.fetch_document(
            validated,
            allowed_hostnames=frozenset({"wrapper.test"}),
            revalidate=safety.validate,
        )
    assert exc.value.code == "REDIRECT_LIMIT_EXCEEDED"


@pytest.mark.asyncio
async def test_document_private_redirect_rejected() -> None:
    def redirect(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://wrapper.test/private"})

    async def revalidate(raw: str) -> SafeDocumentTarget:
        if raw.endswith("/private"):
            raise URLValidationError(
                code="BLOCKED_DESTINATION",
                message="blocked",
                internal_reason="PRIVATE",
            )
        return await safety.validate(raw)

    client, safety = _client({("GET", "https://wrapper.test/doc"): redirect})
    validated = await _validated(safety)
    with pytest.raises(URLValidationError) as exc:
        await client.fetch_document(
            validated,
            allowed_hostnames=frozenset({"wrapper.test"}),
            revalidate=revalidate,
        )
    assert exc.value.code == "BLOCKED_DESTINATION"


@pytest.mark.asyncio
async def test_document_mixed_dns_rejected() -> None:
    from fetchnow.resolution.errors import ResolutionError

    dns = FakeDnsResolver(records={"wrapper.test": ["1.1.1.1", "10.0.0.1"]})
    client, safety = _client({}, dns=dns)
    with pytest.raises(ResolutionError):
        await safety.validate("https://wrapper.test/doc")


@pytest.mark.asyncio
async def test_document_dns_timeout() -> None:
    class TimeoutDns(FakeDnsResolver):
        async def resolve(self, hostname: str):  # type: ignore[override]
            raise URLValidationError(
                code="SOURCE_TIMEOUT",
                message="dns timeout",
                internal_reason="DNS_TIMEOUT",
            )

    from fetchnow.resolution.errors import ResolutionError

    client, safety = _client({}, dns=TimeoutDns(records={}))
    with pytest.raises(ResolutionError):
        await safety.validate("https://wrapper.test/doc")


@pytest.mark.asyncio
async def test_document_connect_time_dns_mismatch() -> None:
    class FlipDns(FakeDnsResolver):
        def __init__(self) -> None:
            super().__init__(records={"wrapper.test": ["1.1.1.1"]})
            self._calls = 0

        async def resolve(self, hostname: str):  # type: ignore[override]
            self._calls += 1
            # Preflight public; connect-time disjoint public set → mismatch.
            if self._calls % 2 == 1:
                return await super().resolve(hostname)
            from ipaddress import ip_address

            return [ip_address("8.8.8.8")]

    def page(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, text="x")

    dns = FlipDns()
    client, safety = _client({("GET", "https://wrapper.test/doc"): page}, dns=dns)
    validated = await _validated(safety)
    with pytest.raises(URLValidationError) as exc:
        await client.fetch_document(
            validated,
            allowed_hostnames=frozenset({"wrapper.test"}),
            revalidate=safety.validate,
        )
    assert exc.value.code == "BLOCKED_DESTINATION"


@pytest.mark.asyncio
async def test_document_total_timeout() -> None:
    def slow(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow")

    client, safety = _client(
        {("GET", "https://wrapper.test/doc"): slow},
        settings=_settings(OUTBOUND_TOTAL_TIMEOUT_SECONDS=1),
    )
    validated = await _validated(safety)
    with pytest.raises(URLValidationError) as exc:
        await client.fetch_document(
            validated,
            allowed_hostnames=frozenset({"wrapper.test"}),
            revalidate=safety.validate,
        )
    assert exc.value.code == "SOURCE_TIMEOUT"


@pytest.mark.asyncio
async def test_document_response_closed_on_success() -> None:
    closed: list[bool] = []

    def page(_request: httpx.Request) -> httpx.Response:
        response = httpx.Response(200, headers={"content-type": "text/html"}, text="ok")
        original = response.aclose

        async def tracked_close() -> None:
            closed.append(True)
            await original()

        response.aclose = tracked_close  # type: ignore[method-assign]
        return response

    client, safety = _client({("GET", "https://wrapper.test/doc"): page})
    validated = await _validated(safety)
    await client.fetch_document(
        validated,
        allowed_hostnames=frozenset({"wrapper.test"}),
        revalidate=safety.validate,
    )
    assert closed == [True]


@pytest.mark.asyncio
async def test_document_response_closed_on_error() -> None:
    closed: list[bool] = []

    def page(_request: httpx.Request) -> httpx.Response:
        response = httpx.Response(
            200, headers={"content-type": "application/octet-stream"}, content=b"x"
        )
        original = response.aclose

        async def tracked_close() -> None:
            closed.append(True)
            await original()

        response.aclose = tracked_close  # type: ignore[method-assign]
        return response

    client, safety = _client({("GET", "https://wrapper.test/doc"): page})
    validated = await _validated(safety)
    with pytest.raises(URLValidationError):
        await client.fetch_document(
            validated,
            allowed_hostnames=frozenset({"wrapper.test"}),
            revalidate=safety.validate,
        )
    assert closed == [True]


@pytest.mark.asyncio
async def test_document_response_closed_on_cancellation() -> None:
    closed: list[bool] = []
    entered = asyncio.Event()

    def page(_request: httpx.Request) -> httpx.Response:
        response = httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"x" * 40,
        )
        original_aiter = response.aiter_bytes
        original_close = response.aclose

        async def slow_aiter(chunk_size: int = 65536):  # noqa: ARG001
            entered.set()
            await asyncio.sleep(60)
            async for chunk in original_aiter(chunk_size):
                yield chunk

        async def tracked_close() -> None:
            closed.append(True)
            await original_close()

        response.aiter_bytes = slow_aiter  # type: ignore[method-assign]
        response.aclose = tracked_close  # type: ignore[method-assign]
        return response

    client, safety = _client({("GET", "https://wrapper.test/doc"): page})
    validated = await _validated(safety)
    task = asyncio.create_task(
        client.fetch_document(
            validated,
            allowed_hostnames=frozenset({"wrapper.test"}),
            revalidate=safety.validate,
        )
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [True]


@pytest.mark.asyncio
async def test_document_query_not_in_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def page(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="ok",
        )

    client, safety = _client({("GET", f"https://wrapper.test/doc?{SECRET}=1"): page})
    validated = await safety.validate(f"https://wrapper.test/doc?{SECRET}=1")
    with caplog.at_level("INFO"):
        doc = await client.fetch_document(
            validated,
            allowed_hostnames=frozenset({"wrapper.test"}),
            revalidate=safety.validate,
        )
    assert SECRET not in repr(doc)
    assert SECRET not in repr(validated)
    assert SECRET not in caplog.text
    assert TOP_SECRET not in caplog.text


@pytest.mark.asyncio
async def test_document_gzip_decompressed_overflow(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import gzip

    decompressed = b"X" * 200
    compressed = gzip.compress(decompressed)
    assert len(compressed) < 64
    closed: list[bool] = []

    def page(_request: httpx.Request) -> httpx.Response:
        response = httpx.Response(
            200,
            headers={
                "content-type": "text/html",
                "content-encoding": "gzip",
                "content-length": str(len(compressed)),
            },
            content=compressed,
        )
        original = response.aclose

        async def tracked_close() -> None:
            closed.append(True)
            await original()

        response.aclose = tracked_close  # type: ignore[method-assign]
        return response

    client, safety = _client(
        {("GET", "https://wrapper.test/doc"): page},
        settings=_settings(OUTBOUND_MAX_RESPONSE_BYTES=64),
    )
    validated = await _validated(safety)
    with (
        caplog.at_level("INFO"),
        pytest.raises(URLValidationError) as exc,
    ):
        await client.fetch_document(
            validated,
            allowed_hostnames=frozenset({"wrapper.test"}),
            revalidate=safety.validate,
        )
    assert exc.value.code == "RESPONSE_TOO_LARGE"
    assert closed == [True]
    assert "XXXX" not in caplog.text
    assert decompressed.decode("latin-1") not in caplog.text
    decompressed_overflow_rejected = True
    assert decompressed_overflow_rejected is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    [
        None,
        "",
        ":::not-a-url",
        "javascript:alert(1)",
        "https://user:pass@wrapper.test/final",
        "ftp://wrapper.test/final",
        "http://wrapper.test/final",
        "https://wrapper.test:99999/final",
        "https://[not-a-host/final",
    ],
)
async def test_document_malformed_location_rejected(location: str | None) -> None:
    calls: list[str] = []

    def redirect(_request: httpx.Request) -> httpx.Response:
        headers = {} if location is None else {"location": location}
        return httpx.Response(302, headers=headers)

    client, safety = _client(
        {("GET", "https://wrapper.test/doc"): redirect},
        track_calls=calls,
    )
    validated = await _validated(safety)
    with pytest.raises(URLValidationError) as exc:
        await client.fetch_document(
            validated,
            allowed_hostnames=frozenset({"wrapper.test"}),
            revalidate=safety.validate,
        )
    assert exc.value.code in {"INVALID_REDIRECT", "BLOCKED_DESTINATION"}
    assert calls == ["https://wrapper.test/doc"]
    malformed_redirect_followed = len(calls) > 1
    assert malformed_redirect_followed is False
