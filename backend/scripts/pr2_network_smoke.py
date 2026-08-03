#!/usr/bin/env python3
"""Deterministic PR2 network smoke (MockTransport; no public internet)."""

from __future__ import annotations

import asyncio
import logging
import sys

import httpx

from fetchnow.core.config import Settings
from fetchnow.core.logging import configure_logging
from fetchnow.network.client import SafeHTTPClient
from fetchnow.url.dns import FakeDnsResolver
from fetchnow.url.errors import URLValidationError
from fetchnow.url.providers import ProviderRegistry
from fetchnow.url.validate import URLValidator


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "APP_ENV": "test",
        "LOG_LEVEL": "INFO",
        "URL_ALLOWED_SCHEMES": "http,https",
        "URL_ALLOWED_PORTS": "80,443",
        "URL_MAX_REDIRECTS": 5,
        "PROVIDER_VK_ENABLED": True,
        "PROVIDER_RUTUBE_ENABLED": True,
        "OUTBOUND_MAX_RESPONSE_BYTES": 1024,
        "OUTBOUND_PROBE_BODY_BYTES": 1024,
        "OUTBOUND_ALLOWED_CONTENT_TYPES": "text/html,application/json,text/plain",
        "OUTBOUND_CONNECT_TIMEOUT_SECONDS": 2,
        "OUTBOUND_READ_TIMEOUT_SECONDS": 2,
        "OUTBOUND_TOTAL_TIMEOUT_SECONDS": 5,
        "OUTBOUND_USER_AGENT": "FetchNow-Smoke/1.0",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


ROUTES: dict[tuple[str, str], httpx.Response] = {
    ("HEAD", "https://vk.com/video-123_456"): httpx.Response(
        200, headers={"content-type": "text/html"}
    ),
    ("HEAD", "https://rutube.ru/video/abcdef0123456789/"): httpx.Response(
        200, headers={"content-type": "text/html"}
    ),
    ("HEAD", "https://vk.com/redir"): httpx.Response(
        302, headers={"Location": "https://vk.com/final"}
    ),
    ("HEAD", "https://vk.com/final"): httpx.Response(
        200, headers={"content-type": "text/html"}
    ),
    ("HEAD", "https://vk.com/to-local"): httpx.Response(
        302, headers={"Location": "https://127.0.0.1/x"}
    ),
    ("HEAD", "https://vk.com/loop-a"): httpx.Response(
        302, headers={"Location": "https://vk.com/loop-b"}
    ),
    ("HEAD", "https://vk.com/loop-b"): httpx.Response(
        302, headers={"Location": "https://vk.com/loop-a"}
    ),
    ("HEAD", "https://vk.com/big"): httpx.Response(405),
    ("GET", "https://vk.com/big"): httpx.Response(
        200, headers={"content-type": "text/plain"}, content=b"x" * 5000
    ),
    ("HEAD", "https://vk.com/video-mp4"): httpx.Response(405),
    ("GET", "https://vk.com/video-mp4"): httpx.Response(
        200, headers={"content-type": "video/mp4"}, content=b"data"
    ),
}


def _handler(request: httpx.Request) -> httpx.Response:
    bare = f"{request.url.scheme}://{request.url.host}{request.url.path}"
    key = (request.method.upper(), bare)
    template = ROUTES.get(key)
    if template is None:
        return httpx.Response(404)
    return httpx.Response(
        template.status_code, headers=template.headers, content=template.content
    )


async def _expect_allow(client: SafeHTTPClient, url: str, label: str) -> None:
    result = await client.probe(url, method="HEAD")
    print(f"PASS {label}: status={result.status_code} host={result.final_hostname}")


async def _expect_deny(
    client: SafeHTTPClient, url: str, code: str, label: str
) -> None:
    try:
        await client.probe(url, method="HEAD")
    except URLValidationError as exc:
        if exc.code != code:
            raise SystemExit(f"FAIL {label}: expected {code}, got {exc.code}") from exc
        print(f"PASS {label}: {exc.code}")
        return
    raise SystemExit(f"FAIL {label}: expected deny {code}")


async def main() -> int:
    configure_logging("INFO")
    settings = _settings()
    resolver = FakeDnsResolver(default_addresses=("8.8.8.8",))
    validator = URLValidator(
        settings,
        registry=ProviderRegistry.from_settings(settings),
        resolver=resolver,
    )
    client = SafeHTTPClient(
        settings,
        validator=validator,
        resolver=resolver,
        transport=httpx.MockTransport(_handler),
    )
    secret = "smoke-secret-token-xyz"
    try:
        await _expect_allow(client, "https://vk.com/video-123_456", "vk")
        await _expect_allow(
            client, "https://rutube.ru/video/abcdef0123456789/", "rutube"
        )
        await _expect_allow(client, "https://vk.com/redir", "allowed-redirect")
        await _expect_deny(
            client, "https://vk.com/to-local", "BLOCKED_DESTINATION", "localhost-redirect"
        )
        await _expect_deny(
            client, "https://vk.com/loop-a", "REDIRECT_LIMIT_EXCEEDED", "redirect-loop"
        )
        await _expect_deny(
            client, "https://vk.com/big", "RESPONSE_TOO_LARGE", "oversized"
        )
        await _expect_deny(
            client,
            "https://vk.com/video-mp4",
            "UNSUPPORTED_CONTENT_TYPE",
            "unsupported-mime",
        )
        # Secret query must not appear in FetchNow log messages.
        cap: list[str] = []
        handler = logging.Handler()
        handler.emit = lambda record: cap.append(record.getMessage())  # type: ignore[method-assign]
        logging.getLogger("fetchnow").addHandler(handler)
        await client.probe(
            f"https://vk.com/video-123_456?access_token={secret}", method="HEAD"
        )
        logging.getLogger("fetchnow").removeHandler(handler)
        blob = "\n".join(cap)
        if secret in blob:
            print("FAIL secret-query-redaction: token found in logs")
            return 1
        print("PASS secret-query-redaction")
    finally:
        await client.aclose()
    print("All deterministic PR2 network smoke checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
