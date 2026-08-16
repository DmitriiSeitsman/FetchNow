"""Dzen / ZenYandex provider identity, short-form aliases, and inspection."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from fetchnow.media_inspection.normalize import project_metadata
from fetchnow.media_inspection.ytdlp_parse import parse_ytdlp_json
from fetchnow.resolution.defaults import build_wrapper_registry
from fetchnow.resolution.service import ResolutionService
from fetchnow.url.dns import FakeDnsResolver
from fetchnow.url.models import ProviderID
from fetchnow.url.provider_identity import (
    ProviderIdentityError,
    parse_stable_identity_from_url,
)
from fetchnow.url.providers import ProviderRegistry
from fetchnow.url.validate import URLValidator
from media_inspection_fixtures import DZEN_FIXTURE, dumps_fixture
from media_inspection_helpers import settings

_DZEN_HOSTS = frozenset(
    {
        "dzen.ru",
        "www.dzen.ru",
        "zen.yandex.ru",
        "www.zen.yandex.ru",
    }
)


def _settings(**overrides: object):
    return settings(
        PROVIDER_DZEN_ENABLED=True,
        PROVIDER_VK_ENABLED=True,
        PROVIDER_RUTUBE_ENABLED=True,
        PROVIDER_OK_ENABLED=True,
        **overrides,
    )


@pytest.mark.parametrize(
    ("raw", "media_id", "canonical_path", "hostname"),
    [
        (
            "https://dzen.ru/video/watch/6002240ff8b1af50bb2da5e3",
            "6002240ff8b1af50bb2da5e3",
            "/video/watch/6002240ff8b1af50bb2da5e3",
            "dzen.ru",
        ),
        (
            "https://dzen.ru/shorts/696b8ec4b0c7824d6689eddc?utm=1",
            "696b8ec4b0c7824d6689eddc",
            "/video/watch/696b8ec4b0c7824d6689eddc",
            "dzen.ru",
        ),
        (
            "https://www.dzen.ru/shorts/696b8ec4b0c7824d6689eddc",
            "696b8ec4b0c7824d6689eddc",
            "/video/watch/696b8ec4b0c7824d6689eddc",
            "dzen.ru",
        ),
        (
            "https://zen.yandex.ru/video/watch/6002240ff8b1af50bb2da5e3",
            "6002240ff8b1af50bb2da5e3",
            "/video/watch/6002240ff8b1af50bb2da5e3",
            "zen.yandex.ru",
        ),
        (
            "https://www.zen.yandex.ru/watch/6002240ff8b1af50bb2da5e3",
            "6002240ff8b1af50bb2da5e3",
            "/video/watch/6002240ff8b1af50bb2da5e3",
            "zen.yandex.ru",
        ),
        (
            "https://dzen.ru/media/id/5ee34cac593391747b5aef24/"
            "slug-60c7c443da18892ebfe85ed7",
            "60c7c443da18892ebfe85ed7",
            "/video/watch/60c7c443da18892ebfe85ed7",
            "dzen.ru",
        ),
    ],
)
def test_dzen_stable_identity_aliases(
    raw: str, media_id: str, canonical_path: str, hostname: str
) -> None:
    identity = parse_stable_identity_from_url(
        raw,
        provider_id=ProviderID.DZEN.value,
        allowed_hostnames=_DZEN_HOSTS,
    )
    assert identity.provider_id == ProviderID.DZEN.value
    assert identity.media_id == media_id
    assert identity.canonical_path == canonical_path
    assert identity.hostname == hostname


@pytest.mark.parametrize(
    "raw",
    [
        "https://dzen.ru/a/60c7c443da18892ebfe85ed7",
        "https://dzen.ru/b/60c7c443da18892ebfe85ed7",
        "https://dzen.ru/id/5ee34cac593391747b5aef24",
        "https://dzen.ru/video/",
        "https://dzen.ru/embed/6002240ff8b1af50bb2da5e3",
        "https://dzen.ru/live/6002240ff8b1af50bb2da5e3",
        "https://dzen.ru/video/watch/",
        "https://dzen.ru/shorts/",
    ],
)
def test_dzen_non_media_paths_rejected(raw: str) -> None:
    with pytest.raises(ProviderIdentityError) as exc:
        parse_stable_identity_from_url(
            raw,
            provider_id=ProviderID.DZEN.value,
            allowed_hostnames=_DZEN_HOSTS,
        )
    assert exc.value.reason in {
        "DZEN_PATH_INVALID",
        "FORBIDDEN_MEDIA_PATH",
        "IDENTITY_URL_PARSE",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        (
            "https://dzen.ru/video/watch/6002240ff8b1af50bb2da5e3",
            "https://dzen.ru/video/watch/6002240ff8b1af50bb2da5e3",
        ),
        (
            "https://dzen.ru/shorts/696b8ec4b0c7824d6689eddc?utm=share",
            "https://dzen.ru/video/watch/696b8ec4b0c7824d6689eddc",
        ),
        (
            "https://www.dzen.ru/shorts/696b8ec4b0c7824d6689eddc",
            "https://dzen.ru/video/watch/696b8ec4b0c7824d6689eddc",
        ),
        (
            "https://zen.yandex.ru/media/id/5ee34cac593391747b5aef24/"
            "slug-60c7c443da18892ebfe85ed7",
            "https://zen.yandex.ru/video/watch/60c7c443da18892ebfe85ed7",
        ),
    ],
)
async def test_dzen_resolution_canonicalizes_aliases(raw: str, canonical: str) -> None:
    cfg = _settings()
    providers = ProviderRegistry.from_settings(cfg)
    dns = FakeDnsResolver(default_addresses=("8.8.8.8",))
    validator = URLValidator(cfg, registry=providers, resolver=dns)
    http = MagicMock()
    http.aclose = AsyncMock()
    http.fetch_document = AsyncMock(
        side_effect=AssertionError("stable dzen must not HTTP fetch")
    )
    wrappers = build_wrapper_registry(cfg, providers)
    service = ResolutionService(
        cfg,
        provider_registry=providers,
        wrapper_registry=wrappers,
        validator=validator,
        http_client=http,
        dns_resolver=dns,
    )
    validated = await validator.validate(raw)
    rewritten = service._with_stable_provider_identity(validated)
    assert rewritten.url.canonical == canonical
    assert rewritten.provider_id == ProviderID.DZEN.value
    assert "/shorts/" not in rewritten.url.path
    assert "?" not in rewritten.url.canonical

    result = await service.resolve(raw)
    assert result.provider_id == ProviderID.DZEN.value
    assert result.canonical_provider_url == canonical
    http.fetch_document.assert_not_called()


def test_dzen_inspection_fixture_resolves_dual_extractor_labels() -> None:
    draft = parse_ytdlp_json(
        dumps_fixture(DZEN_FIXTURE),
        expected_provider_id=ProviderID.DZEN.value,
        expected_canonical_url="https://dzen.ru/video/watch/6002240ff8b1af50bb2da5e3",
        expected_media_id="6002240ff8b1af50bb2da5e3",
        allowed_extractor_keys=frozenset({"dzen.ru"}),
        allowed_hostnames=_DZEN_HOSTS,
        max_height=4320,
        max_width=7680,
        max_bytes=2_000_000_000,
        max_duration=6 * 3600,
        tool_version="2026.07.04",
    )
    assert draft.extractor_key == "dzen.ru"
    assert draft.media_id == "6002240ff8b1af50bb2da5e3"
    meta = project_metadata(
        draft,
        settings(MEDIA_MUXING_ENABLED=True),
        trusted_canonical_url="https://dzen.ru/video/watch/6002240ff8b1af50bb2da5e3",
        trusted_media_id="6002240ff8b1af50bb2da5e3",
        trusted_provider_id=ProviderID.DZEN.value,
    )
    assert meta.provider_id == ProviderID.DZEN.value
    assert meta.title == "Sample Dzen video"
    assert meta.duration_seconds == 243
    heights = sorted(f.height for f in meta.formats if f.height is not None)
    assert heights == [360, 720]
    assert all(f.has_video and f.has_audio for f in meta.formats)
    assert all(f.container == "mp4" for f in meta.formats)
