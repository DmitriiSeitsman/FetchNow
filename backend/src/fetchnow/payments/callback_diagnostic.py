"""One-shot, value-free diagnostics for the Robokassa ResultURL boundary."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger("fetchnow.payments.callback_diagnostic")

EXPECTED_CALLBACK_FIELDS = frozenset({"OutSum", "InvId", "SignatureValue"})
_SAFE_FIELD_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_emitted = False


@dataclass(slots=True)
class CallbackDiagnostic:
    http_method: str = "POST"
    content_type_category: str = "missing"
    field_names: tuple[str, ...] = field(default_factory=tuple)
    expected_fields_present: bool = False
    unexpected_field_names: tuple[str, ...] = field(default_factory=tuple)
    parser_entered: bool = False
    parser_accepted: bool = False
    inv_id_parsing_attempted: bool = False
    inv_id_valid: bool = False
    signature_verification_attempted: bool = False
    signature_valid: bool = False
    amount_validation_attempted: bool = False
    amount_valid: bool = False
    order_lookup_succeeded: bool | None = None
    receipt_validation_attempted: bool = False
    rejection_stage: str = "none"
    rejection_category: str = "none"
    http_status: int = 200
    response_outcome: str = "OK"

    def record_names(self, names: list[str]) -> None:
        safe = tuple(
            sorted({name for name in names if _SAFE_FIELD_NAME.fullmatch(name)})
        )
        self.field_names = safe
        present = set(safe)
        self.expected_fields_present = EXPECTED_CALLBACK_FIELDS.issubset(present)


def emit_once(value: CallbackDiagnostic) -> None:
    """Emit at most one sanitized diagnostic event per API process lifetime."""
    global _emitted
    if _emitted:
        return
    _emitted = True
    logger.info(
        "payment_callback_sanitized_diagnostic",
        extra={
            "http_method": value.http_method,
            "http_status": value.http_status,
            "content_type_category": value.content_type_category,
            "callback_field_names": value.field_names,
            "expected_fields_present": value.expected_fields_present,
            "unexpected_callback_field_names": value.unexpected_field_names,
            "parser_entered": value.parser_entered,
            "parser_accepted": value.parser_accepted,
            "inv_id_parsing_attempted": value.inv_id_parsing_attempted,
            "inv_id_valid": value.inv_id_valid,
            "signature_verification_attempted": value.signature_verification_attempted,
            "signature_valid": value.signature_valid,
            "amount_validation_attempted": value.amount_validation_attempted,
            "amount_valid": value.amount_valid,
            "order_lookup_succeeded": value.order_lookup_succeeded,
            "receipt_validation_attempted": value.receipt_validation_attempted,
            "rejection_stage": value.rejection_stage,
            "rejection_category": value.rejection_category,
            "response_outcome": value.response_outcome,
        },
    )
