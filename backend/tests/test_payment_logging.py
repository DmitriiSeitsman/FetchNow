"""Payment audit allowlist and redaction boundary."""

from __future__ import annotations

import json
import logging
import uuid

from fetchnow.core.logging import JsonFormatter
from fetchnow.payments.audit import payment_event


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.payloads: list[dict[str, object]] = []
        self.setFormatter(JsonFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        self.payloads.append(json.loads(self.format(record)))


def test_payment_event_emits_only_sanitized_allowlisted_fields() -> None:
    logger = logging.getLogger("fetchnow.payments.audit")
    capture = _Capture()
    old_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(capture)
    try:
        payment_event(
            "payment_callback_accepted",
            public_id=uuid.UUID("12345678-1234-5678-1234-567812345678"),
            provider_invoice_id=42,
            previous_status="pending",
            new_status="paid",
            outcome="accepted",
            signature_valid=True,
            amount_match=True,
        )
    finally:
        logger.removeHandler(capture)
        logger.setLevel(old_level)
    payload = capture.payloads[0]
    assert payload["provider_invoice_id"] == 42
    assert payload["previous_status"] == "pending"
    assert payload["new_status"] == "paid"
    assert payload["signature_valid"] is True
    assert payload["amount_match"] is True
    assert len(str(payload["payment_order_fingerprint"])) == 16
    raw = json.dumps(payload)
    assert "12345678-1234-5678-1234-567812345678" not in raw
    assert "SignatureValue" not in raw
    assert "password" not in raw.lower()


def test_malformed_payment_extras_are_dropped_at_formatter_boundary() -> None:
    record = logging.LogRecord(
        "fetchnow.payments.audit",
        logging.INFO,
        __file__,
        1,
        "payment_event",
        (),
        None,
    )
    record.payment_order_fingerprint = "../../secret"
    record.provider_invoice_id = -1
    record.previous_status = "refunded-with-secret"
    record.signature_valid = "yes"
    payload = json.loads(JsonFormatter().format(record))
    assert "payment_order_fingerprint" not in payload
    assert "provider_invoice_id" not in payload
    assert "previous_status" not in payload
    assert "signature_valid" not in payload


def test_callback_diagnostic_formatter_emits_names_and_booleans_only() -> None:
    record = logging.LogRecord(
        "fetchnow.payments.callback_diagnostic",
        logging.INFO,
        __file__,
        1,
        "payment_callback_sanitized_diagnostic",
        (),
        None,
    )
    record.http_method = "POST"
    record.http_status = 400
    record.content_type_category = "form_urlencoded"
    record.callback_field_names = (
        "InvId",
        "IsTest",
        "OutSum",
        "SignatureValue",
    )
    record.expected_fields_present = True
    record.unexpected_callback_field_names = ("IsTest",)
    record.parser_entered = True
    record.parser_accepted = False
    record.signature_verification_attempted = False
    record.rejection_stage = "field_schema"
    record.rejection_category = "unexpected_field"
    record.response_outcome = "ERROR"
    record.raw_body = "OutSum=secret"
    record.password = "secret"

    payload = json.loads(JsonFormatter().format(record))

    assert payload["callback_field_names"] == [
        "InvId",
        "IsTest",
        "OutSum",
        "SignatureValue",
    ]
    assert payload["unexpected_callback_field_names"] == ["IsTest"]
    assert payload["parser_accepted"] is False
    raw = json.dumps(payload)
    assert "OutSum=secret" not in raw
    assert "password" not in raw.lower()
