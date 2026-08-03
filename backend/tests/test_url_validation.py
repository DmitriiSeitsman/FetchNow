"""Unit tests for URL validation pipeline."""

from __future__ import annotations

import logging

import pytest

from fetchnow.core.config import Settings
from fetchnow.url.dns import FakeDnsResolver
from fetchnow.url.errors import URLValidationError
from fetchnow.url.models import ProviderID
from fetchnow.url.providers import ProviderRegistry
from fetchnow.url.validate import URLValidator


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "APP_ENV": "test",
        "LOG_LEVEL": "INFO",
        "URL_ALLOWED_SCHEMES": "http,https",
        "URL_ALLOWED_PORTS": "80,443",
        "URL_MAX_LENGTH": 4096,
        "DNS_RESOLUTION_TIMEOUT_SECONDS": 1,
        "PROVIDER_VK_ENABLED": True,
        "PROVIDER_RUTUBE_ENABLED": True,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _validator(
    settings: Settings | None = None,
    *,
    resolver: FakeDnsResolver | None = None,
    registry: ProviderRegistry | None = None,
) -> URLValidator:
    settings = settings or _settings()
    return URLValidator(
        settings,
        registry=registry or ProviderRegistry.from_settings(settings),
        resolver=resolver or FakeDnsResolver(default_addresses=("8.8.8.8",)),
    )


@pytest.mark.asyncio
async def test_supported_vk_url() -> None:
    result = await _validator().validate("https://vk.com/video-123_456")
    assert result.provider_id == ProviderID.VK
    assert result.url.hostname == "vk.com"
    assert result.url.canonical == "https://vk.com/video-123_456"


@pytest.mark.asyncio
async def test_supported_rutube_url() -> None:
    result = await _validator().validate("https://rutube.ru/video/abcdef0123456789/")
    assert result.provider_id == ProviderID.RUTUBE
    assert result.url.hostname == "rutube.ru"


@pytest.mark.asyncio
async def test_mixed_case_hostname() -> None:
    result = await _validator().validate("https://WWW.VK.COM/video-1")
    assert result.url.hostname == "www.vk.com"


@pytest.mark.asyncio
async def test_trailing_dot() -> None:
    result = await _validator().validate("https://vk.com./video-1")
    assert result.url.hostname == "vk.com"
    assert result.url.canonical.startswith("https://vk.com/")


@pytest.mark.asyncio
async def test_default_port_normalization() -> None:
    result = await _validator().validate("https://vk.com:443/video-1")
    assert result.url.port is None
    assert result.url.canonical == "https://vk.com/video-1"


@pytest.mark.asyncio
async def test_unsupported_port() -> None:
    with pytest.raises(URLValidationError) as exc:
        await _validator().validate("https://vk.com:8443/video-1")
    assert exc.value.code == "INVALID_URL"


@pytest.mark.asyncio
async def test_credentials_rejected() -> None:
    with pytest.raises(URLValidationError) as exc:
        await _validator().validate("https://user:pass@vk.com/video-1")
    assert exc.value.code == "INVALID_URL"


@pytest.mark.asyncio
async def test_fragment_removed_from_canonical() -> None:
    result = await _validator().validate("https://vk.com/video-1#section")
    assert "#" not in result.url.canonical
    assert result.url.canonical == "https://vk.com/video-1"


@pytest.mark.asyncio
async def test_deceptive_suffix() -> None:
    with pytest.raises(URLValidationError) as exc:
        await _validator().validate("https://vk.com.attacker.example/video")
    assert exc.value.code == "UNSUPPORTED_PROVIDER"


@pytest.mark.asyncio
async def test_deceptive_prefix() -> None:
    with pytest.raises(URLValidationError) as exc:
        await _validator().validate("https://notvk.com/video")
    assert exc.value.code == "UNSUPPORTED_PROVIDER"


@pytest.mark.asyncio
async def test_provider_disabled() -> None:
    settings = _settings(PROVIDER_VK_ENABLED=False)
    with pytest.raises(URLValidationError) as exc:
        await _validator(settings).validate("https://vk.com/video-1")
    assert exc.value.code == "UNSUPPORTED_PROVIDER"


@pytest.mark.asyncio
async def test_unknown_provider() -> None:
    with pytest.raises(URLValidationError) as exc:
        await _validator().validate("https://example.com/video.mp4")
    assert exc.value.code == "UNSUPPORTED_PROVIDER"


@pytest.mark.asyncio
async def test_localhost_blocked() -> None:
    with pytest.raises(URLValidationError) as exc:
        await _validator().validate("https://localhost/video")
    assert exc.value.code == "BLOCKED_DESTINATION"


@pytest.mark.asyncio
async def test_ipv4_private_blocked() -> None:
    with pytest.raises(URLValidationError) as exc:
        await _validator().validate("https://192.168.1.1/video")
    assert exc.value.code == "BLOCKED_DESTINATION"


@pytest.mark.asyncio
async def test_ipv6_private_blocked() -> None:
    with pytest.raises(URLValidationError) as exc:
        await _validator().validate("https://[fc00::1]/video")
    assert exc.value.code == "BLOCKED_DESTINATION"


@pytest.mark.asyncio
async def test_ipv4_mapped_ipv6_private_blocked() -> None:
    with pytest.raises(URLValidationError) as exc:
        await _validator().validate("https://[::ffff:192.168.0.1]/video")
    assert exc.value.code == "BLOCKED_DESTINATION"


@pytest.mark.asyncio
async def test_public_dns_result_allows() -> None:
    resolver = FakeDnsResolver(records={"vk.com": ["8.8.8.8"]})
    result = await _validator(resolver=resolver).validate("https://vk.com/video-1")
    assert result.provider_id == ProviderID.VK


@pytest.mark.asyncio
async def test_mixed_public_private_dns_denied() -> None:
    resolver = FakeDnsResolver(records={"vk.com": ["8.8.8.8", "10.0.0.8"]})
    with pytest.raises(URLValidationError) as exc:
        await _validator(resolver=resolver).validate("https://vk.com/video-1")
    assert exc.value.code == "BLOCKED_DESTINATION"


@pytest.mark.asyncio
async def test_dns_timeout() -> None:
    resolver = FakeDnsResolver(timeouts={"vk.com"})
    with pytest.raises(URLValidationError) as exc:
        await _validator(resolver=resolver).validate("https://vk.com/video-1")
    assert exc.value.code == "SOURCE_TIMEOUT"


@pytest.mark.asyncio
async def test_empty_dns_result() -> None:
    resolver = FakeDnsResolver(empty={"vk.com"})
    with pytest.raises(URLValidationError) as exc:
        await _validator(resolver=resolver).validate("https://vk.com/video-1")
    assert exc.value.code == "BLOCKED_DESTINATION"


@pytest.mark.asyncio
async def test_query_token_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    secret = "super-secret-token-value-xyz"
    url = f"https://vk.com/video-1?token={secret}"
    with caplog.at_level(logging.INFO, logger="fetchnow.url.validate"):
        result = await _validator().validate(url)
    assert result.url.query == f"token={secret}"
    # Canonical returned to clients must not include the query.
    assert secret not in result.url.canonical
    assert "?" not in result.url.canonical
    joined = " ".join(record.getMessage() for record in caplog.records)
    assert secret not in joined
    assert url not in joined
    for record in caplog.records:
        assert secret not in str(record.__dict__)


@pytest.mark.asyncio
async def test_canonical_omits_query() -> None:
    result = await _validator().validate("https://vk.com/video-1?list=abc")
    assert result.url.canonical == "https://vk.com/video-1"
    assert result.url.query == "list=abc"


def test_empty_schemes_configuration_error() -> None:
    with pytest.raises(ValueError):
        Settings(URL_ALLOWED_SCHEMES="")


def test_empty_ports_configuration_error() -> None:
    with pytest.raises(ValueError):
        Settings(URL_ALLOWED_PORTS="")
