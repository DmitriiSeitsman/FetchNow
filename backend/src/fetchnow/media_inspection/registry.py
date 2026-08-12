"""Immutable provider-scoped inspection extractor registry."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

from fetchnow.media_inspection.errors import (
    InspectionErrorKind,
    raise_inspection_error,
)
from fetchnow.media_inspection.identity import normalize_hostname
from fetchnow.media_inspection.protocols import MediaExtractor
from fetchnow.url.models import ProviderID
from fetchnow.url.providers import ProviderRegistry

_REGISTRY_SEAL = object()
_SAFE_PROVIDER_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_SAFE_EXTRACTOR_KEY = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_FORBIDDEN_EXTRACTOR_KEYS = frozenset({"generic", "default", "all", "end"})


def _normalize_provider_id(provider_id: str) -> str:
    if not isinstance(provider_id, str) or not _SAFE_PROVIDER_ID.fullmatch(provider_id):
        raise_inspection_error(
            InspectionErrorKind.INTERNAL_INSPECTION_ERROR,
            internal_reason="UNSAFE_PROVIDER_ID",
        )
    return provider_id


def _normalize_extractor_key(raw: str) -> str:
    if not isinstance(raw, str):
        raise_inspection_error(
            InspectionErrorKind.INTERNAL_INSPECTION_ERROR,
            internal_reason="UNSAFE_EXTRACTOR_KEY",
        )
    key = raw.strip().lower()
    if not key or not _SAFE_EXTRACTOR_KEY.fullmatch(key):
        raise_inspection_error(
            InspectionErrorKind.INTERNAL_INSPECTION_ERROR,
            internal_reason="UNSAFE_EXTRACTOR_KEY",
        )
    if key in _FORBIDDEN_EXTRACTOR_KEYS or key.startswith("generic"):
        raise_inspection_error(
            InspectionErrorKind.INTERNAL_INSPECTION_ERROR,
            internal_reason="FORBIDDEN_EXTRACTOR_KEY",
        )
    return key


@dataclass(frozen=True, slots=True)
class ExtractorBinding:
    """Frozen ownership snapshot captured once at registry build time.

    Runtime policy must use these frozen fields — never re-read mutable
    extractor/provider properties for host or extractor allowlisting.
    """

    provider_id: str
    exact_hostnames: frozenset[str]
    allowed_extractor_keys: frozenset[str]
    extractor_id: str
    extractor: MediaExtractor = field(repr=False)


class InspectionExtractorRegistry:
    """Fail-closed immutable registry. Build only via factory methods."""

    __slots__ = ("_bindings", "_by_provider", "_by_host", "_frozen")

    _bindings: tuple[ExtractorBinding, ...]
    _by_provider: Mapping[str, ExtractorBinding]
    _by_host: Mapping[str, ExtractorBinding]
    _frozen: bool

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise_inspection_error(
            InspectionErrorKind.INTERNAL_INSPECTION_ERROR,
            internal_reason="REGISTRY_CONSTRUCTOR_BLOCKED",
        )

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_frozen", False):
            raise AttributeError("InspectionExtractorRegistry is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        raise AttributeError("InspectionExtractorRegistry is immutable")

    @classmethod
    def _create(
        cls,
        bindings: tuple[ExtractorBinding, ...],
        by_provider: Mapping[str, ExtractorBinding],
        by_host: Mapping[str, ExtractorBinding],
        *,
        seal: object,
    ) -> InspectionExtractorRegistry:
        if seal is not _REGISTRY_SEAL:
            raise_inspection_error(
                InspectionErrorKind.INTERNAL_INSPECTION_ERROR,
                internal_reason="REGISTRY_SEAL_INVALID",
            )
        obj = object.__new__(cls)
        object.__setattr__(obj, "_frozen", False)
        object.__setattr__(obj, "_bindings", bindings)
        object.__setattr__(obj, "_by_provider", MappingProxyType(dict(by_provider)))
        object.__setattr__(obj, "_by_host", MappingProxyType(dict(by_host)))
        object.__setattr__(obj, "_frozen", True)
        return obj

    @property
    def bindings(self) -> tuple[ExtractorBinding, ...]:
        return self._bindings

    @classmethod
    def from_bindings(
        cls, bindings: Sequence[ExtractorBinding]
    ) -> InspectionExtractorRegistry:
        """Validate and seal bindings into an immutable registry."""
        if not bindings:
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_UNSUPPORTED_PROVIDER,
                internal_reason="EMPTY_REGISTRY",
            )
        by_provider: dict[str, ExtractorBinding] = {}
        by_host: dict[str, ExtractorBinding] = {}
        frozen_bindings: list[ExtractorBinding] = []
        for binding in bindings:
            provider_id = _normalize_provider_id(binding.provider_id)
            if provider_id in by_provider:
                raise_inspection_error(
                    InspectionErrorKind.INTERNAL_INSPECTION_ERROR,
                    internal_reason="DUPLICATE_PROVIDER_BINDING",
                )
            # Snapshot once — do not retain caller-owned mutable containers.
            raw_hosts = binding.exact_hostnames
            if not raw_hosts:
                raise_inspection_error(
                    InspectionErrorKind.INSPECTION_UNSUPPORTED_PROVIDER,
                    internal_reason="EMPTY_HOST_ALLOWLIST",
                )
            hosts = frozenset(normalize_hostname(h) for h in raw_hosts)
            if not hosts:
                raise_inspection_error(
                    InspectionErrorKind.INSPECTION_UNSUPPORTED_PROVIDER,
                    internal_reason="EMPTY_HOST_ALLOWLIST",
                )
            raw_keys = binding.allowed_extractor_keys
            if not raw_keys:
                raise_inspection_error(
                    InspectionErrorKind.INSPECTION_UNSUPPORTED_PROVIDER,
                    internal_reason="EMPTY_EXTRACTOR_ALLOWLIST",
                )
            keys = frozenset(_normalize_extractor_key(k) for k in raw_keys)
            extractor = binding.extractor
            extractor_id_raw = getattr(extractor, "extractor_id", None)
            if not isinstance(extractor_id_raw, str) or not extractor_id_raw:
                raise_inspection_error(
                    InspectionErrorKind.INTERNAL_INSPECTION_ERROR,
                    internal_reason="MISSING_EXTRACTOR_ID",
                )
            extractor_id = _normalize_provider_id(
                extractor_id_raw.strip().lower().replace("-", "_")
            )
            snap = ExtractorBinding(
                provider_id=provider_id,
                exact_hostnames=hosts,
                allowed_extractor_keys=keys,
                extractor_id=extractor_id,
                extractor=extractor,
            )
            for host in snap.exact_hostnames:
                if host in by_host:
                    raise_inspection_error(
                        InspectionErrorKind.INTERNAL_INSPECTION_ERROR,
                        internal_reason="HOST_OWNERSHIP_COLLISION",
                    )
                by_host[host] = snap
            by_provider[provider_id] = snap
            frozen_bindings.append(snap)
        return cls._create(
            tuple(frozen_bindings),
            by_provider,
            by_host,
            seal=_REGISTRY_SEAL,
        )

    @classmethod
    def from_provider_registry(
        cls,
        providers: ProviderRegistry,
        *,
        extractors_by_provider: Mapping[str, MediaExtractor],
        allowed_extractor_keys: Mapping[str, frozenset[str]],
    ) -> InspectionExtractorRegistry:
        """Build bindings from enabled ProviderRegistry ownership snapshot."""
        bindings: list[ExtractorBinding] = []
        for descriptor in providers.providers:
            if not descriptor.enabled:
                continue
            if descriptor.id not in extractors_by_provider:
                continue
            if descriptor.id not in allowed_extractor_keys:
                continue
            extractor = extractors_by_provider[descriptor.id]
            extractor_id = getattr(extractor, "extractor_id", "unknown")
            bindings.append(
                ExtractorBinding(
                    provider_id=descriptor.id,
                    exact_hostnames=frozenset(descriptor.exact_hostnames),
                    allowed_extractor_keys=frozenset(
                        allowed_extractor_keys[descriptor.id]
                    ),
                    extractor_id=(
                        extractor_id
                        if isinstance(extractor_id, str)
                        else "unknown"
                    ),
                    extractor=extractor,
                )
            )
        return cls.from_bindings(bindings)

    def resolve(self, *, provider_id: str, hostname: str) -> ExtractorBinding:
        """Resolve by frozen provider id + exact hostname ownership."""
        host = normalize_hostname(hostname)
        pid = provider_id.strip().lower()
        binding = self._by_provider.get(pid)
        if binding is None:
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_UNSUPPORTED_PROVIDER,
                internal_reason="PROVIDER_NOT_REGISTERED",
            )
        host_binding = self._by_host.get(host)
        if host_binding is None or host_binding.provider_id != binding.provider_id:
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH,
                internal_reason="HOST_NOT_OWNED",
            )
        return binding

    def owned_hostnames(self) -> frozenset[str]:
        return frozenset(self._by_host.keys())


# Immutable default allowlist — generic/default never included.
DEFAULT_YTDLP_EXTRACTOR_KEYS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        ProviderID.VK.value: frozenset({"vk"}),
        ProviderID.RUTUBE.value: frozenset({"rutube"}),
    }
)
