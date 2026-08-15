"""Unit tests for browser delivery grant tokens and cookie parsing."""

from __future__ import annotations

import hashlib

import pytest

from fetchnow.delivery.cookie_parse import (
    CookieParseError,
    _extract,
    extract_delivery_grant_token,
)
from fetchnow.downloads.browser_grant_tokens import (
    COOKIE_NAME,
    generate_grant_token,
    grant_hashes_match,
    hash_grant_token,
    parse_grant_token,
)
from fetchnow.downloads.errors import DownloadError, DownloadErrorCode


def test_generate_parse_hash_roundtrip() -> None:
    token = generate_grant_token()
    assert len(token) == 43
    raw = parse_grant_token(token)
    assert len(raw) == 32
    digest = hash_grant_token(token)
    assert len(digest) == 32
    expected = hashlib.sha256(
        b"fetchnow:v1:browser-delivery-grant\0" + raw
    ).digest()
    assert digest == expected
    assert grant_hashes_match(digest, expected)


def test_token_not_in_error_repr() -> None:
    token = generate_grant_token()
    with pytest.raises(DownloadError) as exc:
        parse_grant_token("!" * 43)
    text = f"{exc.value!s}{exc.value!r}"
    assert token not in text
    assert "fetchnow:v1" not in text
    assert "!" * 43 not in text


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "abc",
        "a" * 43,
        "!" * 43,
    ],
)
def test_malformed_token_indistinguishable(bad: str) -> None:
    with pytest.raises(DownloadError) as exc:
        parse_grant_token(bad)
    assert exc.value.code is DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND


def test_padded_token_rejected() -> None:
    token = generate_grant_token()
    with pytest.raises(DownloadError) as exc:
        parse_grant_token(token + "=")
    assert exc.value.code is DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND


def test_hash_mismatch() -> None:
    a = hash_grant_token(generate_grant_token())
    b = hash_grant_token(generate_grant_token())
    assert not grant_hashes_match(a, b)
    assert not grant_hashes_match(a, b[:31])


def test_cookie_extract_happy() -> None:
    token = generate_grant_token()
    assert extract_delivery_grant_token(f"{COOKIE_NAME}={token}") == token
    assert (
        extract_delivery_grant_token(f"other=1; {COOKIE_NAME}={token}; x=y")
        == token
    )


def test_cookie_duplicate_rejected() -> None:
    token = generate_grant_token()
    header = f"{COOKIE_NAME}={token}; {COOKIE_NAME}={token}"
    with pytest.raises(DownloadError) as exc:
        extract_delivery_grant_token(header)
    assert exc.value.code is DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND
    with pytest.raises(CookieParseError) as raw:
        _extract(header)
    assert raw.value.reason == "COOKIE_DUPLICATE"
    assert token not in repr(raw.value)


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "session=abc",
        f"{COOKIE_NAME}=",
        f'{COOKIE_NAME}="abc"',
        "x" * 5000,
        f"{COOKIE_NAME}=abc\ndef",
    ],
)
def test_cookie_missing_or_bad(header: str | None) -> None:
    with pytest.raises(DownloadError) as exc:
        extract_delivery_grant_token(header)
    assert exc.value.code is DownloadErrorCode.DOWNLOAD_JOB_NOT_FOUND
    text = f"{exc.value!s}{exc.value!r}"
    assert COOKIE_NAME not in text
    if isinstance(header, str) and header and len(header) < 100:
        assert header not in text
