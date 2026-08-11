"""Production wrapper-registry construction (PR3B)."""

from __future__ import annotations

from fetchnow.core.config import Settings
from fetchnow.resolution.registry import WrapperResolverRegistry
from fetchnow.resolution.yandex_preview import (
    YandexVideoPreviewResolver,
    provider_hosts_from_registry,
)
from fetchnow.url.providers import ProviderRegistry


def build_wrapper_registry(
    settings: Settings,
    provider_registry: ProviderRegistry,
) -> WrapperResolverRegistry:
    """Build the immutable production wrapper registry.

    ``settings`` is accepted for future feature flags; Yandex preview is always
    registered in PR3B when the process loads this factory.
    """
    del settings  # reserved for future enable/disable flags
    hosts: set[str] = set()
    for provider in provider_registry.providers:
        if not provider.enabled:
            continue
        hosts.update(provider.exact_hostnames)
    resolver = YandexVideoPreviewResolver(
        supported_provider_hosts=provider_hosts_from_registry(hosts),
    )
    return WrapperResolverRegistry.from_resolvers([resolver])
