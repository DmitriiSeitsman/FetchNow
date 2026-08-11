#!/usr/bin/env python3
"""Manual-only live smoke for Yandex Video Preview resolution.

NOT part of CI / ``make check``. Requires explicit opt-in and network access.

Usage (from repo root, with backend venv):

  FETCHNOW_LIVE_YANDEX_SMOKE=1 \\
    backend/.venv/bin/python backend/scripts/yandex_preview_live_smoke.py \\
    'https://yandex.ru/video/preview/<id>'

Prints only provider id, query-stripped canonical URL, and provenance.
On failure prints only safe domain kind / optional uppercase internal reason.
Exits nonzero if the result looks like ``/video_ext.php``, retains a query,
uses an unexpected provider, or resolution fails.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from urllib.parse import urlsplit

from fetchnow.core.config import Settings
from fetchnow.network.client import SafeHTTPClient
from fetchnow.resolution.defaults import build_wrapper_registry
from fetchnow.resolution.errors import ResolutionError
from fetchnow.resolution.service import ResolutionService
from fetchnow.resolution.yandex_preview import _is_stable_provider_path
from fetchnow.url.dns import SystemDnsResolver
from fetchnow.url.providers import ProviderRegistry
from fetchnow.url.validate import URLValidator


def _settings() -> Settings:
    return Settings(
        APP_ENV="smoke",
        LOG_LEVEL="WARNING",
        URL_ALLOWED_SCHEMES="https",
        URL_ALLOWED_PORTS="443",
        URL_MAX_LENGTH=4096,
        URL_MAX_REDIRECTS=3,
        DNS_RESOLUTION_TIMEOUT_SECONDS=5,
        PROVIDER_VK_ENABLED=True,
        PROVIDER_RUTUBE_ENABLED=True,
        OUTBOUND_CONNECT_TIMEOUT_SECONDS=5,
        OUTBOUND_READ_TIMEOUT_SECONDS=10,
        OUTBOUND_TOTAL_TIMEOUT_SECONDS=20,
        OUTBOUND_MAX_RESPONSE_BYTES=524288,
        OUTBOUND_PROBE_BODY_BYTES=1024,
        OUTBOUND_USER_AGENT="FetchNow-YandexSmoke/1.0",
        OUTBOUND_ALLOWED_CONTENT_TYPES="text/html,application/json,text/plain",
        WRAPPER_RESOLUTION_MAX_DEPTH=3,
    )


async def _run(url: str) -> int:
    cfg = _settings()
    dns = SystemDnsResolver(timeout_seconds=cfg.dns_resolution_timeout_seconds)
    providers = ProviderRegistry.from_settings(cfg)
    validator = URLValidator(cfg, registry=providers, resolver=dns)
    client = SafeHTTPClient(cfg, validator=validator, resolver=dns)
    wrappers = build_wrapper_registry(cfg, providers)
    service = ResolutionService(
        cfg,
        provider_registry=providers,
        wrapper_registry=wrappers,
        validator=validator,
        http_client=client,
        dns_resolver=dns,
    )
    try:
        try:
            result = await service.resolve(url)
        except ResolutionError as exc:
            print(f"FAIL: kind={exc.kind.value}", file=sys.stderr)
            if exc.internal_reason:
                print(f"FAIL: internal_reason={exc.internal_reason}", file=sys.stderr)
            return 1
    finally:
        await client.aclose()

    canonical = result.canonical_provider_url
    parts = urlsplit(canonical)
    print(f"provider={result.provider_id}")
    print(f"canonical={canonical}")
    print(f"provenance={result.provenance.value}")

    if result.provenance.value != "wrapper_resolved":
        print("FAIL: unexpected provenance", file=sys.stderr)
        return 2
    if "/video_ext" in parts.path.lower():
        print("FAIL: embed endpoint selected", file=sys.stderr)
        return 3
    if parts.query:
        print("FAIL: query-dependent canonical", file=sys.stderr)
        return 4
    if result.provider_id not in {"vk", "rutube"}:
        print("FAIL: unexpected provider", file=sys.stderr)
        return 5
    host = (parts.hostname or "").lower()
    if not _is_stable_provider_path(host, parts.path or "/"):
        print("FAIL: unstable provider identity", file=sys.stderr)
        return 6
    return 0


def main() -> int:
    if os.environ.get("FETCHNOW_LIVE_YANDEX_SMOKE") != "1":
        print(
            "Refusing to run: set FETCHNOW_LIVE_YANDEX_SMOKE=1 to opt in.",
            file=sys.stderr,
        )
        return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Exact Yandex preview HTTPS URL")
    args = parser.parse_args()
    return asyncio.run(_run(args.url))


if __name__ == "__main__":
    raise SystemExit(main())
