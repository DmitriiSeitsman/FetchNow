"""Anonymous Robokassa test-order API and public signed callback."""

from __future__ import annotations

import re
import uuid
from urllib.parse import parse_qsl

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fetchnow.core.errors import error_envelope
from fetchnow.payments.audit import payment_event
from fetchnow.payments.callback_diagnostic import (
    CallbackDiagnostic,
)
from fetchnow.payments.callback_diagnostic import (
    emit_once as emit_callback_diagnostic_once,
)
from fetchnow.payments.errors import (
    IdempotencyConflictError,
    InvalidCallbackError,
    InvalidIdempotencyKeyError,
    PaymentOrderNotFoundError,
    PaymentsDisabledError,
    UnknownProductError,
)
from fetchnow.payments.service import PaymentService
from fetchnow.quota.errors import AnonymousIdentityRequiredError
from fetchnow.quota.service import QuotaService

router = APIRouter(prefix="/payments", tags=["payments"])
_NO_STORE = {"Cache-Control": "no-store"}
_CALLBACK_BODY_LIMIT = 16_384
_INV_ID = re.compile(r"^[1-9][0-9]{0,18}$")
_CALLBACK_FIELDS = frozenset(
    {
        "OutSum",
        "InvId",
        "SignatureValue",
        "Fee",
        "EMail",
        "PaymentMethod",
        "IncCurrLabel",
    }
)


class CreatePaymentOrderBody(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    product_code: str = Field(alias="productCode", min_length=1, max_length=32)


def _session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    value = getattr(request.app.state, "session_factory", None)
    if value is None:
        raise RuntimeError("session_factory is not configured")
    return value  # type: ignore[no-any-return]


def _payment_service(request: Request) -> PaymentService:
    value = getattr(request.app.state, "payment_service", None)
    if value is None:
        value = PaymentService(request.app.state.settings)
        request.app.state.payment_service = value
    return value


async def _callback_form(
    request: Request, diagnostic: CallbackDiagnostic
) -> dict[str, str]:
    diagnostic.parser_entered = True
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
    diagnostic.content_type_category = (
        "form_urlencoded"
        if content_type == "application/x-www-form-urlencoded"
        else ("missing" if not content_type else "other")
    )
    if content_type != "application/x-www-form-urlencoded":
        diagnostic.rejection_stage = "content_type"
        diagnostic.rejection_category = "content_type_invalid"
        raise InvalidCallbackError()
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _CALLBACK_BODY_LIMIT:
            diagnostic.rejection_stage = "form_decode"
            diagnostic.rejection_category = "form_invalid"
            raise InvalidCallbackError()
    try:
        text = bytes(body).decode("utf-8", errors="strict")
        pairs = parse_qsl(
            text,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=16,
        )
    except (UnicodeError, ValueError) as exc:
        diagnostic.rejection_stage = "form_decode"
        diagnostic.rejection_category = "form_invalid"
        raise InvalidCallbackError() from exc
    diagnostic.record_names([key for key, _value in pairs])
    values: dict[str, str] = {}
    for key, value in pairs:
        if key in values:
            diagnostic.rejection_stage = "field_schema"
            diagnostic.rejection_category = "duplicate_field"
            raise InvalidCallbackError()
        if key not in _CALLBACK_FIELDS or key.startswith("Shp_"):
            diagnostic.unexpected_field_names = tuple(
                sorted(
                    name
                    for name in diagnostic.field_names
                    if name not in _CALLBACK_FIELDS or name.startswith("Shp_")
                )
            )
            diagnostic.rejection_stage = "field_schema"
            diagnostic.rejection_category = "unexpected_field"
            raise InvalidCallbackError()
        if any(char in value for char in ("\r", "\n", "\0")):
            raise InvalidCallbackError()
        values[key] = value
    if not {"OutSum", "InvId", "SignatureValue"}.issubset(values):
        diagnostic.rejection_stage = "field_schema"
        diagnostic.rejection_category = "required_field_missing"
        raise InvalidCallbackError()
    diagnostic.parser_accepted = True
    return values


def _parse_inv_id(raw: str, diagnostic: CallbackDiagnostic) -> int:
    diagnostic.inv_id_parsing_attempted = True
    if _INV_ID.fullmatch(raw) is None:
        diagnostic.rejection_stage = "inv_id"
        diagnostic.rejection_category = "inv_id_invalid"
        raise InvalidCallbackError()
    value = int(raw)
    if value > 9_223_372_036_854_775_807:
        diagnostic.rejection_stage = "inv_id"
        diagnostic.rejection_category = "inv_id_invalid"
        raise InvalidCallbackError()
    diagnostic.inv_id_valid = True
    return value


@router.post("/orders", response_model=None)
async def create_payment_order(
    request: Request,
    body: CreatePaymentOrderBody,
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    service = _payment_service(request)
    try:
        service.ensure_checkout_available()
        async with _session_factory(request)() as session:
            identity = await QuotaService(request.app.state.settings).require_identity(
                cookie_header=request.headers.get("cookie"), session=session
            )
            created = await service.create_order(
                anonymous_client_id=identity.id,
                product_code=body.product_code,
                idempotency_key=idempotency_key,
                session=session,
            )
            await session.commit()
    except PaymentsDisabledError:
        return JSONResponse(
            status_code=503,
            headers=_NO_STORE,
            content=error_envelope(
                code="PAYMENTS_DISABLED",
                message="Payments are not active.",
                request_id=request_id,
            ),
        )
    except AnonymousIdentityRequiredError:
        return JSONResponse(
            status_code=428,
            headers=_NO_STORE,
            content=error_envelope(
                code="PAYMENT_IDENTITY_REQUIRED",
                message="A valid anonymous client identity is required.",
                request_id=request_id,
            ),
        )
    except UnknownProductError:
        return JSONResponse(
            status_code=422,
            headers=_NO_STORE,
            content=error_envelope(
                code="PAYMENT_PRODUCT_UNKNOWN",
                message="The requested payment product is unavailable.",
                request_id=request_id,
            ),
        )
    except InvalidIdempotencyKeyError:
        return JSONResponse(
            status_code=400,
            headers=_NO_STORE,
            content=error_envelope(
                code="PAYMENT_IDEMPOTENCY_KEY_INVALID",
                message="The idempotency key is invalid.",
                request_id=request_id,
            ),
        )
    except IdempotencyConflictError:
        return JSONResponse(
            status_code=409,
            headers=_NO_STORE,
            content=error_envelope(
                code="PAYMENT_IDEMPOTENCY_CONFLICT",
                message="The idempotency key conflicts with an existing request.",
                request_id=request_id,
            ),
        )

    payment_event(
        "payment_order_pending",
        public_id=created.order.public_id,
        provider_invoice_id=created.order.provider_invoice_id,
        previous_status="created" if created.created else "pending",
        new_status="pending",
        outcome="created" if created.created else "idempotent",
    )
    return JSONResponse(
        status_code=201 if created.created else 200,
        headers=_NO_STORE,
        content=service.creation_dict(created),
    )


@router.get("/config", response_model=None)
async def get_payment_config(request: Request) -> JSONResponse:
    """Expose only the derived, non-secret temporary checkout availability."""
    return JSONResponse(
        status_code=200,
        headers=_NO_STORE,
        content={
            "testCheckoutAvailable": _payment_service(
                request
            ).test_checkout_available
        },
    )


@router.get("/orders/{public_id}", response_model=None)
async def get_payment_order(request: Request, public_id: str) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    try:
        _payment_service(request).ensure_available()
        parsed_id = uuid.UUID(public_id)
        if str(parsed_id) != public_id:
            raise ValueError
        async with _session_factory(request)() as session:
            identity = await QuotaService(request.app.state.settings).require_identity(
                cookie_header=request.headers.get("cookie"), session=session
            )
            row = await _payment_service(request).get_order(
                public_id=parsed_id,
                anonymous_client_id=identity.id,
                session=session,
            )
    except PaymentsDisabledError:
        return JSONResponse(
            status_code=503,
            headers=_NO_STORE,
            content=error_envelope(
                code="PAYMENTS_DISABLED",
                message="Payments are not active.",
                request_id=request_id,
            ),
        )
    except AnonymousIdentityRequiredError:
        return JSONResponse(
            status_code=428,
            headers=_NO_STORE,
            content=error_envelope(
                code="PAYMENT_IDENTITY_REQUIRED",
                message="A valid anonymous client identity is required.",
                request_id=request_id,
            ),
        )
    except (ValueError, PaymentOrderNotFoundError):
        return JSONResponse(
            status_code=404,
            headers=_NO_STORE,
            content=error_envelope(
                code="PAYMENT_ORDER_NOT_FOUND",
                message="The payment order was not found.",
                request_id=request_id,
            ),
        )
    return JSONResponse(
        status_code=200,
        headers=_NO_STORE,
        content=_payment_service(request).public_dict(row),
    )


@router.post("/robokassa/result", response_model=None)
async def robokassa_result(request: Request) -> PlainTextResponse:
    diagnostic = CallbackDiagnostic()
    try:
        values = await _callback_form(request, diagnostic)
        inv_id = _parse_inv_id(values["InvId"], diagnostic)
        async with _session_factory(request)() as session:
            result = await _payment_service(request).accept_callback(
                raw_out_sum=values["OutSum"],
                inv_id=inv_id,
                supplied_signature=values["SignatureValue"],
                session=session,
                diagnostic=diagnostic,
            )
            await session.commit()
    except (InvalidCallbackError, PaymentsDisabledError) as exc:
        if isinstance(exc, PaymentsDisabledError):
            diagnostic.rejection_stage = "snapshot"
            diagnostic.rejection_category = "payments_disabled"
        diagnostic.http_status = 400
        diagnostic.response_outcome = "ERROR"
        emit_callback_diagnostic_once(diagnostic)
        payment_event(
            "payment_callback_rejected",
            outcome="rejected",
        )
        return PlainTextResponse("ERROR", status_code=400, headers=_NO_STORE)
    except Exception:
        diagnostic.rejection_stage = "persistence"
        diagnostic.rejection_category = "internal_error"
        diagnostic.http_status = 500
        diagnostic.response_outcome = "ERROR"
        emit_callback_diagnostic_once(diagnostic)
        payment_event("payment_callback_failed", outcome="internal_error")
        return PlainTextResponse("ERROR", status_code=500, headers=_NO_STORE)

    diagnostic.http_status = 200
    diagnostic.response_outcome = "OK"
    emit_callback_diagnostic_once(diagnostic)
    payment_event(
        "payment_callback_accepted",
        provider_invoice_id=result.inv_id,
        previous_status="pending" if result.transitioned else "paid",
        new_status="paid",
        outcome="accepted" if result.transitioned else "duplicate",
        signature_valid=True,
        amount_match=True,
    )
    return PlainTextResponse(f"OK{result.inv_id}", status_code=200, headers=_NO_STORE)


def _browser_return(message: str) -> PlainTextResponse:
    # Redirect parameters are deliberately ignored. Browser navigation is not
    # a payment mutation or a source of payment truth.
    return PlainTextResponse(message, status_code=200, headers=_NO_STORE)


@router.get("/robokassa/success", response_model=None)
@router.post("/robokassa/success", response_model=None)
async def robokassa_success() -> PlainTextResponse:
    return _browser_return(
        "Payment return received. Check the server-side order status."
    )


@router.get("/robokassa/fail", response_model=None)
@router.post("/robokassa/fail", response_model=None)
async def robokassa_fail() -> PlainTextResponse:
    return _browser_return("Payment was not completed. The order was not changed.")
