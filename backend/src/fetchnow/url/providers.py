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
    # Transport compatibility: HEAD statuses that may retry as bounded GET.
    # Not a bot-protection bypass — only confirmed method-not-useful responses.
    head_fallback_statuses: frozenset[int] = frozenset()


# Confirmed live: VK returns HTTP 418 for HEAD from datacenter clients.
_VK_HEAD_FALLBACK = frozenset({418, 405, 501})
# Confirmed live: Rutube serves HEAD 200 for real pages; keep only standard
# method-not-allowed / not-implemented codes (do not treat 404 as fallback).
_RUTUBE_HEAD_FALLBACK = frozenset({405, 501})


def build_default_providers(settings: Settings) -> tuple[ProviderDescriptor, ...]:
    """Construct the built-in provider registry from settings flags."""
    return (
        ProviderDescriptor(
            id=ProviderID.VK.value,
            display_name="VK",
            # Explicit public hosts only — no naive suffix matching.
            # vkvideo.ru hosts are VK's video frontends observed on redirects.
            exact_hostnames=frozenset(
                {
                    "vk.com",
                    "www.vk.com",
                    "m.vk.com",
                    "vkvideo.ru",
                    "www.vkvideo.ru",
                    "m.vkvideo.ru",
                }
            ),
            enabled=settings.provider_vk_enabled,
            head_fallback_statuses=_VK_HEAD_FALLBACK,
        ),
        ProviderDescriptor(
            id=ProviderID.RUTUBE.value,
            display_name="Rutube",
            exact_hostnames=frozenset({"rutube.ru", "www.rutube.ru"}),
            enabled=settings.provider_rutube_enabled,
            head_fallback_statuses=_RUTUBE_HEAD_FALLBACK,
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
            head_fallback_statuses=frozenset(),
        )
        return self.with_extra_provider(extra)

    def find(self, hostname: str) -> ProviderDescriptor | None:
        """Soft exact-host lookup; None when unknown or disabled."""
        host = hostname.lower().rstrip(".")
        for provider in self.providers:
            if host in provider.exact_hostnames:
                if not provider.enabled:
                    return None
                return provider
        return None

    def resolve(self, hostname: str) -> ProviderDescriptor:
        """Resolve a normalized hostname to a provider or raise."""
        match = self.find(hostname)
        if match is None:
            raise_validation_error("UNSUPPORTED_PROVIDER")
        return match
