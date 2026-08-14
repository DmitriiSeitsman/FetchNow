#!/usr/bin/env python3
"""Manual-only live smoke for VK /clip on vk.ru (disabled by default).

NOT part of CI / ``make check``. Requires explicit opt-in and network access.
Does not download media bytes.

Usage (from repo root, with backend venv and inspection enabled):

  FETCHNOW_LIVE_VK_CLIP_SMOKE=1 \\
    MEDIA_INSPECTION_ENABLED=true \\
    MEDIA_INSPECTION_YTDLP_PATH=/absolute/path/to/yt-dlp \\
    backend/.venv/bin/python backend/scripts/vk_clip_live_smoke.py \\
    'https://vk.ru/clip-<owner>_<id>'

Prints only sanitized provider / extractor / media identity fields.
Never prints query strings, titles, direct media URLs, raw JSON, or tool stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from fetchnow.core.config import Settings
from fetchnow.media_inspection.defaults import build_media_inspection_service
from fetchnow.media_inspection.errors import InspectionError
from fetchnow.media_inspection.identity import (
    build_canonical_provider_url,
    parse_provider_media_identity,
)
from fetchnow.network.client import SafeHTTPClient
from fetchnow.resolution.defaults import build_wrapper_registry
from fetchnow.resolution.errors import ResolutionError
from fetchnow.resolution.service import ResolutionService
from fetchnow.url.dns import SystemDnsResolver
from fetchnow.url.providers import ProviderRegistry
from fetchnow.url.validate import URLValidator


def _die(message: str, code: int = 2) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


async def _run(url: str) -> int:
    cfg = Settings()
    if not cfg.media_inspection_enabled:
        _die(
            "MEDIA_INSPECTION_ENABLED must be true for live smoke "
            "(set MEDIA_INSPECTION_YTDLP_PATH to a trusted absolute binary)."
        )

    dns = SystemDnsResolver(timeout_seconds=cfg.dns_resolution_timeout_seconds)
    providers = ProviderRegistry.from_settings(cfg)
    validator = URLValidator(cfg, registry=providers, resolver=dns)
    client = SafeHTTPClient(cfg, validator=validator, resolver=dns)
    wrappers = build_wrapper_registry(cfg, providers)
    resolution = ResolutionService(
        cfg,
        provider_registry=providers,
        wrapper_registry=wrappers,
        validator=validator,
        http_client=client,
        dns_resolver=dns,
    )
    inspection = build_media_inspection_service(cfg, providers)
    vk_hosts = next(
        p.exact_hostnames for p in providers.providers if p.id == "vk"
    )

    try:
        try:
            resolved = await resolution.resolve(url)
            identity = parse_provider_media_identity(
                provider_id=resolved.provider_id,
                hostname=resolved.provider_url.hostname,
                path=resolved.provider_url.path,
                allowed_hostnames=vk_hosts,
            )
            trusted = build_canonical_provider_url(
                hostname=identity.hostname,
                canonical_path=identity.canonical_path,
            )
            meta = await inspection.inspect(resolved)
        except ResolutionError as exc:
            print(f"FAIL: resolution kind={exc.kind.value}", file=sys.stderr)
            return 1
        except InspectionError as exc:
            print(f"FAIL: inspection kind={exc.kind.value}", file=sys.stderr)
            return 1
    finally:
        await client.aclose()

    print(f"provider={meta.provider_id}")
    print(f"extractor={meta.extraction_tool}")
    print(f"media_id={meta.media_id}")
    print(f"identity_match={meta.media_id == identity.media_id}")
    print(f"canonical_form=video")
    print(f"trusted_host={identity.hostname}")
    # Do not print full URL, title, formats, or query.
    _ = trusted
    return 0


def main() -> None:
    if os.environ.get("FETCHNOW_LIVE_VK_CLIP_SMOKE") != "1":
        _die(
            "Refusing to run: set FETCHNOW_LIVE_VK_CLIP_SMOKE=1 to acknowledge "
            "live network VK clip smoke."
        )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "url",
        nargs="?",
        default="https://vk.ru/clip-235548483_456239236",
        help="Public VK clip URL (default: documented public sample)",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args.url)))


if __name__ == "__main__":
    main()
