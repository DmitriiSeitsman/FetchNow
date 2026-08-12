"""API-facing media-job orchestration service."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from fetchnow.core.config import Settings
from fetchnow.jobs.credentials import (
    hash_access_token,
    hash_request_fingerprint,
    tokens_match,
)
from fetchnow.jobs.errors import JobErrorCode, raise_job_error
from fetchnow.jobs.metadata_codec import media_metadata_from_jsonable
from fetchnow.jobs.models import MediaJob
from fetchnow.jobs.repository import MediaJobRepository
from fetchnow.jobs.states import MediaJobState
from fetchnow.media_inspection.errors import InspectionError
from fetchnow.media_inspection.identity import (
    build_canonical_provider_url,
    parse_provider_media_identity,
)
from fetchnow.media_inspection.models import MediaMetadata
from fetchnow.media_inspection.registry import (
    DEFAULT_YTDLP_EXTRACTOR_KEYS,
    InspectionExtractorRegistry,
)
from fetchnow.resolution.errors import (
    ResolutionErrorKind,
    raise_resolution_error,
)
from fetchnow.resolution.service import ResolutionService
from fetchnow.url.errors import URLValidationError
from fetchnow.url.parse import parse_and_normalize
from fetchnow.url.providers import ProviderRegistry


@dataclass(frozen=True, slots=True, repr=False)
class JobView:
    """Public-safe job snapshot for API responses (no access token)."""

    id: uuid.UUID
    public_state: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    completed_at: datetime | None
    result: MediaMetadata | None
    error_code: str | None
    created: bool = False

    def __repr__(self) -> str:
        return (
            f"JobView(id={self.id!r}, public_state={self.public_state!r}, "
            f"created={self.created!r}, error_code={self.error_code!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class JobCreateView:
    """Create response wrapper that never carries the bearer credential."""

    job: JobView


def _provider_inspectable(
    *,
    provider_id: str,
    hostname: str,
    inspection_registry: InspectionExtractorRegistry | None,
    provider_registry: ProviderRegistry | None,
) -> frozenset[str]:
    """Return allowed hostnames when provider has inspection bindings."""
    pid = provider_id.strip().lower()
    if inspection_registry is not None:
        binding = inspection_registry.resolve(provider_id=pid, hostname=hostname)
        return binding.exact_hostnames
    if pid not in DEFAULT_YTDLP_EXTRACTOR_KEYS:
        raise_resolution_error(
            ResolutionErrorKind.RESOLVED_PROVIDER_UNSUPPORTED,
            internal_reason="NO_INSPECTION_BINDING",
        )
    if provider_registry is None:
        raise_resolution_error(
            ResolutionErrorKind.RESOLVED_PROVIDER_UNSUPPORTED,
            internal_reason="NO_PROVIDER_REGISTRY",
        )
    descriptor = provider_registry.find(hostname)
    if descriptor is None or descriptor.id != pid or not descriptor.enabled:
        raise_resolution_error(
            ResolutionErrorKind.RESOLVED_PROVIDER_UNSUPPORTED,
            internal_reason="PROVIDER_HOST_MISMATCH",
        )
    return descriptor.exact_hostnames


class MediaJobService:
    """Create and read durable media-inspection jobs for the public API."""

    __slots__ = ("_settings",)

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def create_job(
        self,
        *,
        raw_url: str,
        access_token: str,
        resolution_service: ResolutionService,
        session: AsyncSession,
        inspection_registry: InspectionExtractorRegistry | None = None,
        provider_registry: ProviderRegistry | None = None,
    ) -> JobCreateView:
        """Recover or create a job without reflecting the bearer credential."""
        if not self._settings.media_jobs_enabled:
            raise_job_error(
                JobErrorCode.CAPACITY_UNAVAILABLE,
                internal_reason="MEDIA_JOBS_DISABLED",
            )

        credential_hash = hash_access_token(access_token)
        try:
            parsed_request = parse_and_normalize(
                raw_url,
                allowed_schemes=set(self._settings.url_allowed_schemes),
                allowed_ports=set(self._settings.url_allowed_ports),
                max_length=self._settings.url_max_length,
            )
            normalized_request = parsed_request.canonical_without_query
            if parsed_request.query:
                normalized_request = f"{normalized_request}?{parsed_request.query}"
        except URLValidationError:
            # ResolutionService remains the owner of public URL error mapping.
            normalized_request = raw_url.strip()
        fingerprint = hash_request_fingerprint(
            access_token=access_token,
            normalized_request=normalized_request,
        )
        repo = MediaJobRepository(session)
        existing = await repo.get_by_credential_hash(credential_hash)
        if existing is not None:
            if not tokens_match(existing.request_fingerprint, fingerprint):
                raise_job_error(
                    JobErrorCode.IDEMPOTENCY_CONFLICT,
                    internal_reason="FINGERPRINT_MISMATCH",
                )
            return JobCreateView(
                job=self._to_view(existing, created=False, include_result=True)
            )

        resolution = await resolution_service.resolve(raw_url)
        url = resolution.validated.url
        provider_id = resolution.provider_id.strip().lower()

        try:
            allowed_hosts = _provider_inspectable(
                provider_id=provider_id,
                hostname=url.hostname,
                inspection_registry=inspection_registry,
                provider_registry=provider_registry,
            )
        except InspectionError:
            raise_resolution_error(
                ResolutionErrorKind.RESOLVED_PROVIDER_UNSUPPORTED,
                internal_reason="INSPECTION_UNSUPPORTED",
            )

        try:
            identity = parse_provider_media_identity(
                provider_id=provider_id,
                hostname=url.hostname,
                path=url.path or "/",
                allowed_hostnames=allowed_hosts,
            )
        except InspectionError:
            raise_resolution_error(
                ResolutionErrorKind.RESOLVED_PROVIDER_UNSUPPORTED,
                internal_reason="IDENTITY_PARSE_FAILED",
            )
        # Persist tool-facing canonical (https, no port/query) matching PR4.
        canonical = build_canonical_provider_url(
            hostname=identity.hostname,
            canonical_path=identity.canonical_path,
        )
        if "?" in canonical or "#" in canonical:
            raise_job_error(
                JobErrorCode.INTERNAL_ERROR,
                internal_reason="CANONICAL_QUERY_OR_FRAGMENT",
            )

        now = await repo.database_now()
        job, created = await repo.enqueue_or_get(
            credential_hash=credential_hash,
            request_fingerprint=fingerprint,
            provider_id=provider_id,
            canonical_provider_url=canonical,
            media_id=identity.media_id,
            hostname=identity.hostname,
            path=identity.canonical_path,
            scheme="https",
            port=None,
            now=now,
            ttl_seconds=self._settings.media_job_absolute_ttl_seconds,
            max_attempts=self._settings.media_job_max_attempts,
        )
        await session.flush()

        view = self._to_view(job, created=created, include_result=True)
        return JobCreateView(job=view)

    async def get_job(
        self,
        *,
        job_id: uuid.UUID,
        access_token: str,
        session: AsyncSession,
    ) -> JobView:
        """Load job by id; wrong/unknown credential → indistinguishable 404."""
        credential_hash = hash_access_token(access_token)
        repo = MediaJobRepository(session)
        job = await repo.get_by_id(job_id)
        if job is None or not tokens_match(job.credential_hash, credential_hash):
            raise_job_error(
                JobErrorCode.JOB_NOT_FOUND,
                internal_reason="MISSING_OR_UNAUTHORIZED",
            )

        now = await repo.database_now()
        if (
            job.public_state == MediaJobState.EXPIRED.value
            or job.expires_at <= now
        ):
            raise_job_error(
                JobErrorCode.JOB_EXPIRED,
                internal_reason="JOB_TTL",
            )
        return self._to_view(job, created=False, include_result=True)

    def _to_view(
        self,
        job: MediaJob,
        *,
        created: bool,
        include_result: bool,
    ) -> JobView:
        try:
            state = MediaJobState(job.public_state)
        except ValueError:
            raise_job_error(
                JobErrorCode.INTERNAL_ERROR,
                internal_reason="UNKNOWN_JOB_STATE",
            )
        result: MediaMetadata | None = None
        if (
            include_result
            and state is MediaJobState.INSPECTED
            and isinstance(job.result_metadata, dict)
        ):
            result = media_metadata_from_jsonable(
                job.result_metadata,
                max_bytes=self._settings.media_job_result_max_bytes,
            )
        error_code: str | None = None
        if state is MediaJobState.FAILED:
            try:
                error_code = JobErrorCode(str(job.public_error_code)).value
            except ValueError:
                error_code = JobErrorCode.INTERNAL_ERROR.value
        return JobView(
            id=job.id,
            public_state=state.value,
            created_at=job.created_at,
            updated_at=job.updated_at,
            expires_at=job.expires_at,
            completed_at=job.completed_at,
            result=result,
            error_code=error_code,
            created=created,
        )

    def job_to_public_dict(self, view: JobView) -> dict[str, Any]:
        """Serialize a JobView to camelCase API fields (no secrets)."""
        payload: dict[str, Any] = {
            "id": str(view.id),
            "state": view.public_state,
            "createdAt": view.created_at.isoformat(),
            "updatedAt": view.updated_at.isoformat(),
            "expiresAt": view.expires_at.isoformat(),
            "completedAt": (
                view.completed_at.isoformat() if view.completed_at else None
            ),
            "errorCode": view.error_code,
        }
        if view.result is not None:
            from fetchnow.jobs.metadata_codec import media_metadata_to_jsonable

            payload["result"] = media_metadata_to_jsonable(
                view.result,
                max_bytes=self._settings.media_job_result_max_bytes,
            )
        else:
            payload["result"] = None
        return payload
