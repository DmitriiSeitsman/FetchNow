"""Single-byte-range parser tests."""

from __future__ import annotations

import pytest

from fetchnow.delivery.range_parse import (
    ByteRange,
    RangeUnsatisfiable,
    parse_single_byte_range,
)
from fetchnow.downloads.errors import DownloadError


def test_absent_range_returns_none() -> None:
    assert parse_single_byte_range(None, size=100) is None
    assert parse_single_byte_range("", size=100) is None


def test_start_end() -> None:
    r = parse_single_byte_range("bytes=0-9", size=100)
    assert isinstance(r, ByteRange)
    assert r.start == 0 and r.end == 10 and r.length == 10
    assert r.content_range_header() == "bytes 0-9/100"


def test_start_open() -> None:
    r = parse_single_byte_range("bytes=90-", size=100)
    assert isinstance(r, ByteRange)
    assert r.start == 90 and r.end == 100 and r.length == 10


def test_suffix() -> None:
    r = parse_single_byte_range("bytes=-10", size=100)
    assert isinstance(r, ByteRange)
    assert r.start == 90 and r.end == 100


def test_boundary_byte() -> None:
    r = parse_single_byte_range("bytes=99-99", size=100)
    assert isinstance(r, ByteRange)
    assert r.length == 1


def test_unsatisfiable_start() -> None:
    r = parse_single_byte_range("bytes=100-", size=100)
    assert isinstance(r, RangeUnsatisfiable)
    assert r.content_range_header() == "bytes */100"


def test_multiple_ranges_rejected() -> None:
    with pytest.raises(DownloadError):
        parse_single_byte_range("bytes=0-1,2-3", size=100)


def test_malformed_unit_rejected() -> None:
    with pytest.raises(DownloadError):
        parse_single_byte_range("items=0-1", size=100)


def test_overflow_header_rejected() -> None:
    with pytest.raises(DownloadError):
        parse_single_byte_range("bytes=" + ("1" * 200), size=100)


def test_empty_range_rejected() -> None:
    with pytest.raises(DownloadError):
        parse_single_byte_range("bytes=5-4", size=100)
