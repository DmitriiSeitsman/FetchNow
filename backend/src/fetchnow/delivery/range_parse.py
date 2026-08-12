"""Bounded single-byte-range parsing for private artifact delivery."""

from __future__ import annotations

from dataclasses import dataclass

from fetchnow.downloads.errors import DownloadErrorCode, raise_download_error

# Cap Range header length so parsing never allocates proportional to attacker input.
_MAX_RANGE_HEADER_CHARS = 128


@dataclass(frozen=True, slots=True, repr=False)
class ByteRange:
    """Inclusive start / exclusive end interval into a validated artifact."""

    start: int
    end: int  # exclusive
    total: int

    @property
    def length(self) -> int:
        return self.end - self.start

    def content_range_header(self) -> str:
        # RFC 9110: inclusive end in Content-Range.
        return f"bytes {self.start}-{self.end - 1}/{self.total}"

    def __repr__(self) -> str:
        return (
            f"ByteRange(start={self.start!r}, end={self.end!r}, "
            f"total={self.total!r})"
        )


@dataclass(frozen=True, slots=True)
class RangeUnsatisfiable:
    """Marker for HTTP 416 responses."""

    total: int

    def content_range_header(self) -> str:
        return f"bytes */{self.total}"


def parse_single_byte_range(
    header: str | None,
    *,
    size: int,
) -> ByteRange | RangeUnsatisfiable | None:
    """Parse a single ``bytes=`` Range or return None when absent.

    Rejects multiple ranges, unknown units, overflows, and empty ambiguity.
    """
    if header is None:
        return None
    if not isinstance(header, str):
        raise_download_error(
            DownloadErrorCode.INTERNAL_ERROR,
            internal_reason="RANGE_HEADER_TYPE",
            http_status=400,
        )
    raw = header.strip()
    if not raw:
        return None
    if len(raw) > _MAX_RANGE_HEADER_CHARS:
        raise_download_error(
            DownloadErrorCode.INTERNAL_ERROR,
            internal_reason="RANGE_HEADER_TOO_LONG",
            http_status=400,
        )
    if "," in raw:
        raise_download_error(
            DownloadErrorCode.INTERNAL_ERROR,
            internal_reason="RANGE_MULTIPLE",
            http_status=400,
        )
    if not raw.lower().startswith("bytes="):
        raise_download_error(
            DownloadErrorCode.INTERNAL_ERROR,
            internal_reason="RANGE_UNIT",
            http_status=400,
        )
    spec = raw[6:]
    if not spec or "=" in spec or " " in spec:
        raise_download_error(
            DownloadErrorCode.INTERNAL_ERROR,
            internal_reason="RANGE_MALFORMED",
            http_status=400,
        )
    if size <= 0:
        raise_download_error(
            DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE,
            internal_reason="RANGE_EMPTY_TARGET",
        )

    if spec.startswith("-"):
        # Suffix: last N bytes.
        suffix_s = spec[1:]
        if not suffix_s.isdigit():
            raise_download_error(
                DownloadErrorCode.INTERNAL_ERROR,
                internal_reason="RANGE_SUFFIX_MALFORMED",
                http_status=400,
            )
        if len(suffix_s) > 1 and suffix_s.startswith("0"):
            raise_download_error(
                DownloadErrorCode.INTERNAL_ERROR,
                internal_reason="RANGE_SUFFIX_LEADING_ZERO",
                http_status=400,
            )
        suffix = int(suffix_s)
        if suffix == 0:
            return RangeUnsatisfiable(total=size)
        start = 0 if suffix > size else size - suffix
        return ByteRange(start=start, end=size, total=size)

    if "-" not in spec:
        raise_download_error(
            DownloadErrorCode.INTERNAL_ERROR,
            internal_reason="RANGE_MALFORMED",
            http_status=400,
        )
    start_s, end_s = spec.split("-", 1)
    if not start_s.isdigit():
        raise_download_error(
            DownloadErrorCode.INTERNAL_ERROR,
            internal_reason="RANGE_START_MALFORMED",
            http_status=400,
        )
    start = int(start_s)
    if start >= size:
        return RangeUnsatisfiable(total=size)

    if end_s == "":
        # start- form: through end of file.
        return ByteRange(start=start, end=size, total=size)

    if not end_s.isdigit():
        raise_download_error(
            DownloadErrorCode.INTERNAL_ERROR,
            internal_reason="RANGE_END_MALFORMED",
            http_status=400,
        )
    end_inclusive = int(end_s)
    if end_inclusive < start:
        raise_download_error(
            DownloadErrorCode.INTERNAL_ERROR,
            internal_reason="RANGE_EMPTY",
            http_status=400,
        )
    end_exclusive = min(end_inclusive + 1, size)
    return ByteRange(start=start, end=end_exclusive, total=size)
