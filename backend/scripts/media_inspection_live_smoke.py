#!/usr/bin/env python3
"""Manual-only live smoke for media inspection (disabled by default).

NOT part of CI / ``make check``. Requires explicit opt-in and network access.

Usage (from repo root, with backend venv and inspection enabled):

  FETCHNOW_LIVE_MEDIA_INSPECTION=1 \\
    MEDIA_INSPECTION_ENABLED=true \\
    MEDIA_INSPECTION_YTDLP_PATH=/absolute/path/to/yt-dlp \\
    backend/.venv/bin/python backend/scripts/media_inspection_live_smoke.py \\
    'https://vk.com/video-<id>'

Prints only a sanitized metadata summary (provider, media id, duration,
format counts/labels). Never prints query strings, direct media URLs, raw
JSON, or tool stderr. Does not download media bytes.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from fetchnow.core.config import Settings
from fetchnow.media_inspection.defaults import build_media_inspection_service
from fetchnow.media_inspection.errors import InspectionError
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

    try:
        try:
            resolved = await resolution.resolve(url)
            meta = await inspection.inspect(resolved)
        except ResolutionError as exc:
            print(f"FAIL: resolution kind={exc.kind.value}", file=sys.stderr)
            return 1
        except InspectionError as exc:
            print(f"FAIL: inspection kind={exc.kind.value}", file=sys.stderr)
            return 1
    finally:
        await client.aclose()

    free = sum(1 for fmt in meta.formats if fmt.free_tier_eligible)
    print(f"provider={meta.provider_id}")
    print(f"media_id={meta.media_id}")
    print(f"duration_seconds={meta.duration_seconds}")
    print(f"format_count={len(meta.formats)}")
    print(f"free_tier_formats={free}")
    print(f"muxing_required={meta.muxing_required}")
    print(f"extraction_tool={meta.extraction_tool}")
    # Canonical is query/fragment-stripped by construction.
    print(f"canonical={meta.canonical_provider_url}")
    for fmt in meta.formats[:8]:
        print(
            "format "
            f"id={fmt.format_option_id} "
            f"label={fmt.quality_label} "
            f"height={fmt.height} "
            f"free={fmt.free_tier_eligible} "
            f"category={fmt.category.value}"
        )
    return 0


def main() -> None:
    if os.environ.get("FETCHNOW_LIVE_MEDIA_INSPECTION") != "1":
        _die(
            "Refusing to run: set FETCHNOW_LIVE_MEDIA_INSPECTION=1 to acknowledge "
            "live network media inspection."
        )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Single public VK/Rutube/Yandex-preview URL")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args.url)))


if __name__ == "__main__":
    main()
