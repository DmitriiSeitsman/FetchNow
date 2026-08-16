"""Wrapper resolution orchestration service."""

from __future__ import annotations

from fetchnow.core.config import Settings
from fetchnow.network.client import SafeHTTPClient
from fetchnow.network.models import SafeDocumentResponse, SafeDocumentTarget
from fetchnow.resolution.errors import (
    ResolutionError,
    ResolutionErrorKind,
    normalize_resolver_error,
    raise_resolution_error,
)
from fetchnow.resolution.models import (
    ResolutionHop,
    ResolutionProvenance,
    ResolutionResult,
    freeze_resolution_metadata,
    require_audit_token,
)
from fetchnow.resolution.protocols import DocumentFetchPort
from fetchnow.resolution.registry import WrapperRegistryError, WrapperResolverRegistry
from fetchnow.resolution.wrapper_safety import (
    WrapperSafetyValidator,
    internal_url_fingerprint,
)
from fetchnow.url.destination import (
    is_reserved_hostname,
    looks_like_nonstandard_ipv4,
    parse_ip_literal,
)
from fetchnow.url.dns import DnsResolver
from fetchnow.url.errors import URLValidationError
from fetchnow.url.models import NormalizedMediaURL, ProviderID, URLValidationResult
from fetchnow.url.provider_identity import (
    ProviderIdentityError,
    build_public_canonical_url,
    parse_stable_provider_identity,
)
from fetchnow.url.providers import ProviderRegistry
from fetchnow.url.validate import URLValidator


class ScopedDocumentFetchPort:
    """DocumentFetchPort locked to an orchestration-derived host allowlist.

    Resolvers receive this port and cannot supply or override allowed hosts.
    """

    __slots__ = ("_client", "_safety", "_allowed_hostnames")

    def __init__(
        self,
        client: SafeHTTPClient,
        safety: WrapperSafetyValidator,
        allowed_hostnames: frozenset[str],
    ) -> None:
        normalized = frozenset(
            h.strip().lower().rstrip(".") for h in allowed_hostnames if h and h.strip()
        )
        if not normalized:
            raise_resolution_error(
                ResolutionErrorKind.INTERNAL_ERROR,
                internal_reason="EMPTY_DOCUMENT_ALLOWLIST",
            )
        self._client = client
        self._safety = safety
        self._allowed_hostnames = frozenset(normalized)

    @property
    def allowed_hostnames(self) -> frozenset[str]:
        return self._allowed_hostnames

    async def fetch_document(
        self,
        validated: SafeDocumentTarget,
    ) -> SafeDocumentResponse:
        return await self._client.fetch_document(
            validated,
            allowed_hostnames=self._allowed_hostnames,
            revalidate=self._safety.validate,
        )


class ResolutionService:
    """Provider-first wrapper resolution orchestrator."""

    def __init__(
        self,
        settings: Settings,
        *,
        provider_registry: ProviderRegistry,
        wrapper_registry: WrapperResolverRegistry,
        validator: URLValidator,
        http_client: SafeHTTPClient,
        dns_resolver: DnsResolver | None = None,
    ) -> None:
        self._settings = settings
        self._providers = provider_registry
        self._wrappers = wrapper_registry
        self._validator = validator
        self._safety = WrapperSafetyValidator(settings, resolver=dns_resolver)
        self._http_client = http_client

    def _scoped_documents(self, exact_hostnames: frozenset[str]) -> DocumentFetchPort:
        """Build a resolver-scoped document port from registry host ownership."""
        return ScopedDocumentFetchPort(
            self._http_client,
            self._safety,
            exact_hostnames,
        )

    async def resolve(self, raw_url: str) -> ResolutionResult:
        """Resolve a submitted URL to a validated terminal provider result."""
        try:
            return await self._resolve_inner(raw_url)
        except ResolutionError:
            raise
        except WrapperRegistryError:
            raise_resolution_error(
                ResolutionErrorKind.INTERNAL_ERROR,
                internal_reason="WRAPPER_REGISTRY",
            )
        except Exception as exc:
            raise_resolution_error(
                ResolutionErrorKind.INTERNAL_ERROR,
                internal_reason=type(exc).__name__.upper()[:64],
            )

    async def _resolve_inner(self, raw_url: str) -> ResolutionResult:
        parsed = self._safety.parse_for_classification(raw_url)

        # Reserved / literal destinations fail before classification / DNS for
        # unknown hosts.
        if is_reserved_hostname(parsed.hostname):
            raise_resolution_error(
                ResolutionErrorKind.UNSAFE_RESOLUTION_TARGET,
                internal_reason="RESERVED_HOSTNAME",
            )
        if looks_like_nonstandard_ipv4(parsed.hostname):
            raise_resolution_error(
                ResolutionErrorKind.UNSAFE_RESOLUTION_TARGET,
                internal_reason="NONSTANDARD_IPV4",
            )
        if parse_ip_literal(parsed.hostname) is not None:
            raise_resolution_error(
                ResolutionErrorKind.UNSAFE_RESOLUTION_TARGET,
                internal_reason="IP_LITERAL",
            )

        provider = self._providers.find(parsed.hostname)
        if provider is not None:
            try:
                validated = await self._validator.validate(raw_url)
            except URLValidationError as exc:
                self._map_url_error(exc)
            validated = self._with_stable_provider_identity(validated)
            return ResolutionResult(
                submitted_url=raw_url,
                validated=validated,
                provider_id=validated.provider_id,
                provider_display_name=validated.provider_display_name,
                canonical_provider_url=validated.url.canonical,
                wrapper_type=None,
                provenance=ResolutionProvenance.DIRECT_PROVIDER,
                resolution_chain=(),
            )

        if self._wrappers.find(parsed.hostname) is None:
            # Unknown non-wrapper host: no DNS.
            raise_resolution_error(ResolutionErrorKind.WRAPPER_UNSUPPORTED)

        return await self._resolve_wrapper_chain(raw_url)

    async def _resolve_wrapper_chain(self, raw_url: str) -> ResolutionResult:
        current_raw = raw_url
        chain: list[ResolutionHop] = []
        seen: set[str] = set()
        terminal_wrapper_type: str | None = None
        max_depth = self._settings.wrapper_resolution_max_depth

        while True:
            if len(chain) >= max_depth:
                raise_resolution_error(ResolutionErrorKind.RESOLUTION_LIMIT_EXCEEDED)

            try:
                wrapper = await self._safety.validate(current_raw)
            except ResolutionError:
                raise
            except URLValidationError as exc:
                self._map_url_error(exc)

            fingerprint = internal_url_fingerprint(
                scheme=wrapper.scheme,
                hostname=wrapper.hostname,
                port=wrapper.port,
                path=wrapper.path,
                query=wrapper.query,
            )
            if fingerprint in seen:
                raise_resolution_error(ResolutionErrorKind.RESOLUTION_LOOP)
            seen.add(fingerprint)

            registration = self._wrappers.match(wrapper)
            if registration is None:
                # Host may be owned by a wrapper registration whose matches()
                # rejected the URL shape — that is unsupported wrapper input,
                # not an unsupported terminal provider.
                if self._wrappers.find(wrapper.hostname) is not None:
                    raise_resolution_error(
                        ResolutionErrorKind.WRAPPER_UNSUPPORTED,
                        internal_reason="WRAPPER_SHAPE_MISMATCH",
                    )
                raise_resolution_error(
                    ResolutionErrorKind.RESOLVED_PROVIDER_UNSUPPORTED
                )

            # Host ownership + audit IDs come only from the registry snapshot.
            terminal_wrapper_type = registration.wrapper_type
            documents = self._scoped_documents(registration.exact_hostnames)
            try:
                outcome = await registration.resolver.resolve(
                    wrapper, documents=documents
                )
            except ResolutionError as exc:
                # Rebuild so resolver-controlled message/reason cannot leak.
                raise normalize_resolver_error(exc) from None
            except URLValidationError as exc:
                self._map_url_error(exc)
            except Exception:
                raise_resolution_error(
                    ResolutionErrorKind.WRAPPER_UNRESOLVED,
                    internal_reason="RESOLVER_EXCEPTION",
                )

            candidate = (outcome.candidate_url or "").strip()
            if not candidate:
                raise_resolution_error(ResolutionErrorKind.WRAPPER_UNRESOLVED)

            try:
                candidate_parsed = self._safety.parse_for_classification(candidate)
            except ResolutionError:
                raise

            if candidate_parsed.scheme != "https":
                raise_resolution_error(
                    ResolutionErrorKind.UNSAFE_RESOLUTION_TARGET,
                    internal_reason="WRAPPER_HTTP_CANDIDATE",
                )

            path_lower = candidate_parsed.path.lower()
            if path_lower.endswith((".m3u8", ".mpd")):
                raise_resolution_error(
                    ResolutionErrorKind.RESOLVED_PROVIDER_UNSUPPORTED,
                    internal_reason="MEDIA_MANIFEST_URL",
                )

            # Classify candidate: provider wins; else another wrapper; else fail.
            next_provider = self._providers.find(candidate_parsed.hostname)
            try:
                metadata = freeze_resolution_metadata(outcome.metadata)
                resolver_id = require_audit_token(
                    registration.resolver_id, field_name="resolver_id"
                )
                wrapper_type = require_audit_token(
                    registration.wrapper_type, field_name="wrapper_type"
                )
                strategy = require_audit_token(outcome.strategy, field_name="strategy")
            except ValueError:
                raise_resolution_error(
                    ResolutionErrorKind.INTERNAL_ERROR,
                    internal_reason="UNSAFE_AUDIT_TOKEN",
                )
            hop = ResolutionHop(
                resolver_id=resolver_id,
                wrapper_type=wrapper_type,
                source_canonical=wrapper.canonical_without_query,
                target_canonical=candidate_parsed.canonical_without_query,
                strategy=strategy,
                metadata=metadata,
            )
            chain.append(hop)

            if next_provider is not None:
                try:
                    validated = await self._validator.validate(candidate)
                except URLValidationError as exc:
                    self._map_url_error(exc)
                validated = self._with_stable_provider_identity(validated)
                # Terminal hop must agree with the public /video canonical.
                if chain:
                    prev = chain[-1]
                    chain[-1] = ResolutionHop(
                        resolver_id=prev.resolver_id,
                        wrapper_type=prev.wrapper_type,
                        source_canonical=prev.source_canonical,
                        target_canonical=validated.url.canonical,
                        strategy=prev.strategy,
                        metadata=prev.metadata,
                    )
                return ResolutionResult(
                    submitted_url=raw_url,
                    validated=validated,
                    provider_id=validated.provider_id,
                    provider_display_name=validated.provider_display_name,
                    canonical_provider_url=validated.url.canonical,
                    wrapper_type=terminal_wrapper_type,
                    provenance=ResolutionProvenance.WRAPPER_RESOLVED,
                    resolution_chain=tuple(chain),
                )

            if self._wrappers.find(candidate_parsed.hostname) is not None:
                # Nested wrapper: detect loops on the candidate before continuing.
                cand_fp = internal_url_fingerprint(
                    scheme=candidate_parsed.scheme,
                    hostname=candidate_parsed.hostname,
                    port=candidate_parsed.port,
                    path=candidate_parsed.path,
                    query=candidate_parsed.query,
                )
                if cand_fp in seen:
                    raise_resolution_error(ResolutionErrorKind.RESOLUTION_LOOP)
                current_raw = candidate
                continue

            raise_resolution_error(ResolutionErrorKind.RESOLVED_PROVIDER_UNSUPPORTED)

    def _with_stable_provider_identity(
        self,
        validated: URLValidationResult,
    ) -> URLValidationResult:
        """Rewrite stable provider path aliases into a coherent trusted snapshot.

        VK ``/clip…``, Rutube ``/shorts/…``, OK ``/videoembed/…`` (plus
        moviePlayer), and Dzen ``/shorts/…`` / ``/media/…`` become their
        ``/video…`` canonical forms. Non-stable paths keep the prior snapshot.
        No extra DNS or HTTP I/O.
        """
        if validated.provider_id not in {
            ProviderID.VK.value,
            ProviderID.RUTUBE.value,
            ProviderID.OK.value,
            ProviderID.DZEN.value,
        }:
            return validated
        provider = self._providers.find(validated.url.hostname)
        if provider is None:
            return validated
        try:
            identity = parse_stable_provider_identity(
                provider_id=validated.provider_id,
                hostname=validated.url.hostname,
                path=validated.url.path,
                allowed_hostnames=provider.exact_hostnames,
            )
        except ProviderIdentityError:
            return validated

        canonical = build_public_canonical_url(
            scheme=validated.url.scheme,
            hostname=identity.hostname,
            port=validated.url.port,
            canonical_path=identity.canonical_path,
        )
        if (
            validated.url.path == identity.canonical_path
            and validated.url.hostname == identity.hostname
            and validated.url.canonical == canonical
        ):
            return validated

        rebuilt_url = NormalizedMediaURL(
            original_scheme=validated.url.original_scheme,
            scheme=validated.url.scheme,
            hostname=identity.hostname,
            port=validated.url.port,
            path=identity.canonical_path,
            query=validated.url.query,
            provider_id=validated.url.provider_id,
            canonical=canonical,
        )
        return URLValidationResult(
            provider_id=validated.provider_id,
            provider_display_name=validated.provider_display_name,
            url=rebuilt_url,
            head_fallback_statuses=validated.head_fallback_statuses,
        )

    @staticmethod
    def _map_url_error(exc: URLValidationError) -> None:
        if exc.code in {
            "BLOCKED_DESTINATION",
            "INVALID_URL",
            "UNSUPPORTED_SCHEME",
            "INVALID_REDIRECT",
            "REDIRECT_LIMIT_EXCEEDED",
            "UNSUPPORTED_CONTENT_TYPE",
            "RESPONSE_TOO_LARGE",
            "SOURCE_UNAVAILABLE",
            "SOURCE_TIMEOUT",
        }:
            raise_resolution_error(
                ResolutionErrorKind.UNSAFE_RESOLUTION_TARGET,
                internal_reason=exc.code,
            )
        if exc.code == "UNSUPPORTED_PROVIDER":
            raise_resolution_error(
                ResolutionErrorKind.RESOLVED_PROVIDER_UNSUPPORTED,
                internal_reason=exc.code,
            )
        raise_resolution_error(
            ResolutionErrorKind.UNSAFE_RESOLUTION_TARGET,
            internal_reason=exc.code,
        )
