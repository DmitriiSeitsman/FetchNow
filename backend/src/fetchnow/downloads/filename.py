"""Server-side suggested download filenames derived from sanitized titles.

This is not a trusted provider filename. The provider reports a title; the
server builds an attachment name. Access tokens, submitted URLs, media IDs,
and query/fragment never enter the name.
"""

from __future__ import annotations

import re
import unicodedata
import uuid

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_BIDI = dict.fromkeys(
    (
        0x061C,
        0x200E,
        0x200F,
        0x202A,
        0x202B,
        0x202C,
        0x202D,
        0x202E,
        0x2066,
        0x2067,
        0x2068,
        0x2069,
    )
)
_FORBIDDEN = frozenset('\\/:*?"<>|\0')
_WHITESPACE = re.compile(r"\s+")
_ATTR_CHAR = re.compile(r"^[A-Za-z0-9!#$&+\-.^_`|~]$")
_RESERVED = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)
_CONTAINER_RE = re.compile(r"^[a-z0-9]{2,8}$")
ALLOWED_CONTAINERS = frozenset({"mp4", "webm", "mkv", "m4a", "mp3", "ogg"})
MAX_STEM_CHARS = 80  # Unicode code points, not UTF-16 code units
MAX_STEM_UTF8_BYTES = 180
MAX_FILENAME_CHARS = 255  # Unicode code points
MAX_FILENAME_UTF8_BYTES = 240


def fallback_filename(download_job_id: uuid.UUID, container: str) -> str:
    ext = _validated_container(container)
    return f"fetchnow-{download_job_id}.{ext}"


def suggested_filename_for(
    *,
    title: str | None,
    container: str,
    download_job_id: uuid.UUID,
) -> str:
    """Deterministic attachment name. Invalid titles use the UUID fallback."""
    ext = _validated_container(container)
    fallback = fallback_filename(download_job_id, ext)
    stem = _sanitize_stem(title)
    if stem is None:
        return fallback
    name = f"{stem}.{ext}"
    if not _filename_bounds_ok(name):
        return fallback
    return name


def ascii_filename_parameter(name: str, fallback: str) -> str:
    """Quoted ASCII `filename=` value. Non-ASCII names use the UUID fallback."""
    if _is_ascii_token(name):
        return name
    if not _is_ascii_token(fallback):
        raise ValueError("unsafe ascii fallback")
    return fallback


def content_disposition_header(*, filename: str, ascii_fallback: str) -> str:
    """attachment; filename="ascii"; filename*=UTF-8''... with no CR/LF."""
    ascii_name = ascii_filename_parameter(filename, ascii_fallback)
    if any(ch in ascii_name for ch in '"\\\r\n'):
        raise ValueError("unsafe ascii filename")
    if any(ch in filename for ch in "\r\n\0"):
        raise ValueError("unsafe unicode filename")
    encoded = encode_rfc8187(filename)
    header = f'attachment; filename="{ascii_name}"; filename*={encoded}'
    if "\r" in header or "\n" in header or "\0" in header:
        raise ValueError("header injection")
    return header


def encode_rfc8187(value: str) -> str:
    """RFC 8187 `UTF-8''` percent-encoding. Uppercase hex, deterministic."""
    pieces: list[str] = ["UTF-8''"]
    for byte in value.encode("utf-8"):
        char = chr(byte)
        if _ATTR_CHAR.fullmatch(char):
            pieces.append(char)
        else:
            pieces.append(f"%{byte:02X}")
    return "".join(pieces)


def decode_rfc8187(value: str) -> str | None:
    """Decode `UTF-8''pct-encoded`. Reject malformed percent and invalid UTF-8."""
    if not value.startswith("UTF-8''"):
        return None
    raw = value[7:]
    if not raw:
        return None
    pieces: list[int] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "%":
            hex_part = raw[i + 1 : i + 3]
            if len(hex_part) != 2:
                return None
            try:
                pieces.append(int(hex_part, 16))
            except ValueError:
                return None
            i += 3
            continue
        if not _ATTR_CHAR.fullmatch(ch):
            return None
        pieces.append(ord(ch))
        i += 1
    try:
        return bytes(pieces).decode("utf-8")
    except UnicodeDecodeError:
        return None


def is_safe_suggested_filename(name: str, *, container: str) -> bool:
    ext = _validated_container(container)
    if not isinstance(name, str) or not name.endswith(f".{ext}"):
        return False
    if not _filename_bounds_ok(name):
        return False
    if any(ch in name for ch in _FORBIDDEN) or "\r" in name or "\n" in name:
        return False
    stem = name[: -(len(ext) + 1)]
    if stem != _sanitize_stem(stem):
        # Already-sanitized stems must round-trip; UUID fallback is also allowed.
        return _UUID_FALLBACK.fullmatch(name) is not None
    return True


_UUID_FALLBACK = re.compile(
    r"^fetchnow-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.[a-z0-9]{2,8}$",
    re.IGNORECASE,
)


def _validated_container(container: str) -> str:
    ext = container.strip().lower()
    if not _CONTAINER_RE.fullmatch(ext) or ext not in ALLOWED_CONTAINERS:
        raise ValueError("unsafe container")
    return ext


def _is_ascii_token(value: str) -> bool:
    if not value or not value.isascii():
        return False
    return all(32 <= ord(ch) <= 126 and ch not in '"\\\r\n' for ch in value)


def _filename_bounds_ok(name: str) -> bool:
    if not name or len(name) > MAX_FILENAME_CHARS:
        return False
    encoded = name.encode("utf-8")
    return 1 <= len(encoded) <= MAX_FILENAME_UTF8_BYTES


def _sanitize_stem(raw: str | None) -> str | None:
    if raw is None or not isinstance(raw, str):
        return None
    text = unicodedata.normalize("NFC", raw)
    cleaned: list[str] = []
    for char in text:
        code = ord(char)
        if code in _BIDI or char in _FORBIDDEN:
            continue
        category = unicodedata.category(char)
        if category.startswith("C"):
            continue
        if char.isspace():
            cleaned.append(" ")
            continue
        cleaned.append(char)
    collapsed = _WHITESPACE.sub(" ", "".join(cleaned)).strip(" .")
    collapsed = _CONTROL.sub("", collapsed)
    if not collapsed or collapsed in {".", ".."}:
        return None
    head = collapsed.split(".", 1)[0]
    if head.upper() in _RESERVED:
        return None
    if len(collapsed) > MAX_STEM_CHARS:
        collapsed = collapsed[:MAX_STEM_CHARS].rstrip(" .")
    encoded = collapsed.encode("utf-8")
    if len(encoded) > MAX_STEM_UTF8_BYTES:
        encoded = encoded[:MAX_STEM_UTF8_BYTES]
        collapsed = encoded.decode("utf-8", errors="ignore").rstrip(" .")
    if not collapsed or collapsed in {".", ".."}:
        return None
    if collapsed.upper() in _RESERVED:
        return None
    return collapsed
