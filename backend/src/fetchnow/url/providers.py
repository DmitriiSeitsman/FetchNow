"""Explicit media provider registry."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from fetchnow.core.config import Settings
from fetchnow.url.errors import raise_validation_error
from fetchnow.url.models import ProviderID


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    """Immutable provider metadata and hostname allowlist."""

    id: str
    display_name: str
    exact_hostnames: frozenset[str]
    enabled: bool


def build_default_providers(settings: Settings) -> tuple[ProviderDescriptor, ...]:
    """Construct the built-in provider registry from settings flags."""
    return (
        ProviderDescriptor(
            id=ProviderID.VK.value,
            display_name="VK",
            # Explicit public hosts only — no naive suffix matching.
            exact_hostnames=frozenset({"vk.com", "www.vk.com", "m.vk.com"}),
            enabled=settings.provider_vk_enabled,
        ),
        ProviderDescriptor(
            id=ProviderID.RUTUBE.value,
            display_name="Rutube",
            exact_hostnames=frozenset({"rutube.ru", "www.rutube.ru"}),
            enabled=settings.provider_rutube_enabled,
        ),
    )


@dataclass(frozen=True, slots=True)
class ProviderRegistry:
    """Hostname → provider lookup with label-boundary-safe exact matching."""

    providers: tuple[ProviderDescriptor, ...]

    @classmethod
    def from_settings(cls, settings: Settings) -> ProviderRegistry:
        return cls(providers=build_default_providers(settings))

    @classmethod
    def from_descriptors(
        cls, descriptors: Sequence[ProviderDescriptor]
    ) -> ProviderRegistry:
        return cls(providers=tuple(descriptors))

    def with_extra_provider(self, descriptor: ProviderDescriptor) -> ProviderRegistry:
        """Return a copy that includes an additional descriptor (tests)."""
        return ProviderRegistry(providers=(*self.providers, descriptor))

    def with_extra_exact_hosts(
        self,
        *,
        provider_id: str,
        display_name: str,
        hostnames: Iterable[str],
        enabled: bool = True,
    ) -> ProviderRegistry:
        """Return a copy with an additional exact-host provider (fixtures)."""
        extra = ProviderDescriptor(
            id=provider_id,
            display_name=display_name,
            exact_hostnames=frozenset(h.lower().rstrip(".") for h in hostnames),
            enabled=enabled,
        )
        return self.with_extra_provider(extra)

    def resolve(self, hostname: str) -> ProviderDescriptor:
        """Resolve a normalized hostname to a provider or raise."""
        host = hostname.lower().rstrip(".")
        match: ProviderDescriptor | None = None
        for provider in self.providers:
            if host in provider.exact_hostnames:
                match = provider
                break
        if match is None:
            raise_validation_error("UNSUPPORTED_PROVIDER")
        resolved = match
        if not resolved.enabled:
            raise_validation_error("UNSUPPORTED_PROVIDER")
        return resolved
