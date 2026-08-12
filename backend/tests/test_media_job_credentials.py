"""Unit tests for media-job access-token parsing and hashing."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

import pytest

from fetchnow.jobs.credentials import (
    generate_access_token,
    hash_access_token,
    hash_request_fingerprint,
    parse_access_token,
    tokens_match,
)
from fetchnow.jobs.errors import JobError, JobErrorCode


def test_generate_access_token_shape() -> None:
    token = generate_access_token()
    assert len(token) == 43
    assert "=" not in token
    raw = parse_access_token(token)
    assert len(raw) == 32
    # Same construction as the helper contract.
    expected = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip(
        "="
    )
    assert len(expected) == 43


def test_parse_and_hash_roundtrip() -> None:
    token = generate_access_token()
    raw = parse_access_token(token)
    digest = hash_access_token(token)
    assert len(digest) == 32
    assert digest == hashlib.sha256(b"fetchnow:v1:media-job:access\0" + raw).digest()


def test_malformed_short_oversized_rejected() -> None:
    with pytest.raises(JobError) as short:
        parse_access_token("abc")
    assert short.value.code == JobErrorCode.INVALID_ACCESS_TOKEN

    with pytest.raises(JobError) as empty:
        parse_access_token("")
    assert empty.value.code == JobErrorCode.INVALID_ACCESS_TOKEN

    with pytest.raises(JobError) as oversized:
        parse_access_token("A" * 128)
    assert oversized.value.code == JobErrorCode.INVALID_ACCESS_TOKEN

    with pytest.raises(JobError) as padded:
        # Valid alphabet but wrong length / padding.
        parse_access_token(generate_access_token() + "=")
    assert padded.value.code == JobErrorCode.INVALID_ACCESS_TOKEN

    with pytest.raises(JobError) as bad_type:
        parse_access_token(None)  # type: ignore[arg-type]
    assert bad_type.value.code == JobErrorCode.INVALID_ACCESS_TOKEN


def test_domain_separated_hashes_differ() -> None:
    token = generate_access_token()
    access = hash_access_token(token)
    fingerprint = hash_request_fingerprint(
        access_token=token,
        normalized_request="https://vk.com/video-1_2",
    )
    assert access != fingerprint
    # Fingerprint domain material must not match access domain hash of same bytes.
    raw = parse_access_token(token)
    access_domain = hashlib.sha256(b"fetchnow:v1:media-job:access\0" + raw).digest()
    fp_domain = hmac.new(
        raw,
        b"fetchnow:v1:media-job:fingerprint\0https://vk.com/video-1_2",
        hashlib.sha256,
    ).digest()
    assert access == access_domain
    assert fingerprint == fp_domain
    assert access_domain != fp_domain


def test_fingerprint_deterministic() -> None:
    token = generate_access_token()
    a = hash_request_fingerprint(
        access_token=token,
        normalized_request="https://vk.com/video-1_2",
    )
    b = hash_request_fingerprint(
        access_token=token,
        normalized_request="https://vk.com/video-1_2",
    )
    assert a == b
    assert tokens_match(a, b)


def test_tokens_match_constant_time_api() -> None:
    left = secrets.token_bytes(32)
    right = bytes(left)
    assert tokens_match(left, right) is True
    assert tokens_match(left, secrets.token_bytes(32)) is False
    assert tokens_match(left, left[:16]) is False
    assert tokens_match(left, "not-bytes") is False  # type: ignore[arg-type]


def test_raw_token_absent_from_job_error_str_repr() -> None:
    token = generate_access_token()
    with pytest.raises(JobError) as exc_info:
        parse_access_token(token[:-1] + ("!" if token[-1] != "!" else "@"))
    err = exc_info.value
    assert token not in str(err)
    assert token not in repr(err)
    assert err.internal_reason is None or token not in (err.internal_reason or "")
    assert "INVALID_ACCESS_TOKEN" in str(err)
