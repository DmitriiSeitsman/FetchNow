"""Exact classic-interface money, Receipt, and signature primitives."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from fetchnow.payments.catalog import PaymentProduct
from fetchnow.payments.errors import InvalidCallbackError, PaymentInvariantError

PAYMENT_ACTION = "https://auth.robokassa.ru/Merchant/Index.aspx"
_OUT_SUM = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.([0-9]+))?$")
_HEX_SHA256 = re.compile(r"^[0-9A-Fa-f]{64}$")


@dataclass(frozen=True, slots=True)
class ReceiptRepresentation:
    json_text: str
    url_encoded: str


def format_out_sum(amount_minor: int) -> str:
    if type(amount_minor) is not int or amount_minor <= 0:
        raise ValueError("amount_minor must be a positive integer")
    whole, fractional = divmod(amount_minor, 100)
    return f"{whole}.{fractional:02d}"


def parse_callback_out_sum(raw: str) -> int:
    if not isinstance(raw, str) or len(raw) > 64 or _OUT_SUM.fullmatch(raw) is None:
        raise InvalidCallbackError()
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise InvalidCallbackError() from exc
    scaled = value * 100
    if scaled != scaled.to_integral_value() or scaled <= 0:
        raise InvalidCallbackError()
    amount = int(scaled)
    if amount > 9_223_372_036_854_775_807:
        raise InvalidCallbackError()
    return amount


def serialize_receipt(product: PaymentProduct) -> ReceiptRepresentation:
    if product.amount_minor <= 0 or product.receipt_payment_object != "service":
        raise PaymentInvariantError()

    # ``sum`` must be a JSON number, never a float and never a JSON string.
    # Assemble only this fixed schema while json.dumps safely escapes strings.
    def string(value: str) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    text = (
        '{"items":[{"name":'
        f"{string(product.receipt_name)},"
        '"quantity":1,'
        f'"sum":{format_out_sum(product.amount_minor)},'
        f'"payment_method":{string(product.receipt_payment_method)},'
        f'"payment_object":{string(product.receipt_payment_object)},'
        f'"tax":{string(product.receipt_tax)}'
        "}]}"
    )
    return ReceiptRepresentation(text, quote(text, safe="", encoding="utf-8"))


def encode_receipt_json(receipt_json: str) -> str:
    return quote(receipt_json, safe="", encoding="utf-8")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def initiation_signature_base(
    *,
    merchant_login: str,
    out_sum: str,
    inv_id: int,
    encoded_receipt: str,
    password1: str,
) -> str:
    return f"{merchant_login}:{out_sum}:{inv_id}:{encoded_receipt}:{password1}"


def sign_initiation(
    *,
    merchant_login: str,
    out_sum: str,
    inv_id: int,
    encoded_receipt: str,
    password1: str,
) -> str:
    return _sha256(
        initiation_signature_base(
            merchant_login=merchant_login,
            out_sum=out_sum,
            inv_id=inv_id,
            encoded_receipt=encoded_receipt,
            password1=password1,
        )
    )


def callback_signature_base(*, raw_out_sum: str, inv_id: int, password2: str) -> str:
    return f"{raw_out_sum}:{inv_id}:{password2}"


def sign_callback(*, raw_out_sum: str, inv_id: int, password2: str) -> str:
    return _sha256(
        callback_signature_base(
            raw_out_sum=raw_out_sum, inv_id=inv_id, password2=password2
        )
    )


def signature_matches(*, supplied: str, expected: str) -> bool:
    if _HEX_SHA256.fullmatch(supplied) is None:
        return False
    return hmac.compare_digest(supplied.lower(), expected.lower())
