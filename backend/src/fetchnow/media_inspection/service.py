"""Orchestrate validated resolution results into bounded MediaMetadata."""

from __future__ import annotations

from fetchnow.core.config import Settings
from fetchnow.media_inspection.errors import (
    InspectionError,
    InspectionErrorKind,
    raise_inspection_error,
)
from fetchnow.media_inspection.identity import (
    build_canonical_provider_url,
    parse_provider_media_identity,
)
from fetchnow.media_inspection.models import ExtractedMediaDraft, MediaMetadata
from fetchnow.media_inspection.normalize import project_metadata
from fetchnow.media_inspection.protocols import InspectionTarget
from fetchnow.media_inspection.registry import InspectionExtractorRegistry
from fetchnow.resolution.models import ResolutionResult
from fetchnow.url.errors import URLValidationError
from fetchnow.url.parse import parse_and_normalize


class MediaInspectionService:
    """Metadata inspection entrypoint — accepts ResolutionResult only."""

    def __init__(
        self,
        *,
        settings: Settings,
        registry: InspectionExtractorRegistry,
    ) -> None:
        self._settings = settings
        self._registry = registry

    async def inspect(self, resolution: ResolutionResult) -> MediaMetadata:
        """Inspect media metadata for a previously resolved provider URL."""
        draft = await self.inspect_draft(resolution)
        target_canonical = draft.canonical_provider_url
        return project_metadata(
            draft,
            self._settings,
            trusted_canonical_url=target_canonical,
            trusted_media_id=draft.media_id,
            trusted_provider_id=draft.provider_id,
        )

    async def inspect_draft(self, resolution: ResolutionResult) -> ExtractedMediaDraft:
        """Run the same binding/validation as inspect, returning the draft.

        Used by download execution to build an ephemeral
        formatOptionId → provider_format_token map without projecting public
        metadata. Never persists the draft or provider tokens.
        """
        if not isinstance(resolution, ResolutionResult):
            raise_inspection_error(
                InspectionErrorKind.INTERNAL_INSPECTION_ERROR,
                internal_reason="INVALID_RESOLUTION_TYPE",
            )

        try:
            target = self._bind_target(resolution)
        except InspectionError:
            raise
        except Exception:
            # Malformed ports / URL edge cases → catalog error, no raw URL leak.
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_POLICY_REJECTED,
                internal_reason="RESOLUTION_SNAPSHOT_INVALID",
            )

        binding = self._registry.resolve(
            provider_id=target.provider_id, hostname=target.hostname
        )
        draft = await binding.extractor.extract(target)

        if draft.provider_id != target.provider_id:
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH,
                internal_reason="DRAFT_PROVIDER_MISMATCH",
            )
        if draft.media_id != target.media_id:
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH,
                internal_reason="DRAFT_MEDIA_MISMATCH",
            )
        if draft.canonical_provider_url != target.canonical_provider_url:
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH,
                internal_reason="DRAFT_CANONICAL_MISMATCH",
            )
        if draft.extractor_key.lower() not in binding.allowed_extractor_keys:
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH,
                internal_reason="DRAFT_EXTRACTOR_MISMATCH",
            )
        return draft

    def _bind_target(self, resolution: ResolutionResult) -> InspectionTarget:
        """Derive the tool target from a coherent validated URL snapshot only."""
        validated = resolution.validated
        url = validated.url
        allowed_schemes = {s.lower() for s in self._settings.url_allowed_schemes}
        allowed_ports = set(self._settings.url_allowed_ports)

        provider_id = resolution.provider_id.strip().lower()
        validated_provider = validated.provider_id.strip().lower()
        url_provider = url.provider_id.strip().lower()
        if provider_id != validated_provider or provider_id != url_provider:
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH,
                internal_reason="PROVIDER_FIELD_MISMATCH",
            )

        public_canonical = resolution.canonical_provider_url
        structured_canonical = url.canonical
        # Reject query/fragment on public or structured canonical — do not strip
        # and accept. Exact equality is required.
        if (
            not isinstance(public_canonical, str)
            or not isinstance(structured_canonical, str)
            or "?" in public_canonical
            or "#" in public_canonical
            or "?" in structured_canonical
            or "#" in structured_canonical
        ):
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_POLICY_REJECTED,
                internal_reason="CANONICAL_QUERY_OR_FRAGMENT",
            )
        if public_canonical != structured_canonical:
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH,
                internal_reason="CANONICAL_FIELD_MISMATCH",
            )

        # validated.url.query is untrusted internal data — never used below.
        _ = url.query

        try:
            reparsed = parse_and_normalize(
                structured_canonical,
                allowed_schemes=allowed_schemes,
                allowed_ports=allowed_ports,
                max_length=self._settings.url_max_length,
            )
        except URLValidationError:
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_POLICY_REJECTED,
                internal_reason="CANONICAL_REPARSE_FAILED",
            )

        if reparsed.scheme != url.scheme:
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH,
                internal_reason="SCHEME_FIELD_MISMATCH",
            )
        if reparsed.hostname != url.hostname:
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH,
                internal_reason="HOSTNAME_FIELD_MISMATCH",
            )
        if reparsed.port != url.port:
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH,
                internal_reason="PORT_FIELD_MISMATCH",
            )
        if reparsed.path != url.path:
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH,
                internal_reason="PATH_FIELD_MISMATCH",
            )
        if reparsed.canonical_without_query != url.canonical:
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH,
                internal_reason="CANONICAL_REPARSE_MISMATCH",
            )

        original_scheme = (url.original_scheme or "").strip().lower()
        if original_scheme not in allowed_schemes:
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_POLICY_REJECTED,
                internal_reason="ORIGINAL_SCHEME_INVALID",
            )
        # Coherence with normalization contract: original_scheme must match the
        # structured scheme (parse_and_normalize stores both as lowercase scheme).
        if original_scheme != url.scheme or original_scheme != reparsed.original_scheme:
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH,
                internal_reason="ORIGINAL_SCHEME_MISMATCH",
            )

        if url.scheme == "http" and "http" not in allowed_schemes:
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_POLICY_REJECTED,
                internal_reason="SCHEME_NOT_ALLOWED",
            )
        if url.scheme not in allowed_schemes:
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_POLICY_REJECTED,
                internal_reason="SCHEME_NOT_ALLOWED",
            )
        if url.port is not None and url.port not in allowed_ports:
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_POLICY_REJECTED,
                internal_reason="NONDEFAULT_PORT",
            )

        hostname = url.hostname
        binding = self._registry.resolve(provider_id=provider_id, hostname=hostname)
        identity = parse_provider_media_identity(
            provider_id=provider_id,
            hostname=hostname,
            path=url.path or "/",
            allowed_hostnames=binding.exact_hostnames,
        )
        # Tool URL / public canonical derived only from coherent snapshot identity.
        # Never incorporate validated.url.query.
        tool_canonical = build_canonical_provider_url(
            hostname=identity.hostname,
            canonical_path=identity.canonical_path,
        )
        return InspectionTarget(
            provider_id=provider_id,
            hostname=identity.hostname,
            media_id=identity.media_id,
            canonical_provider_url=tool_canonical,
            tool_url=tool_canonical,
            allowed_hostnames=binding.exact_hostnames,
        )
