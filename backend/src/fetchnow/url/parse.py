"""URL parsing and normalization without network I/O."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import ParseResult, quote, urlparse, urlunparse

from fetchnow.url.errors import raise_validation_error


@dataclass(frozen=True, slots=True)
class ParsedURL:
    """Syntactically accepted URL prior to provider/DNS checks."""

    original_scheme: str
    scheme: str
    hostname: str
    port: int | None
    path: str
    query: str
    canonical_without_query: str


def _idna_hostname(raw_host: str) -> str:
    host = raw_host.rstrip(".")
    if not host:
        raise_validation_error("INVALID_URL")
    if "%" in host:
        # Reject percent-encoded authority tricks before any decoding round-trips.
        raise_validation_error("INVALID_URL")
    try:
        # Strict IDNA; rejects empty labels and many malformed hosts.
        ascii_host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise_validation_error("INVALID_URL")
    if not ascii_host or ".." in ascii_host or ascii_host.startswith("."):
        raise_validation_error("INVALID_URL")
    return ascii_host


def _normalize_port(
    scheme: str, port: int | None, allowed_ports: set[int]
) -> int | None:
    if port is None:
        default = 443 if scheme == "https" else 80
        if default not in allowed_ports:
            raise_validation_error("INVALID_URL")
        return None
    if port not in allowed_ports:
        raise_validation_error("INVALID_URL")
    default = 443 if scheme == "https" else 80
    if port == default:
        return None
    return port


def _build_canonical(
    *,
    scheme: str,
    hostname: str,
    port: int | None,
    path: str,
) -> str:
    # Keep path as parsed; ensure empty path becomes "/".
    normalized_path = path if path else "/"
    netloc = hostname if port is None else f"{hostname}:{port}"
    result = ParseResult(
        scheme=scheme,
        netloc=netloc,
        path=normalized_path,
        params="",
        query="",
        fragment="",
    )
    return urlunparse(result)


def parse_and_normalize(
    raw: str,
    *,
    allowed_schemes: set[str],
    allowed_ports: set[int],
    max_length: int,
) -> ParsedURL:
    """Parse user input into a normalized authority/path representation."""
    if raw is None:
        raise_validation_error("INVALID_URL")

    candidate = raw.strip()
    if not candidate:
        raise_validation_error("INVALID_URL")
    if len(candidate) > max_length:
        raise_validation_error("INVALID_URL")

    # Scheme-relative URLs start with // and must be rejected before urlparse
    # invents a scheme from the environment.
    if candidate.startswith("//"):
        raise_validation_error("UNSUPPORTED_SCHEME")

    parsed = urlparse(candidate)
    if not parsed.scheme or not parsed.netloc:
        # javascript:, data:, or other non-hierarchical forms
        if parsed.scheme and parsed.scheme.lower() not in allowed_schemes:
            raise_validation_error("UNSUPPORTED_SCHEME")
        raise_validation_error("INVALID_URL")

    original_scheme = parsed.scheme
    scheme = parsed.scheme.lower()
    if scheme not in allowed_schemes:
        raise_validation_error("UNSUPPORTED_SCHEME")

    if parsed.username is not None or parsed.password is not None:
        raise_validation_error("INVALID_URL")

    hostname = parsed.hostname
    if hostname is None:
        raise_validation_error("INVALID_URL")
    host: str = hostname

    # urlparse lowercases host in some versions; still normalize via IDNA.
    # Bracketed IPv6: parsed.hostname already strips brackets.
    if ":" in host and not _is_probably_ipv6(host):
        # Unexpected colon in hostname
        raise_validation_error("INVALID_URL")

    normalized_host = host.lower() if _is_probably_ipv6(host) else _idna_hostname(host)

    try:
        port = parsed.port
    except ValueError:
        raise_validation_error("INVALID_URL")

    normalized_port = _normalize_port(scheme, port, allowed_ports)

    # Preserve path/query as parsed once; do not recursively percent-decode.
    path = parsed.path or "/"
    query = parsed.query  # may be empty; fragment intentionally dropped

    # Guard against NUL and other control characters slipping through.
    if any(ord(ch) < 32 for ch in path) or any(ord(ch) < 32 for ch in query):
        raise_validation_error("INVALID_URL")

    canonical = _build_canonical(
        scheme=scheme,
        hostname=_canonical_host_for_url(normalized_host),
        port=normalized_port,
        path=path,
    )

    return ParsedURL(
        original_scheme=original_scheme.lower(),
        scheme=scheme,
        hostname=normalized_host,
        port=normalized_port,
        path=path,
        query=query,
        canonical_without_query=canonical,
    )


def _is_probably_ipv6(hostname: str) -> bool:
    return ":" in hostname


def _canonical_host_for_url(hostname: str) -> str:
    if _is_probably_ipv6(hostname):
        return f"[{hostname}]"
    # Hostnames are already ASCII/IDNA; quote is a safety no-op for labels.
    return quote(hostname, safe=".-_")
