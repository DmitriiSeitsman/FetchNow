"""Exact Robokassa classic-interface canonicalization tests."""

from __future__ import annotations

import hashlib
import json
from urllib.parse import parse_qs, unquote, urlencode

import pytest

from fetchnow.payments.catalog import PaymentProduct
from fetchnow.payments.errors import InvalidCallbackError
from fetchnow.payments.robokassa import (
    callback_signature_base,
    encode_receipt_json,
    format_out_sum,
    initiation_signature_base,
    parse_callback_out_sum,
    serialize_receipt,
    sign_callback,
    sign_initiation,
    signature_matches,
)


def _product(amount: int = 19_900) -> PaymentProduct:
    return PaymentProduct(
        code="premium_24h",
        amount_minor=amount,
        currency="RUB",
        entitlement_duration_seconds=86_400,
        receipt_name="Доступ к функциям FetchNow на 24 часа",
        receipt_tax="none",
        receipt_payment_method="full_payment",
    )


@pytest.mark.parametrize(
    ("minor", "expected"),
    [(1, "0.01"), (100, "1.00"), (1_000, "10.00"), (19_900, "199.00")],
)
def test_minor_units_format_exactly(minor: int, expected: str) -> None:
    assert format_out_sum(minor) == expected


@pytest.mark.parametrize("minor", [0, -1, 1.0, True])
def test_minor_units_reject_non_positive_or_non_integer(minor: object) -> None:
    with pytest.raises(ValueError):
        format_out_sum(minor)  # type: ignore[arg-type]


@pytest.mark.parametrize("raw", ["10", "10.0", "10.00", "10.000000"])
def test_callback_equivalent_scale_is_accepted(raw: str) -> None:
    assert parse_callback_out_sum(raw) == 1_000


@pytest.mark.parametrize(
    "raw",
    ["-10.00", "+10.00", "1e1", "10.001", "NaN", "Infinity", "", ".10"],
)
def test_callback_malformed_or_sub_kopeck_amount_is_rejected(raw: str) -> None:
    with pytest.raises(InvalidCallbackError):
        parse_callback_out_sum(raw)


def test_receipt_is_deterministic_numeric_one_item_service() -> None:
    first = serialize_receipt(_product())
    second = serialize_receipt(_product())
    assert first == second
    assert first.json_text == (
        '{"items":[{"name":"Доступ к функциям FetchNow на 24 часа",'
        '"quantity":1,"sum":199.00,"payment_method":"full_payment",'
        '"payment_object":"service","tax":"none"}]}'
    )
    decoded = json.loads(first.json_text)
    assert decoded["items"][0]["sum"] == 199.0
    assert decoded["items"][0]["payment_object"] == "service"


@pytest.mark.parametrize("amount_minor", [100, 1_000, 19_900])
def test_receipt_item_sum_equals_out_sum_exactly(amount_minor: int) -> None:
    product = _product(amount_minor)
    receipt = serialize_receipt(product)
    out_sum = format_out_sum(product.amount_minor)
    item_sum = json.loads(receipt.json_text)["items"][0]["sum"]
    assert parse_callback_out_sum(out_sum) == product.amount_minor
    parsed_item_minor = int(item_sum * 100)
    assert (
        parse_callback_out_sum(format_out_sum(parsed_item_minor))
        == product.amount_minor
    )
    assert out_sum == format_out_sum(parsed_item_minor)


def test_cyrillic_receipt_golden_vector_and_exactly_once_encoding() -> None:
    receipt = serialize_receipt(_product(100))
    assert receipt.url_encoded == (
        "%7B%22items%22%3A%5B%7B%22name%22%3A%22"
        "%D0%94%D0%BE%D1%81%D1%82%D1%83%D0%BF%20%D0%BA%20%D1%84%D1%83%D0%BD"
        "%D0%BA%D1%86%D0%B8%D1%8F%D0%BC%20FetchNow%20%D0%BD%D0%B0%2024%20%D1%87"
        "%D0%B0%D1%81%D0%B0%22%2C%22quantity%22%3A1%2C%22sum%22%3A1.00%2C"
        "%22payment_method%22%3A%22full_payment%22%2C%22payment_object%22%3A"
        "%22service%22%2C%22tax%22%3A%22none%22%7D%5D%7D"
    )
    assert unquote(receipt.url_encoded) == receipt.json_text
    assert "%25" not in receipt.url_encoded
    wire = urlencode({"Receipt": receipt.url_encoded})
    assert "Receipt=%257B%2522items%2522" in wire
    assert parse_qs(wire)["Receipt"] == [receipt.url_encoded]


def test_official_robokassa_receipt_encoding_golden_vector() -> None:
    official_json = (
        '{"items":[{"name":"product","quantity":1,"sum":8.96,"tax":"none"}]}'
    )
    assert encode_receipt_json(official_json) == (
        "%7B%22items%22%3A%5B%7B%22name%22%3A%22product%22%2C%22quantity%22"
        "%3A1%2C%22sum%22%3A8.96%2C%22tax%22%3A%22none%22%7D%5D%7D"
    )


def test_initiation_signature_uses_encoded_receipt_and_password1() -> None:
    encoded = encode_receipt_json(
        '{"items":[{"name":"product","quantity":1,"sum":8.96,"tax":"none"}]}'
    )
    base = initiation_signature_base(
        merchant_login="demo",
        out_sum="8.96",
        inv_id=12345,
        encoded_receipt=encoded,
        password1="password_1",
    )
    assert base == f"demo:8.96:12345:{encoded}:password_1"
    digest = sign_initiation(
        merchant_login="demo",
        out_sum="8.96",
        inv_id=12345,
        encoded_receipt=encoded,
        password1="password_1",
    )
    assert digest == "d4ebee124320c57ae4a9177c6411b45625ab2b1e23e091fb2258274c85009bc0"
    assert digest == hashlib.sha256(base.encode()).hexdigest()


def test_callback_signature_uses_raw_amount_and_password2() -> None:
    base = callback_signature_base(
        raw_out_sum="10.000000", inv_id=450009, password2="password_2"
    )
    assert base == "10.000000:450009:password_2"
    digest = sign_callback(
        raw_out_sum="10.000000", inv_id=450009, password2="password_2"
    )
    assert digest == "6ec1b9ee56aa533fbed90f4a15acb7d43e2d58d5b2ae595e8782e919ee7fd6c0"
    assert digest == hashlib.sha256(base.encode()).hexdigest()
    assert signature_matches(supplied=digest.upper(), expected=digest)
    assert not signature_matches(supplied="not-a-digest", expected=digest)
