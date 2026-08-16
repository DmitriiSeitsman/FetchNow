"""OK.ru / Odnoklassniki provider identity, resolution aliases, and inspection."""

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
from media_inspection_fixtures import OK_FIXTURE, dumps_fixture
from media_inspection_helpers import settings

_OK_HOSTS = frozenset(
    {
        "ok.ru",
        "www.ok.ru",
        "m.ok.ru",
        "mobile.ok.ru",
        "odnoklassniki.ru",
        "www.odnoklassniki.ru",
        "m.odnoklassniki.ru",
        "mobile.odnoklassniki.ru",
    }
)


def _settings(**overrides: object):
    return settings(
        PROVIDER_OK_ENABLED=True,
        PROVIDER_VK_ENABLED=True,
        PROVIDER_RUTUBE_ENABLED=True,
        **overrides,
    )


@pytest.mark.parametrize(
    ("raw", "media_id", "canonical_path"),
    [
        ("https://ok.ru/video/20079905452", "20079905452", "/video/20079905452"),
        (
            "https://www.ok.ru/video/20079905452?fromTime=5&st.cmd=foo",
            "20079905452",
            "/video/20079905452",
        ),
        (
            "https://m.ok.ru/videoembed/20079905452",
            "20079905452",
            "/video/20079905452",
        ),
        (
            "https://ok.ru/web-api/video/moviePlayer/20079905452",
            "20079905452",
            "/video/20079905452",
        ),
        (
            "https://odnoklassniki.ru/video/63567059965189-0",
            "63567059965189-0",
            "/video/63567059965189-0",
        ),
    ],
)
def test_ok_stable_identity_aliases(
    raw: str, media_id: str, canonical_path: str
) -> None:
    identity = parse_stable_identity_from_url(
        raw,
        provider_id=ProviderID.OK.value,
        allowed_hostnames=_OK_HOSTS,
    )
    assert identity.provider_id == ProviderID.OK.value
    assert identity.media_id == media_id
    assert identity.canonical_path == canonical_path


@pytest.mark.parametrize(
    "raw",
    [
        "https://ok.ru/live/20079905452",
        "https://ok.ru/video/",
        "https://ok.ru/videos/20079905452",
        "https://ok.ru/profile/20079905452",
        "https://ok.ru/dk?st.mvId=20079905452",
        "https://ok.ru/group/20079905452",
        "https://ok.ru/video/not-numeric",
    ],
)
def test_ok_non_media_paths_rejected(raw: str) -> None:
    with pytest.raises(ProviderIdentityError) as exc:
        parse_stable_identity_from_url(
            raw,
            provider_id=ProviderID.OK.value,
            allowed_hostnames=_OK_HOSTS,
        )
    assert exc.value.reason in {
        "OK_PATH_INVALID",
        "FORBIDDEN_MEDIA_PATH",
        "IDENTITY_URL_PARSE",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        (
            "https://ok.ru/video/20079905452",
            "https://ok.ru/video/20079905452",
        ),
        (
            "https://ok.ru/videoembed/20079905452",
            "https://ok.ru/video/20079905452",
        ),
        (
            "https://m.ok.ru/video/20079905452?utm=1",
            "https://m.ok.ru/video/20079905452",
        ),
    ],
)
async def test_ok_resolution_canonicalizes_aliases(raw: str, canonical: str) -> None:
    cfg = _settings()
    providers = ProviderRegistry.from_settings(cfg)
    dns = FakeDnsResolver(default_addresses=("8.8.8.8",))
    validator = URLValidator(cfg, registry=providers, resolver=dns)
    http = MagicMock()
    http.aclose = AsyncMock()
    http.fetch_document = AsyncMock(
        side_effect=AssertionError("stable ok must not HTTP fetch")
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
    assert rewritten.provider_id == ProviderID.OK.value
    assert "/videoembed/" not in rewritten.url.path
    assert "?" not in rewritten.url.canonical

    result = await service.resolve(raw)
    assert result.provider_id == ProviderID.OK.value
    assert result.canonical_provider_url == canonical
    http.fetch_document.assert_not_called()


def test_ok_inspection_fixture_projects_muxed_qualities() -> None:
    draft = parse_ytdlp_json(
        dumps_fixture(OK_FIXTURE),
        expected_provider_id=ProviderID.OK.value,
        expected_canonical_url="https://ok.ru/video/20079905452",
        expected_media_id="20079905452",
        allowed_extractor_keys=frozenset({"odnoklassniki"}),
        allowed_hostnames=_OK_HOSTS,
        max_height=4320,
        max_width=7680,
        max_bytes=2_000_000_000,
        max_duration=6 * 3600,
        tool_version="2026.07.04",
    )
    assert draft.extractor_key == "odnoklassniki"
    assert draft.media_id == "20079905452"
    meta = project_metadata(
        draft,
        settings(MEDIA_MUXING_ENABLED=True),
        trusted_canonical_url="https://ok.ru/video/20079905452",
        trusted_media_id="20079905452",
        trusted_provider_id=ProviderID.OK.value,
    )
    assert meta.provider_id == ProviderID.OK.value
    assert meta.title == "Sample OK video"
    assert meta.duration_seconds == 100
    heights = sorted(f.height for f in meta.formats if f.height is not None)
    assert heights == [360, 720]
    assert all(f.has_video and f.has_audio for f in meta.formats)
    assert all(f.container == "webm" for f in meta.formats)
