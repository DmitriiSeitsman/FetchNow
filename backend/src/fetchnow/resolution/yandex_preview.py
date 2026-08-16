"""Yandex Video Preview wrapper resolver (PR3B).

Extracts a stable supported-provider identity from
``https://yandex.ru/video/preview/<numeric-id>`` via the resolver-scoped
DocumentFetchPort. Binding is preview-ID specific; embed/player URLs are never
terminal results. No yt-dlp, Playwright, or subprocess.

Live markup (observed 2026-08-11) may expose the stable source only inside a
trusted ``yastatic.net`` VK player iframe fragment (``counters.videoUrl``),
with empty ``dups`` maps. Compatibility with older ``dups[preview_id]`` markup
is preserved as the first extraction tier.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from fetchnow.network.models import SafeDocumentTarget
from fetchnow.resolution.errors import (
    ResolutionError,
    ResolutionErrorKind,
    raise_resolution_error,
)
from fetchnow.resolution.models import WrapperResolveOutcome
from fetchnow.resolution.protocols import DocumentFetchPort
from fetchnow.url.models import ProviderID
from fetchnow.url.provider_identity import (
    ProviderIdentityError,
    parse_stable_provider_identity,
)

RESOLVER_ID = "yandex_video_preview"
WRAPPER_TYPE = "yandex_video_preview"
EXACT_HOSTNAMES = frozenset({"yandex.ru"})
STRATEGY_TARGET_VIDEO_URL = "yandex_target_video_url"
STRATEGY_VIEWER_IFRAME_VIDEO_URL = "yandex_viewer_iframe_video_url"

_PREVIEW_PATH = re.compile(r"^/video/preview/([0-9]{1,32})$")
_PREVIEW_LOCATION = re.compile(r"^/video/preview/([0-9]{1,32})$")

# Trusted Yandex static VK player (live evidence 2026-08-11).
_YASTATIC_HOST = "yastatic.net"
_VK_PLAYER_PATH = re.compile(
    r"^/video-player/(0x[0-9a-fA-F]{8,24})/pages-common/vk/vk\.html$"
)
_IFRAME_VIDEO_URL_FIELD = "counters"

_MAX_SCRIPT_BLOCKS = 32
_MAX_FRAGMENT_BYTES = 65_536
_MAX_JSON_DEPTH = 8
# Nested JSON-string / HTML-entity decode budget after parse_qsl's single
# percent-decode of fragment values (parse_qsl is not counted here).
_MAX_DECODE_PASSES = 3
_MAX_CANDIDATE_URL_LEN = 2048
_MAX_SCAN_WINDOW = 262_144
_MAX_JSON_NODES = 4_096
_MAX_EMBEDDED_OBJECTS = 8
_MAX_HTML_TAGS = 2_048
_MAX_HTML_ATTRS = 8_192
_MAX_ATTR_LEN = 8_192
_MAX_ATTR_BYTES_TOTAL = 262_144
_MAX_DATA_STATES = 16
_MAX_IFRAMES_INSPECTED = 8
_MAX_IFRAME_SRC_LEN = 4_096
_MAX_QUERY_FIELDS = 32
_MAX_FIELD_NAME_LEN = 64
_MAX_FIELD_VALUE_LEN = 4_096
_MAX_JSON_KEY_LEN = 64
_MAX_IFRAME_CANDIDATES = 8

_FIELD_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

_UNSUPPORTED_MEDIA_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "youtu.be",
    }
)
_VK_HOSTS = frozenset(
    {
        "vk.com",
        "www.vk.com",
        "m.vk.com",
        "vk.ru",
        "www.vk.ru",
        "m.vk.ru",
        "vkvideo.ru",
        "www.vkvideo.ru",
        "m.vkvideo.ru",
    }
)
_RUTUBE_HOSTS = frozenset({"rutube.ru", "www.rutube.ru"})
_OK_HOSTS = frozenset(
    {
        "ok.ru",
        "www.ok.ru",
        "m.ok.ru",
        "mobile.ok.ru",
        "odnoklassniki.ru",
        "www.odnoklassniki.ru",
        "m.odnoklassniki.ru",
        "mobile.odnoklassniki.ru",
    }
)

_SCRIPT_OPEN_RE = re.compile(r"<script\b[^>]*>", re.IGNORECASE)
_SCRIPT_CLOSE_RE = re.compile(r"</script\s*>", re.IGNORECASE)
_FORBIDDEN_PATH_MARKERS = (
    "/video_ext.php",
    "/video_ext",
    ".m3u8",
    ".mpd",
    ".mp4",
    ".webm",
)


@dataclass(frozen=True, slots=True)
class _ExtractionResult:
    candidate_url: str
    strategy: str
    target_bound: bool
    scheme_upgraded: bool
    iframe_payload: bool = False


@dataclass(frozen=True, slots=True)
class YandexVideoPreviewResolver:
    """Production resolver for exact Yandex Video Preview URLs."""

    supported_provider_hosts: frozenset[str]
    resolver_id: str = RESOLVER_ID
    wrapper_type: str = WRAPPER_TYPE
    exact_hostnames: frozenset[str] = EXACT_HOSTNAMES

    def matches(self, validated: SafeDocumentTarget) -> bool:
        if validated.scheme != "https":
            return False
        if validated.hostname != "yandex.ru":
            return False
        if validated.port is not None:
            return False
        return _PREVIEW_PATH.fullmatch(validated.path) is not None

    async def resolve(
        self,
        validated: SafeDocumentTarget,
        *,
        documents: DocumentFetchPort,
    ) -> WrapperResolveOutcome:
        if not self.matches(validated):
            raise_resolution_error(
                ResolutionErrorKind.WRAPPER_UNSUPPORTED,
                internal_reason="YANDEX_SHAPE_MISMATCH",
            )
        match = _PREVIEW_PATH.fullmatch(validated.path)
        assert match is not None
        preview_id = match.group(1)

        doc = await documents.fetch_document(validated)
        text = doc.body.decode("utf-8", errors="replace")
        extracted = _extract_target_bound(
            text,
            preview_id=preview_id,
            supported_hosts=self.supported_provider_hosts,
        )
        metadata: dict[str, bool] = {
            "target_bound": extracted.target_bound,
            "scheme_upgraded": extracted.scheme_upgraded,
        }
        if extracted.iframe_payload:
            metadata["iframe_payload"] = True
        return WrapperResolveOutcome(
            candidate_url=extracted.candidate_url,
            strategy=extracted.strategy,
            metadata=metadata,
        )


def _extract_target_bound(
    text: str,
    *,
    preview_id: str,
    supported_hosts: frozenset[str],
) -> _ExtractionResult:
    """Precedence: dups record, then viewer-bound iframe payload."""
    dups_result = _try_extract_from_dups(
        text, preview_id=preview_id, supported_hosts=supported_hosts
    )
    iframe_result = _try_extract_from_viewer_iframe(
        text,
        preview_id=preview_id,
        supported_hosts=supported_hosts,
        strict=dups_result is None,
    )

    if dups_result is not None and iframe_result is not None:
        if dups_result.candidate_url != iframe_result.candidate_url:
            raise_resolution_error(
                ResolutionErrorKind.WRAPPER_UNRESOLVED,
                internal_reason="AMBIGUOUS_TARGET_RECORD",
            )
        return dups_result
    if dups_result is not None:
        return dups_result
    if iframe_result is not None:
        return iframe_result
    # Prefer viewer-specific reason when data-state existed but did not bind;
    # _try_extract_from_viewer_iframe returns None only for not_found.
    raise_resolution_error(
        ResolutionErrorKind.WRAPPER_UNRESOLVED,
        internal_reason="TARGET_VIEWER_STATE_NOT_FOUND",
    )


def _viewer_binding_status(
    data_states: list[str], *, preview_id: str
) -> str:
    """Return ok|not_found|mismatch|ambiguous for viewer-page binding."""
    bound = 0
    mismatches = 0
    for raw in data_states:
        if len(raw) > _MAX_ATTR_LEN:
            raise_resolution_error(
                ResolutionErrorKind.WRAPPER_UNRESOLVED,
                internal_reason="PARSER_LIMIT_EXCEEDED",
            )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, Mapping):
            continue

        location = data.get("location")
        location_ok = False
        if isinstance(location, str):
            loc_match = _PREVIEW_LOCATION.fullmatch(location.strip())
            location_ok = loc_match is not None and loc_match.group(1) == preview_id

        preloaded = data.get("preloadedState")
        has_viewer = isinstance(preloaded, Mapping) and isinstance(
            preloaded.get("viewer"), Mapping
        )
        video_id_raw: Any = None
        if has_viewer:
            internal = preloaded["viewer"].get("internal")  # type: ignore[index]
            if isinstance(internal, Mapping):
                video_id_raw = internal.get("videoId")

        video_id_nonempty = (
            video_id_raw is not None
            and not isinstance(video_id_raw, bool)
            and str(video_id_raw) != ""
        )
        if video_id_nonempty and str(video_id_raw) != preview_id:
            mismatches += 1
            continue

        if (location_ok and has_viewer) or (
            video_id_nonempty and str(video_id_raw) == preview_id and has_viewer
        ):
            bound += 1

    if mismatches and not bound:
        return "mismatch"
    if bound == 0:
        return "not_found"
    if bound > 1:
        return "ambiguous"
    return "ok"


def _assert_viewer_binding(data_states: list[str], *, preview_id: str) -> None:
    status = _viewer_binding_status(data_states, preview_id=preview_id)
    if status == "mismatch":
        raise_resolution_error(
            ResolutionErrorKind.WRAPPER_UNRESOLVED,
            internal_reason="TARGET_VIEWER_ID_MISMATCH",
        )
    if status == "not_found":
        raise_resolution_error(
            ResolutionErrorKind.WRAPPER_UNRESOLVED,
            internal_reason="TARGET_VIEWER_STATE_NOT_FOUND",
        )
    if status == "ambiguous":
        raise_resolution_error(
            ResolutionErrorKind.WRAPPER_UNRESOLVED,
            internal_reason="AMBIGUOUS_VIEWER_STATE",
        )


def _try_extract_from_dups(
    text: str,
    *,
    preview_id: str,
    supported_hosts: frozenset[str],
) -> _ExtractionResult | None:
    roots = _collect_json_roots(text)
    records = _find_target_records(roots, preview_id=preview_id)
    if not records:
        return None
    if len(records) > 1:
        sources: set[str] = set()
        for rec in records:
            normalized, _ = _stable_source_from_record(
                rec, supported_hosts=supported_hosts
            )
            if normalized is not None:
                sources.add(normalized)
        if len(sources) != 1:
            raise_resolution_error(
                ResolutionErrorKind.WRAPPER_UNRESOLVED,
                internal_reason="AMBIGUOUS_TARGET_RECORD",
            )
        record = records[0]
    else:
        record = records[0]

    normalized, upgraded = _stable_source_from_record(
        record, supported_hosts=supported_hosts
    )
    if normalized is None:
        unsupported = _unsupported_source_from_record(record)
        if unsupported is not None:
            return _ExtractionResult(
                candidate_url=unsupported,
                strategy=STRATEGY_TARGET_VIDEO_URL,
                target_bound=True,
                scheme_upgraded=False,
            )
        return None
    return _ExtractionResult(
        candidate_url=normalized,
        strategy=STRATEGY_TARGET_VIDEO_URL,
        target_bound=True,
        scheme_upgraded=upgraded,
    )


def _try_extract_from_viewer_iframe(
    text: str,
    *,
    preview_id: str,
    supported_hosts: frozenset[str],
    strict: bool,
) -> _ExtractionResult | None:
    parsed = _parse_html_targets(text)
    binding = _viewer_binding_status(parsed.data_states, preview_id=preview_id)
    if binding == "not_found":
        if strict:
            raise_resolution_error(
                ResolutionErrorKind.WRAPPER_UNRESOLVED,
                internal_reason="TARGET_VIEWER_STATE_NOT_FOUND",
            )
        return None
    if binding == "mismatch":
        raise_resolution_error(
            ResolutionErrorKind.WRAPPER_UNRESOLVED,
            internal_reason="TARGET_VIEWER_ID_MISMATCH",
        )
    if binding == "ambiguous":
        raise_resolution_error(
            ResolutionErrorKind.WRAPPER_UNRESOLVED,
            internal_reason="AMBIGUOUS_VIEWER_STATE",
        )

    trusted_srcs = [
        src
        for src in parsed.iframe_srcs
        if _classify_trusted_vk_player_iframe(src) is not None
    ]
    unique_parsed: dict[str, _ParsedTrustedIframe] = {}
    first_error: ResolutionError | None = None
    for src in trusted_srcs:
        try:
            item = _parse_trusted_iframe_src(src, soft_untrusted=True)
        except ResolutionError as exc:
            if first_error is None:
                first_error = exc
            continue
        if item is None:
            continue
        unique_parsed.setdefault(item.identity, item)

    if not unique_parsed:
        if first_error is not None:
            raise first_error
        if strict:
            raise_resolution_error(
                ResolutionErrorKind.WRAPPER_UNRESOLVED,
                internal_reason="TARGET_PLAYER_NOT_FOUND",
            )
        return None

    candidates: list[tuple[str, bool]] = []
    for item in unique_parsed.values():
        video_url = _decode_counters_video_url(item.counters_value)
        normalized, upgraded = _normalize_stable_provider_url(
            video_url,
            supported_hosts=supported_hosts,
            allow_http_upgrade=True,
        )
        if normalized is None:
            decoded_video = _decode_string_value(video_url)
            if decoded_video is not None:
                try:
                    parts_v = urlsplit(decoded_video)
                except ValueError:
                    parts_v = None
                if parts_v is not None:
                    host = (parts_v.hostname or "").lower().rstrip(".")
                    if host in _UNSUPPORTED_MEDIA_HOSTS and parts_v.scheme in {
                        "http",
                        "https",
                    }:
                        candidates.append(
                            (f"https://{host}{parts_v.path or '/'}", False)
                        )
                        continue
            raise_resolution_error(
                ResolutionErrorKind.WRAPPER_UNRESOLVED,
                internal_reason="TARGET_VIDEO_URL_NOT_FOUND",
            )
        else:
            candidates.append((normalized, upgraded))
        if len(candidates) > _MAX_IFRAME_CANDIDATES:
            raise_resolution_error(
                ResolutionErrorKind.WRAPPER_UNRESOLVED,
                internal_reason="PARSER_LIMIT_EXCEEDED",
            )

    if not candidates:
        if strict:
            raise_resolution_error(
                ResolutionErrorKind.WRAPPER_UNRESOLVED,
                internal_reason="TARGET_VIDEO_URL_NOT_FOUND",
            )
        return None

    unique_urls = {url for url, _ in candidates}
    if len(unique_urls) != 1:
        raise_resolution_error(
            ResolutionErrorKind.WRAPPER_UNRESOLVED,
            internal_reason="AMBIGUOUS_TARGET_PLAYER",
        )

    url, upgraded = next(iter(candidates))
    return _ExtractionResult(
        candidate_url=url,
        strategy=STRATEGY_VIEWER_IFRAME_VIDEO_URL,
        target_bound=True,
        scheme_upgraded=upgraded,
        iframe_payload=True,
    )


@dataclass(slots=True)
class _HtmlTargets:
    data_states: list[str]
    iframe_srcs: list[str]


class _YandexHtmlTargetParser(HTMLParser):
    """Bounded HTML parser collecting data-state and iframe src only."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tag_count = 0
        self.attr_count = 0
        self.attr_bytes = 0
        self.data_states: list[str] = []
        self.iframe_srcs: list[str] = []
        self.limit_exceeded = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.limit_exceeded:
            return
        self.tag_count += 1
        if self.tag_count > _MAX_HTML_TAGS:
            self.limit_exceeded = True
            return
        for name, value in attrs:
            self.attr_count += 1
            if self.attr_count > _MAX_HTML_ATTRS:
                self.limit_exceeded = True
                return
            if value is None:
                continue
            value_len = len(value)
            if value_len > _MAX_ATTR_LEN:
                self.limit_exceeded = True
                return
            self.attr_bytes += value_len
            if self.attr_bytes > _MAX_ATTR_BYTES_TOTAL:
                self.limit_exceeded = True
                return
            if name == "data-state":
                if len(self.data_states) < _MAX_DATA_STATES:
                    self.data_states.append(value)
            elif tag == "iframe" and name == "src":
                if len(self.iframe_srcs) >= _MAX_IFRAMES_INSPECTED:
                    self.limit_exceeded = True
                    return
                self.iframe_srcs.append(value)


def _parse_html_targets(text: str) -> _HtmlTargets:
    parser = _YandexHtmlTargetParser()
    parser.feed(text[:_MAX_SCAN_WINDOW])
    parser.close()
    if parser.limit_exceeded:
        raise_resolution_error(
            ResolutionErrorKind.WRAPPER_UNRESOLVED,
            internal_reason="PARSER_LIMIT_EXCEEDED",
        )
    return _HtmlTargets(
        data_states=parser.data_states,
        iframe_srcs=parser.iframe_srcs,
    )


def _classify_trusted_vk_player_iframe(src: str) -> str | None:
    """Return host+path identity for a trusted VK player iframe, or None."""
    if not isinstance(src, str) or not src or len(src) > _MAX_IFRAME_SRC_LEN:
        return None
    raw = src.strip()
    if raw.startswith("//"):
        raw = "https:" + raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    if parts.scheme != "https":
        return None
    if parts.username is not None or parts.password is not None:
        return None
    if parts.port is not None:
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    if host != _YASTATIC_HOST:
        return None
    if _VK_PLAYER_PATH.fullmatch(parts.path or "") is None:
        return None
    return f"https://{host}{parts.path}"


@dataclass(frozen=True, slots=True)
class _ParsedTrustedIframe:
    """Safe structural view of one trusted iframe (no raw URL logging)."""

    classified: str
    identity: str
    counters_value: str


def _iframe_structural_identity(src: str) -> str | None:
    """Dedupe key for trusted iframes; None if not a trusted shape."""
    parsed = _parse_trusted_iframe_src(src, soft_untrusted=True)
    if parsed is None:
        return None
    return parsed.identity


def _video_url_from_trusted_iframe(
    src: str,
    *,
    supported_hosts: frozenset[str],
) -> tuple[str, bool] | None:
    parsed = _parse_trusted_iframe_src(src, soft_untrusted=True)
    if parsed is None:
        return None
    video_url = _decode_counters_video_url(parsed.counters_value)
    normalized, upgraded = _normalize_stable_provider_url(
        video_url,
        supported_hosts=supported_hosts,
        allow_http_upgrade=True,
    )
    if normalized is None:
        decoded_video = _decode_string_value(video_url)
        if decoded_video is not None:
            try:
                parts_v = urlsplit(decoded_video)
            except ValueError:
                parts_v = None
            if parts_v is not None:
                host = (parts_v.hostname or "").lower().rstrip(".")
                if host in _UNSUPPORTED_MEDIA_HOSTS and parts_v.scheme in {
                    "http",
                    "https",
                }:
                    return f"https://{host}{parts_v.path or '/'}", False
        raise_resolution_error(
            ResolutionErrorKind.WRAPPER_UNRESOLVED,
            internal_reason="TARGET_VIDEO_URL_NOT_FOUND",
        )
    return normalized, upgraded


def _parse_trusted_iframe_src(
    src: str,
    *,
    soft_untrusted: bool,
) -> _ParsedTrustedIframe | None:
    """Parse a trusted iframe src under the shared fragment contract.

    When ``soft_untrusted`` is True, non-matching host/path returns None.
    All other contract violations raise typed resolution errors.
    """
    if not isinstance(src, str) or not src:
        if soft_untrusted:
            return None
        raise_resolution_error(
            ResolutionErrorKind.WRAPPER_UNRESOLVED,
            internal_reason="TARGET_PLAYER_PAYLOAD_INVALID",
        )
    if len(src) > _MAX_IFRAME_SRC_LEN:
        raise_resolution_error(
            ResolutionErrorKind.WRAPPER_UNRESOLVED,
            internal_reason="PARSER_LIMIT_EXCEEDED",
        )
    classified = _classify_trusted_vk_player_iframe(src)
    if classified is None:
        if soft_untrusted:
            return None
        raise_resolution_error(
            ResolutionErrorKind.WRAPPER_UNRESOLVED,
            internal_reason="TARGET_PLAYER_PAYLOAD_INVALID",
        )

    raw = src.strip()
    if raw.startswith("//"):
        raw = "https:" + raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        raise_resolution_error(
            ResolutionErrorKind.WRAPPER_UNRESOLVED,
            internal_reason="TARGET_PLAYER_PAYLOAD_INVALID",
        )

    # Live contract is fragment-only: any query is forbidden (not ignored).
    if parts.query:
        raise_resolution_error(
            ResolutionErrorKind.WRAPPER_UNRESOLVED,
            internal_reason="TARGET_PLAYER_PAYLOAD_INVALID",
        )

    fragment = parts.fragment or ""
    if len(fragment) > _MAX_IFRAME_SRC_LEN:
        raise_resolution_error(
            ResolutionErrorKind.WRAPPER_UNRESOLVED,
            internal_reason="PARSER_LIMIT_EXCEEDED",
        )

    fields = _parse_iframe_fragment_fields(fragment)
    counters_value = _require_exactly_one_counters(fields)
    names = tuple(sorted({name for name, _ in fields}))
    digest = hashlib.sha256(counters_value.encode("utf-8")).hexdigest()[:16]
    identity = f"{classified}|{names}|{digest}"
    return _ParsedTrustedIframe(
        classified=classified,
        identity=identity,
        counters_value=counters_value,
    )


def _parse_iframe_fragment_fields(fragment: str) -> list[tuple[str, str]]:
    """Strict bounded fragment parsing shared by identity and extraction."""
    try:
        fields = parse_qsl(
            fragment,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=_MAX_QUERY_FIELDS,
        )
    except ValueError as exc:
        message = str(exc).lower()
        if "max number of fields" in message:
            raise_resolution_error(
                ResolutionErrorKind.WRAPPER_UNRESOLVED,
                internal_reason="PARSER_LIMIT_EXCEEDED",
            )
        raise_resolution_error(
            ResolutionErrorKind.WRAPPER_UNRESOLVED,
            internal_reason="TARGET_PLAYER_PAYLOAD_INVALID",
        )
    except UnicodeError:
        raise_resolution_error(
            ResolutionErrorKind.WRAPPER_UNRESOLVED,
            internal_reason="TARGET_PLAYER_PAYLOAD_INVALID",
        )

    for name, value in fields:
        _validate_fragment_field(name, value)
    return fields


def _validate_fragment_field(name: str, value: str) -> None:
    if not name or not _FIELD_NAME_RE.fullmatch(name):
        raise_resolution_error(
            ResolutionErrorKind.WRAPPER_UNRESOLVED,
            internal_reason="TARGET_PLAYER_PAYLOAD_INVALID",
        )
    if len(name) > _MAX_FIELD_NAME_LEN:
        raise_resolution_error(
            ResolutionErrorKind.WRAPPER_UNRESOLVED,
            internal_reason="PARSER_LIMIT_EXCEEDED",
        )
    if len(value) > _MAX_FIELD_VALUE_LEN:
        raise_resolution_error(
            ResolutionErrorKind.WRAPPER_UNRESOLVED,
            internal_reason="PARSER_LIMIT_EXCEEDED",
        )
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise_resolution_error(
            ResolutionErrorKind.WRAPPER_UNRESOLVED,
            internal_reason="TARGET_PLAYER_PAYLOAD_INVALID",
        )
    # parse_qsl percent-decodes once. Reject incomplete escapes left behind
    # (trailing '%' or '%' + one hex digit). Literal '%' (e.g. CSS "100%") and
    # non-hex tails like '%ZZ' are allowed in non-authoritative fields; the
    # authoritative counters value is checked the same way for incomplete forms.
    if _has_incomplete_percent_escape(value):
        raise_resolution_error(
            ResolutionErrorKind.WRAPPER_UNRESOLVED,
            internal_reason="TARGET_PLAYER_PAYLOAD_INVALID",
        )


def _has_incomplete_percent_escape(value: str) -> bool:
    """True when a residual '%' looks like a broken one-hex-digit escape.

    Bare trailing ``%`` (e.g. CSS ``100%``) and non-hex residues such as
    ``%ZZ`` are treated as literals. Incomplete forms are ``%A`` / ``%A?``
    where a hex digit is not followed by a second hex digit.
    """
    hexdigits = "0123456789abcdefABCDEF"
    i = 0
    n = len(value)
    while i < n:
        if value[i] != "%":
            i += 1
            continue
        if i + 1 >= n:
            return False
        h1 = value[i + 1]
        if h1 not in hexdigits:
            i += 1
            continue
        if i + 2 >= n or value[i + 2] not in hexdigits:
            return True
        i += 3
    return False


def _require_exactly_one_counters(fields: list[tuple[str, str]]) -> str:
    matches = [value for name, value in fields if name == _IFRAME_VIDEO_URL_FIELD]
    if not matches:
        raise_resolution_error(
            ResolutionErrorKind.WRAPPER_UNRESOLVED,
            internal_reason="TARGET_VIDEO_URL_NOT_FOUND",
        )
    if len(matches) > 1:
        raise_resolution_error(
            ResolutionErrorKind.WRAPPER_UNRESOLVED,
            internal_reason="AMBIGUOUS_TARGET_PLAYER",
        )
    return matches[0]


def _decode_counters_video_url(raw: str) -> str:
    """Decode fragment ``counters`` after parse_qsl percent-decode.

    Decode budget (not counting parse_qsl):
    - HTML entity unescape: ≤ ``_MAX_DECODE_PASSES``
    - nested JSON-string layers: ≤ ``_MAX_DECODE_PASSES``
    """
    if len(raw) > _MAX_FIELD_VALUE_LEN:
        raise_resolution_error(
            ResolutionErrorKind.WRAPPER_UNRESOLVED,
            internal_reason="PARSER_LIMIT_EXCEEDED",
        )
    text = raw
    for _ in range(_MAX_DECODE_PASSES):
        prev = text
        text = html.unescape(text)
        if text == prev:
            break
    if len(text) > _MAX_FIELD_VALUE_LEN:
        raise_resolution_error(
            ResolutionErrorKind.WRAPPER_UNRESOLVED,
            internal_reason="PARSER_LIMIT_EXCEEDED",
        )

    loaded: Any = text
    decode_layers = 0
    try:
        while isinstance(loaded, str):
            if decode_layers >= _MAX_DECODE_PASSES:
                raise_resolution_error(
                    ResolutionErrorKind.WRAPPER_UNRESOLVED,
                    internal_reason="PARSER_LIMIT_EXCEEDED",
                )
            loaded = json.loads(loaded)
            decode_layers += 1
    except json.JSONDecodeError:
        raise_resolution_error(
            ResolutionErrorKind.WRAPPER_UNRESOLVED,
            internal_reason="TARGET_PLAYER_PAYLOAD_INVALID",
        )
    except RecursionError:
        raise_resolution_error(
            ResolutionErrorKind.WRAPPER_UNRESOLVED,
            internal_reason="PARSER_LIMIT_EXCEEDED",
        )
    except UnicodeError:
        raise_resolution_error(
            ResolutionErrorKind.WRAPPER_UNRESOLVED,
            internal_reason="TARGET_PLAYER_PAYLOAD_INVALID",
        )
    except ValueError:
        raise_resolution_error(
            ResolutionErrorKind.WRAPPER_UNRESOLVED,
            internal_reason="TARGET_PLAYER_PAYLOAD_INVALID",
        )

    if not isinstance(loaded, Mapping):
        raise_resolution_error(
            ResolutionErrorKind.WRAPPER_UNRESOLVED,
            internal_reason="TARGET_PLAYER_PAYLOAD_INVALID",
        )

    budget = _NodeBudget(max_nodes=_MAX_JSON_NODES)
    try:
        _validate_counters_json_structure(loaded, depth=1, budget=budget)
    except RecursionError:
        raise_resolution_error(
            ResolutionErrorKind.WRAPPER_UNRESOLVED,
            internal_reason="PARSER_LIMIT_EXCEEDED",
        )

    video_url = loaded.get("videoUrl")
    if not isinstance(video_url, str):
        raise_resolution_error(
            ResolutionErrorKind.WRAPPER_UNRESOLVED,
            internal_reason="TARGET_VIDEO_URL_NOT_FOUND",
        )
    if len(video_url) > _MAX_CANDIDATE_URL_LEN:
        raise_resolution_error(
            ResolutionErrorKind.WRAPPER_UNRESOLVED,
            internal_reason="PARSER_LIMIT_EXCEEDED",
        )
    return video_url


def _validate_counters_json_structure(
    node: Any,
    *,
    depth: int,
    budget: _NodeBudget,
) -> None:
    """Enforce JSON depth/node/key/string bounds before reading ``videoUrl``."""
    if depth > _MAX_JSON_DEPTH:
        raise_resolution_error(
            ResolutionErrorKind.WRAPPER_UNRESOLVED,
            internal_reason="PARSER_LIMIT_EXCEEDED",
        )
    budget.consume()

    if isinstance(node, Mapping):
        for key, value in node.items():
            if not isinstance(key, str):
                raise_resolution_error(
                    ResolutionErrorKind.WRAPPER_UNRESOLVED,
                    internal_reason="TARGET_PLAYER_PAYLOAD_INVALID",
                )
            if len(key) > _MAX_JSON_KEY_LEN:
                raise_resolution_error(
                    ResolutionErrorKind.WRAPPER_UNRESOLVED,
                    internal_reason="PARSER_LIMIT_EXCEEDED",
                )
            if isinstance(value, str) and len(value) > _MAX_FIELD_VALUE_LEN:
                raise_resolution_error(
                    ResolutionErrorKind.WRAPPER_UNRESOLVED,
                    internal_reason="PARSER_LIMIT_EXCEEDED",
                )
            _validate_counters_json_structure(
                value, depth=depth + 1, budget=budget
            )
        return
    if isinstance(node, list):
        for item in node:
            _validate_counters_json_structure(
                item, depth=depth + 1, budget=budget
            )
        return
    if isinstance(node, str | int | float | bool) or node is None:
        return
    raise_resolution_error(
        ResolutionErrorKind.WRAPPER_UNRESOLVED,
        internal_reason="TARGET_PLAYER_PAYLOAD_INVALID",
    )


def _stable_source_from_record(
    record: Mapping[str, Any],
    *,
    supported_hosts: frozenset[str],
) -> tuple[str | None, bool]:
    player = record.get("player")
    if isinstance(player, Mapping):
        video_url = player.get("videoUrl")
        if isinstance(video_url, str):
            normalized, upgraded = _normalize_stable_provider_url(
                video_url,
                supported_hosts=supported_hosts,
                allow_http_upgrade=True,
            )
            if normalized is not None:
                return normalized, upgraded
    record_url = record.get("url")
    if isinstance(record_url, str):
        normalized, upgraded = _normalize_stable_provider_url(
            record_url,
            supported_hosts=supported_hosts,
            allow_http_upgrade=True,
        )
        if normalized is not None:
            return normalized, upgraded
    return None, False


def _unsupported_source_from_record(record: Mapping[str, Any]) -> str | None:
    player = record.get("player")
    candidates: list[str] = []
    if isinstance(player, Mapping):
        video_url = player.get("videoUrl")
        if isinstance(video_url, str):
            candidates.append(video_url)
    record_url = record.get("url")
    if isinstance(record_url, str):
        candidates.append(record_url)
    for raw in candidates:
        value = _decode_string_value(raw)
        if value is None:
            continue
        try:
            parts = urlsplit(value)
        except ValueError:
            continue
        host = (parts.hostname or "").lower().rstrip(".")
        if host in _UNSUPPORTED_MEDIA_HOSTS and parts.scheme in {"http", "https"}:
            if parts.scheme == "http":
                return f"https://{host}{parts.path or '/'}"
            return f"{parts.scheme}://{host}{parts.path or '/'}"
    return None


def _find_target_records(
    roots: list[Any],
    *,
    preview_id: str,
) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []
    seen_ids: set[int] = set()
    budget = _NodeBudget(max_nodes=_MAX_JSON_NODES)
    for root in roots:
        _collect_target_records(
            root,
            preview_id=preview_id,
            depth=0,
            budget=budget,
            found=found,
            seen_ids=seen_ids,
            parent_dups=False,
            dups_key=None,
        )
    return found


class _NodeBudget:
    def __init__(self, *, max_nodes: int) -> None:
        self.max_nodes = max_nodes
        self.used = 0

    def consume(self) -> None:
        self.used += 1
        if self.used > self.max_nodes:
            raise_resolution_error(
                ResolutionErrorKind.WRAPPER_UNRESOLVED,
                internal_reason="PARSER_LIMIT_EXCEEDED",
            )


def _collect_target_records(
    node: Any,
    *,
    preview_id: str,
    depth: int,
    budget: _NodeBudget,
    found: list[Mapping[str, Any]],
    seen_ids: set[int],
    parent_dups: bool,
    dups_key: str | None,
) -> None:
    if depth > _MAX_JSON_DEPTH:
        return
    budget.consume()

    if isinstance(node, str):
        decoded = _try_decode_json_string(node)
        if decoded is not None:
            _collect_target_records(
                decoded,
                preview_id=preview_id,
                depth=depth + 1,
                budget=budget,
                found=found,
                seen_ids=seen_ids,
                parent_dups=parent_dups,
                dups_key=dups_key,
            )
        return

    if isinstance(node, Mapping):
        dups = node.get("dups")
        if isinstance(dups, Mapping):
            direct = dups.get(preview_id)
            if isinstance(direct, Mapping):
                _add_record(direct, found=found, seen_ids=seen_ids)
            for key, value in dups.items():
                key_s = key if isinstance(key, str) else None
                _collect_target_records(
                    value,
                    preview_id=preview_id,
                    depth=depth + 1,
                    budget=budget,
                    found=found,
                    seen_ids=seen_ids,
                    parent_dups=True,
                    dups_key=key_s,
                )

        video_id = node.get("videoId")
        if (video_id is not None and str(video_id) == preview_id) or (
            parent_dups and dups_key == preview_id
        ):
            _add_record(node, found=found, seen_ids=seen_ids)

        for key, value in node.items():
            if key == "dups":
                continue
            if isinstance(key, str) and key.lower() in {
                "related",
                "recommended",
                "recommendations",
                "similar",
                "playlist",
            }:
                continue
            _collect_target_records(
                value,
                preview_id=preview_id,
                depth=depth + 1,
                budget=budget,
                found=found,
                seen_ids=seen_ids,
                parent_dups=False,
                dups_key=None,
            )
        return

    if isinstance(node, list):
        for item in node:
            _collect_target_records(
                item,
                preview_id=preview_id,
                depth=depth + 1,
                budget=budget,
                found=found,
                seen_ids=seen_ids,
                parent_dups=parent_dups,
                dups_key=dups_key,
            )


def _add_record(
    record: Mapping[str, Any],
    *,
    found: list[Mapping[str, Any]],
    seen_ids: set[int],
) -> None:
    identity = id(record)
    if identity in seen_ids:
        return
    seen_ids.add(identity)
    found.append(record)


def _normalize_stable_provider_url(
    raw: str,
    *,
    supported_hosts: frozenset[str],
    allow_http_upgrade: bool,
) -> tuple[str | None, bool]:
    value = _decode_string_value(raw)
    if value is None:
        return None, False
    try:
        parts = urlsplit(value)
    except ValueError:
        return None, False

    scheme = (parts.scheme or "").lower()
    upgraded = False
    if scheme == "http":
        if not allow_http_upgrade:
            return None, False
        scheme = "https"
        upgraded = True
    elif scheme != "https":
        return None, False

    if parts.username is not None or parts.password is not None:
        return None, False
    if parts.port is not None:
        return None, False
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or host not in supported_hosts:
        return None, False

    path = parts.path or "/"
    path_lower = path.lower()
    if any(marker in path_lower for marker in _FORBIDDEN_PATH_MARKERS):
        return None, False
    canonical_path = _canonical_stable_provider_path(host, path)
    if canonical_path is None:
        return None, False

    return f"https://{host}{canonical_path}", upgraded


def _canonical_stable_provider_path(host: str, path: str) -> str | None:
    """Return trusted path form, or None when the path is not stable.

    VK ``/clip…``, Rutube ``/shorts/…``, and OK ``/videoembed/…`` aliases
    rewrite to ``/video…``.
    """
    if host in _VK_HOSTS:
        provider_id = ProviderID.VK.value
        allowed = _VK_HOSTS
    elif host in _RUTUBE_HOSTS:
        provider_id = ProviderID.RUTUBE.value
        allowed = _RUTUBE_HOSTS
    elif host in _OK_HOSTS:
        provider_id = ProviderID.OK.value
        allowed = _OK_HOSTS
    else:
        return None
    try:
        identity = parse_stable_provider_identity(
            provider_id=provider_id,
            hostname=host,
            path=path,
            allowed_hostnames=allowed,
        )
    except ProviderIdentityError:
        return None
    return identity.canonical_path


def _is_stable_provider_path(host: str, path: str) -> bool:
    return _canonical_stable_provider_path(host, path) is not None


def _decode_string_value(raw: str) -> str | None:
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value or len(value) > _MAX_CANDIDATE_URL_LEN:
        return None
    for _ in range(_MAX_DECODE_PASSES):
        prev = value
        value = html.unescape(value).strip()
        if "\\/" in value or "\\u" in value:
            try:
                loaded = json.loads(f'"{value}"')
                if isinstance(loaded, str):
                    value = loaded
            except (json.JSONDecodeError, TypeError):
                value = (
                    value.replace("\\/", "/")
                    .replace("\\u002F", "/")
                    .replace("\\u003A", ":")
                )
        if value == prev:
            break
    if not isinstance(value, str):
        return None
    value = value.strip()
    if len(value) > _MAX_CANDIDATE_URL_LEN:
        return None
    return value


def _try_decode_json_string(raw: str) -> Any | None:
    text = raw.strip()
    if len(text) < 2 or len(text) > _MAX_FRAGMENT_BYTES:
        return None
    if text[0] not in "{[\"'":
        return None
    for _ in range(_MAX_DECODE_PASSES):
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError:
            return None
        if isinstance(loaded, str):
            text = loaded.strip()
            continue
        if isinstance(loaded, Mapping | list):
            return loaded
        return None
    return None


def _collect_json_roots(text: str) -> list[Any]:
    roots: list[Any] = []
    for fragment in _iter_script_bodies(text):
        stripped = fragment.strip()
        if stripped[:1] in "{[":
            try:
                roots.append(json.loads(stripped))
                continue
            except json.JSONDecodeError:
                pass
        for obj_text in _iter_embedded_json_objects(stripped):
            try:
                roots.append(json.loads(obj_text))
            except json.JSONDecodeError:
                continue
            if len(roots) >= _MAX_SCRIPT_BLOCKS:
                return roots
    return roots


def _iter_script_bodies(text: str) -> Iterator[str]:
    count = 0
    pos = 0
    window = text[:_MAX_SCAN_WINDOW]
    while count < _MAX_SCRIPT_BLOCKS:
        open_match = _SCRIPT_OPEN_RE.search(window, pos)
        if open_match is None:
            break
        close_match = _SCRIPT_CLOSE_RE.search(window, open_match.end())
        if close_match is None:
            break
        body = window[open_match.end() : close_match.start()]
        pos = close_match.end()
        count += 1
        body_bytes = len(body.encode("utf-8", errors="replace"))
        if not body or body_bytes > _MAX_FRAGMENT_BYTES:
            continue
        yield body


def _iter_embedded_json_objects(text: str) -> Iterator[str]:
    emitted = 0
    i = 0
    n = min(len(text), _MAX_FRAGMENT_BYTES)
    while i < n and emitted < _MAX_EMBEDDED_OBJECTS:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        escape = False
        j = i
        while j < n:
            ch = text[j]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        chunk = text[i : j + 1]
                        if len(chunk) <= _MAX_FRAGMENT_BYTES:
                            yield chunk
                            emitted += 1
                        i = j + 1
                        break
            j += 1
        else:
            break


def provider_hosts_from_registry(exact_hostnames: Iterable[str]) -> frozenset[str]:
    """Normalize provider hostnames for resolver construction."""
    return frozenset(h.strip().lower().rstrip(".") for h in exact_hostnames if h)
