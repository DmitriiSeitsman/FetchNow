"""Delivery HTTP routes: authenticated artifact content streaming."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fetchnow.core.errors import error_envelope
from fetchnow.delivery.cookie_parse import extract_delivery_grant_token
from fetchnow.delivery.range_parse import (
    ByteRange,
    RangeUnsatisfiable,
    parse_single_byte_range,
)
from fetchnow.delivery.reader import OpenArtifactHandle
from fetchnow.delivery.service import DeliveryService
from fetchnow.delivery.stream import iter_fd_range
from fetchnow.downloads.canonical_uuid import parse_canonical_uuid4
from fetchnow.downloads.errors import DownloadError, DownloadErrorCode
from fetchnow.jobs.credentials import parse_access_token
from fetchnow.jobs.errors import JobError, JobErrorCode

router = APIRouter(prefix="/media", tags=["media-delivery"])
logger = logging.getLogger("fetchnow.delivery.routes")

_NO_STORE = {"Cache-Control": "no-store"}


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise RuntimeError("session_factory is not configured")
    return factory  # type: ignore[no-any-return]


def _delivery_service(request: Request) -> DeliveryService:
    service = getattr(request.app.state, "delivery_service", None)
    if service is None:
        raise RuntimeError("delivery_service is not configured")
    return service  # type: ignore[no-any-return]


def _bearer_token(authorization: str | None) -> str | None:
    if not isinstance(authorization, str):
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def _error_response(exc: DownloadError, request_id: str | None) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        headers=_NO_STORE,
        content=error_envelope(
            code=exc.code.value,
            message=exc.message,
            request_id=request_id,
        ),
    )


def _not_found(request_id: str | None) -> JSONResponse:
    return _error_response(
        DownloadError(DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND),
        request_id,
    )


def _parse_bearer_or_not_found(
    authorization: str | None, request_id: str | None
) -> str | JSONResponse:
    """Return a canonical token or the indistinguishible 404 response.

    Uses the shared credential parser; never logs the token and never opens a
    database session for malformed shapes.
    """
    token = _bearer_token(authorization)
    if token is None:
        return _not_found(request_id)
    try:
        parse_access_token(token)
    except JobError as exc:
        if exc.code is JobErrorCode.INVALID_ACCESS_TOKEN:
            return _not_found(request_id)
        # Unexpected credential-layer failure: still do not leak the token.
        return _error_response(
            DownloadError(
                DownloadErrorCode.INTERNAL_ERROR,
                internal_reason="TOKEN_LAYER",
            ),
            request_id,
        )
    return token


async def _handle_content(
    *,
    download_job_id: uuid.UUID,
    request: Request,
    authorization: str | None,
    head_only: bool,
) -> Response:
    request_id = _request_id(request)
    parsed = _parse_bearer_or_not_found(authorization, request_id)
    if not isinstance(parsed, str):
        return parsed
    token = parsed

    service = _delivery_service(request)
    session_factory = _session_factory(request)
    handle: OpenArtifactHandle | None = None
    permit_held = False
    # Once True, only the streaming body finally may release the permit.
    stream_owns_permit = False

    def _cleanup_prestream() -> None:
        nonlocal handle, permit_held
        if handle is not None:
            handle.close()
            handle = None
        if permit_held and not stream_owns_permit:
            service.semaphore.release()
            permit_held = False

    try:
        await service.semaphore.acquire()
        permit_held = True

        async with session_factory() as session:
            authz = await service.authorize(
                download_job_id=download_job_id,
                access_token=token,
                session=session,
            )
        handle = service.open_artifact(authz)
        artifact = handle.artifact

        selected: ByteRange | None = None
        range_header = request.headers.get("range")
        if service.range_enabled:
            parsed_range = parse_single_byte_range(
                range_header, size=artifact.size_bytes
            )
            if isinstance(parsed_range, RangeUnsatisfiable):
                _cleanup_prestream()
                return Response(
                    status_code=416,
                    headers={
                        "Content-Range": parsed_range.content_range_header(),
                        "Accept-Ranges": "bytes",
                        "Cache-Control": "private, no-store",
                        "Pragma": "no-cache",
                    },
                    content=b"",
                )
            if isinstance(parsed_range, ByteRange):
                selected = parsed_range

        if selected is None:
            start = 0
            length = artifact.size_bytes
            status = 200
        else:
            start = selected.start
            length = selected.length
            status = 206

        disposition = service.content_disposition(authz.job)
        headers = service.security_headers(
            content_type=artifact.content_type,
            content_disposition=disposition,
        )
        headers["Content-Length"] = str(length)
        headers["Accept-Ranges"] = "bytes" if service.range_enabled else "none"
        if selected is not None:
            headers["Content-Range"] = selected.content_range_header()

        if head_only:
            _cleanup_prestream()
            return Response(status_code=status, headers=headers, content=b"")

        # Keep FD/permit under pre-stream ownership until StreamingResponse
        # construction succeeds. body() may be created as a constructor arg;
        # its finally must not release until ownership is transferred.
        stream_handle = handle

        async def body() -> AsyncIterator[bytes]:
            nonlocal permit_held
            try:
                async for chunk in iter_fd_range(
                    stream_handle,
                    start=start,
                    length=length,
                    chunk_bytes=service.chunk_bytes,
                ):
                    yield chunk
            finally:
                if stream_owns_permit and permit_held:
                    service.semaphore.release()
                    permit_held = False

        response = StreamingResponse(
            body(),
            status_code=status,
            headers=headers,
            media_type=artifact.content_type,
        )
        # No await between transfer and return: stream body is sole owner.
        stream_owns_permit = True
        handle = None
        return response
    except DownloadError as exc:
        _cleanup_prestream()
        return _error_response(exc, request_id)
    except Exception:
        _cleanup_prestream()
        logger.exception("media_delivery_failed")
        return JSONResponse(
            status_code=500,
            headers=_NO_STORE,
            content=error_envelope(
                code="INTERNAL_ERROR",
                message="An unexpected error occurred.",
                request_id=request_id,
            ),
        )
    except BaseException:
        # CancelledError and other BaseExceptions: never translate to JSON.
        _cleanup_prestream()
        raise


@router.api_route(
    "/download-jobs/{download_job_id}/content",
    methods=["GET"],
    response_model=None,
)
async def get_download_content(
    download_job_id: uuid.UUID,
    request: Request,
    authorization: str | None = Header(default=None),
) -> Response:
    """Stream a ready private artifact authorized by the parent Bearer token."""
    return await _handle_content(
        download_job_id=download_job_id,
        request=request,
        authorization=authorization,
        head_only=False,
    )


@router.api_route(
    "/download-jobs/{download_job_id}/content",
    methods=["HEAD"],
    response_model=None,
)
async def head_download_content(
    download_job_id: uuid.UUID,
    request: Request,
    authorization: str | None = Header(default=None),
) -> Response:
    """Authenticated HEAD mirroring GET headers without a body."""
    return await _handle_content(
        download_job_id=download_job_id,
        request=request,
        authorization=authorization,
        head_only=True,
    )


async def _handle_browser_grant_content(
    *,
    grant_id_raw: str,
    request: Request,
    head_only: bool,
) -> Response:
    """Native browser content authorized only by the exact-path grant cookie."""
    request_id = _request_id(request)
    # Exact clean path: reject any query before cookie, semaphore, or DB.
    # Never log the query string (may contain attacker-supplied secrets).
    if request.url.query != "":
        logger.info(
            "browser_delivery_failed outcome=%s",
            DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND.value,
        )
        return _not_found(request_id)

    # Parse canonical UUID4 before cookie, semaphore, or database work.
    try:
        grant_id = parse_canonical_uuid4(grant_id_raw)
    except DownloadError as exc:
        logger.info(
            "browser_delivery_failed outcome=%s",
            exc.code.value,
        )
        return _error_response(exc, request_id)

    service = _delivery_service(request)
    session_factory = _session_factory(request)
    handle: OpenArtifactHandle | None = None
    permit_held = False
    stream_owns_permit = False

    def _cleanup_prestream() -> None:
        nonlocal handle, permit_held
        if handle is not None:
            handle.close()
            handle = None
        if permit_held and not stream_owns_permit:
            service.semaphore.release()
            permit_held = False

    try:
        # Cookie parse / token shape before semaphore or bytes.
        raw_token = extract_delivery_grant_token(request.headers.get("cookie"))

        await service.semaphore.acquire()
        permit_held = True

        async with session_factory() as session:
            authz = await service.authorize_browser_grant(
                grant_id=grant_id,
                raw_token=raw_token,
                session=session,
            )
        handle = service.open_artifact(authz)
        artifact = handle.artifact

        selected: ByteRange | None = None
        range_header = request.headers.get("range")
        if service.range_enabled:
            parsed_range = parse_single_byte_range(
                range_header, size=artifact.size_bytes
            )
            if isinstance(parsed_range, RangeUnsatisfiable):
                _cleanup_prestream()
                return Response(
                    status_code=416,
                    headers={
                        "Content-Range": parsed_range.content_range_header(),
                        "Accept-Ranges": "bytes",
                        "Cache-Control": "private, no-store",
                        "Pragma": "no-cache",
                    },
                    content=b"",
                )
            if isinstance(parsed_range, ByteRange):
                selected = parsed_range

        if selected is None:
            start = 0
            length = artifact.size_bytes
            status = 200
        else:
            start = selected.start
            length = selected.length
            status = 206

        disposition = service.content_disposition(authz.job)
        headers = service.security_headers(
            content_type=artifact.content_type,
            content_disposition=disposition,
        )
        headers["Content-Length"] = str(length)
        headers["Accept-Ranges"] = "bytes" if service.range_enabled else "none"
        if selected is not None:
            headers["Content-Range"] = selected.content_range_header()

        if head_only:
            _cleanup_prestream()
            logger.info(
                "browser_delivery_started outcome=head range=%s",
                "partial" if selected is not None else "full",
            )
            return Response(status_code=status, headers=headers, content=b"")

        stream_handle = handle

        async def body() -> AsyncIterator[bytes]:
            nonlocal permit_held
            bytes_sent = 0
            try:
                async for chunk in iter_fd_range(
                    stream_handle,
                    start=start,
                    length=length,
                    chunk_bytes=service.chunk_bytes,
                ):
                    bytes_sent += len(chunk)
                    yield chunk
                logger.info(
                    "browser_delivery_started outcome=ok range=%s bytes=%s",
                    "partial" if selected is not None else "full",
                    bytes_sent,
                )
            except Exception:
                logger.info("browser_delivery_failed outcome=stream_error")
                raise
            finally:
                if stream_owns_permit and permit_held:
                    service.semaphore.release()
                    permit_held = False

        response = StreamingResponse(
            body(),
            status_code=status,
            headers=headers,
            media_type=artifact.content_type,
        )
        stream_owns_permit = True
        handle = None
        return response
    except DownloadError as exc:
        _cleanup_prestream()
        logger.info(
            "browser_delivery_failed outcome=%s",
            exc.code.value,
        )
        return _error_response(exc, request_id)
    except Exception:
        _cleanup_prestream()
        # Never log exception text/repr/SQL params (may contain secrets).
        logger.info("browser_delivery_failed outcome=internal")
        return JSONResponse(
            status_code=500,
            headers=_NO_STORE,
            content=error_envelope(
                code="INTERNAL_ERROR",
                message="An unexpected error occurred.",
                request_id=request_id,
            ),
        )
    except BaseException:
        _cleanup_prestream()
        raise


@router.api_route(
    "/browser-grants/{grant_id}/content",
    methods=["GET"],
    response_model=None,
)
async def get_browser_grant_content(
    grant_id: str,
    request: Request,
) -> Response:
    """Stream a ready artifact authorized by the exact-path grant cookie."""
    return await _handle_browser_grant_content(
        grant_id_raw=grant_id,
        request=request,
        head_only=False,
    )


@router.api_route(
    "/browser-grants/{grant_id}/content",
    methods=["HEAD"],
    response_model=None,
)
async def head_browser_grant_content(
    grant_id: str,
    request: Request,
) -> Response:
    """HEAD mirroring GET headers for a browser grant without a body."""
    return await _handle_browser_grant_content(
        grant_id_raw=grant_id,
        request=request,
        head_only=True,
    )
