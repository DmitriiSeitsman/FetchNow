"""Parse yt-dlp JSON into ExtractedMediaDraft without retaining media URLs."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any

from fetchnow.media_inspection.errors import (
    InspectionErrorKind,
    raise_inspection_error,
)
from fetchnow.media_inspection.identity import parse_identity_from_url
from fetchnow.media_inspection.models import (
    CodecFamily,
    ExtractedMediaDraft,
    InternalFormatCandidate,
)
from fetchnow.media_inspection.normalize import normalize_codec
from fetchnow.media_inspection.size_estimate import estimate_format_bytes

_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_CONTAINER_RE = re.compile(r"^[a-z0-9]{1,16}$")
_PROTOCOL_RE = re.compile(r"^[a-z0-9_]{1,32}$")

# Structural limits after JSON decode (stdout byte limit is not enough).
_DEFAULT_MAX_JSON_DEPTH = 12
_DEFAULT_MAX_JSON_NODES = 8_192
_DEFAULT_MAX_MAPPING_KEYS = 256
_DEFAULT_MAX_LIST_LEN = 512
_DEFAULT_MAX_STRING_LEN = 8_192
_DEFAULT_MAX_FORMAT_ENTRIES = 256


def _reject_playlist(payload: Mapping[str, Any]) -> None:
    if payload.get("_type") == "playlist":
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_POLICY_REJECTED,
            internal_reason="PLAYLIST_REJECTED",
        )
    if isinstance(payload.get("entries"), list):
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_POLICY_REJECTED,
            internal_reason="PLAYLIST_ENTRIES",
        )


def _assert_json_structure(
    root: object,
    *,
    max_depth: int,
    max_nodes: int,
    max_mapping_keys: int,
    max_list_len: int,
    max_string_len: int,
) -> None:
    nodes = 0

    def walk(value: object, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > max_nodes:
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_INVALID_OUTPUT,
                internal_reason="JSON_NODE_LIMIT",
            )
        if depth > max_depth:
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_INVALID_OUTPUT,
                internal_reason="JSON_DEPTH_LIMIT",
            )
        if isinstance(value, str):
            if len(value) > max_string_len:
                raise_inspection_error(
                    InspectionErrorKind.INSPECTION_INVALID_OUTPUT,
                    internal_reason="JSON_STRING_LIMIT",
                )
            return
        if isinstance(value, dict):
            if len(value) > max_mapping_keys:
                raise_inspection_error(
                    InspectionErrorKind.INSPECTION_INVALID_OUTPUT,
                    internal_reason="JSON_KEYS_LIMIT",
                )
            for key, child in value.items():
                if not isinstance(key, str) or len(key) > max_string_len:
                    raise_inspection_error(
                        InspectionErrorKind.INSPECTION_INVALID_OUTPUT,
                        internal_reason="JSON_KEY_LIMIT",
                    )
                walk(child, depth + 1)
            return
        if isinstance(value, list):
            if len(value) > max_list_len:
                raise_inspection_error(
                    InspectionErrorKind.INSPECTION_INVALID_OUTPUT,
                    internal_reason="JSON_LIST_LIMIT",
                )
            for child in value:
                walk(child, depth + 1)
            return
        if value is None or isinstance(value, int | float | bool):
            if isinstance(value, float) and not math.isfinite(value):
                raise_inspection_error(
                    InspectionErrorKind.INSPECTION_INVALID_OUTPUT,
                    internal_reason="JSON_NONFINITE",
                )
            return
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_INVALID_OUTPUT,
            internal_reason="JSON_UNSUPPORTED_TYPE",
        )

    try:
        walk(root, 1)
    except RecursionError:
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_INVALID_OUTPUT,
            internal_reason="JSON_RECURSION",
        )


def _extractor_key(
    payload: Mapping[str, Any],
    *,
    allowed_extractor_keys: frozenset[str] | set[str],
) -> str:
    """Resolve yt-dlp extractor identity from payload fields.

    Some IEs emit distinct ``extractor`` (IE_NAME) and ``extractor_key``
    (ie_key) labels — e.g. Dzen: ``dzen.ru`` vs ``ZenYandex``. When labels
    disagree, accept exactly one allowlisted hit and ignore sibling labels
    outside the provider allowlist. Any generic label still fails closed.
    """
    raw_key = payload.get("extractor_key")
    raw_name = payload.get("extractor")
    values: list[str] = []
    for value in (raw_key, raw_name):
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_INVALID_OUTPUT,
                internal_reason="EXTRACTOR_FIELD_INVALID",
            )
        values.append(value.strip().lower())
    if not values:
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_INVALID_OUTPUT,
            internal_reason="MISSING_EXTRACTOR_KEY",
        )
    if any(
        value in {"generic", "default"} or value.startswith("generic")
        for value in values
    ):
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH,
            internal_reason="GENERIC_EXTRACTOR",
        )
    unique = list(dict.fromkeys(values))
    allowed = {k.lower() for k in allowed_extractor_keys}
    if len(unique) == 1:
        key = unique[0]
    else:
        hits = [value for value in unique if value in allowed]
        if len(set(hits)) != 1:
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH,
                internal_reason="EXTRACTOR_FIELD_CONFLICT",
            )
        key = hits[0]
    return key


def _payload_media_id(payload: Mapping[str, Any]) -> str:
    for key in ("id", "display_id"):
        value = payload.get(key)
        if isinstance(value, str) and _SAFE_ID.fullmatch(value):
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            text = str(value)
            if _SAFE_ID.fullmatch(text):
                return text
    raise_inspection_error(
        InspectionErrorKind.INSPECTION_INVALID_OUTPUT,
        internal_reason="MISSING_MEDIA_ID",
    )


def _finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return float(value) if value >= 0 else None
    if isinstance(value, float):
        if not math.isfinite(value) or value < 0:
            return None
        return value
    return None


def _int_dim(value: Any, *, maximum: int) -> int | None:
    number = _finite_number(value)
    if number is None:
        return None
    as_int = int(number)
    if as_int > maximum:
        return None
    return as_int


def _bind_result_identity(
    payload: Mapping[str, Any],
    *,
    expected_provider_id: str,
    expected_media_id: str,
    allowed_hostnames: frozenset[str],
) -> None:
    """Require authoritative URLs and payload id to match the trusted identity."""
    identities = []
    present = False
    for url_key in ("webpage_url", "original_url"):
        value = payload.get(url_key)
        if value is None:
            continue
        present = True
        if not isinstance(value, str):
            raise_inspection_error(
                InspectionErrorKind.INSPECTION_INVALID_OUTPUT,
                internal_reason="IDENTITY_URL_TYPE",
            )
        identities.append(
            parse_identity_from_url(
                value,
                provider_id=expected_provider_id,
                allowed_hostnames=allowed_hostnames,
            )
        )
    if not present or not identities:
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH,
            internal_reason="MISSING_AUTHORITATIVE_IDENTITY",
        )
    media_ids = {identity.media_id for identity in identities}
    if len(media_ids) != 1:
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH,
            internal_reason="CONFLICTING_IDENTITY_FIELDS",
        )
    result_media_id = next(iter(media_ids))
    if result_media_id != expected_media_id:
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH,
            internal_reason="MEDIA_IDENTITY_MISMATCH",
        )
    payload_id = _payload_media_id(payload)
    # Provider contracts: payload id must equal stable media identity.
    if payload_id != expected_media_id:
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH,
            internal_reason="PAYLOAD_ID_MISMATCH",
        )


def _parse_formats(
    payload: Mapping[str, Any],
    *,
    max_height: int,
    max_width: int,
    max_bytes: int,
    max_format_entries: int,
    duration_seconds: int | None,
) -> tuple[InternalFormatCandidate, ...]:
    raw_formats = payload.get("formats")
    if raw_formats is None:
        raw_formats = [payload]
    if not isinstance(raw_formats, list):
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_INVALID_OUTPUT,
            internal_reason="FORMATS_NOT_LIST",
        )
    if len(raw_formats) > max_format_entries:
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_INVALID_OUTPUT,
            internal_reason="FORMAT_ENTRY_LIMIT",
        )

    candidates: list[InternalFormatCandidate] = []
    for item in raw_formats:
        if not isinstance(item, dict):
            continue
        # Ignore url / fragment_base_url / manifest_url / http_headers.
        ext = item.get("ext")
        if not isinstance(ext, str):
            continue
        container = ext.strip().lower()
        if not _CONTAINER_RE.fullmatch(container):
            continue

        vcodec_raw = item.get("vcodec") if isinstance(item.get("vcodec"), str) else None
        acodec_raw = item.get("acodec") if isinstance(item.get("acodec"), str) else None
        video_codec = normalize_codec(vcodec_raw, kind="video")
        audio_codec = normalize_codec(acodec_raw, kind="audio")
        has_video = video_codec is not CodecFamily.NONE
        has_audio = audio_codec is not CodecFamily.NONE

        width = _int_dim(item.get("width"), maximum=max_width)
        height = _int_dim(item.get("height"), maximum=max_height)
        if (width is not None or height is not None) and not has_video:
            has_video = True
            video_codec = CodecFamily.UNKNOWN
        if has_video and width is None and height is None:
            continue
        if not has_video and not has_audio:
            continue

        fps_num = _finite_number(item.get("fps"))
        if fps_num is not None and fps_num > 240:
            continue
        fps = None if fps_num is None else float(fps_num)

        approx_bytes = estimate_format_bytes(
            filesize=item.get("filesize"),
            filesize_approx=item.get("filesize_approx"),
            tbr=item.get("tbr"),
            vbr=item.get("vbr"),
            abr=item.get("abr"),
            has_video=has_video,
            has_audio=has_audio,
            duration_seconds=duration_seconds,
        )
        if approx_bytes is not None and approx_bytes > max_bytes:
            continue

        token = item.get("format_id")
        provider_token = (
            token if isinstance(token, str) and 0 < len(token) <= 64 else None
        )
        protocol_raw = item.get("protocol")
        protocol: str | None = None
        if isinstance(protocol_raw, str):
            proto = protocol_raw.strip().lower()
            if _PROTOCOL_RE.fullmatch(proto):
                protocol = proto
        has_drm = item.get("has_drm") is True
        candidates.append(
            InternalFormatCandidate(
                container=container,
                width=width,
                height=height,
                fps=fps,
                has_video=has_video,
                has_audio=has_audio,
                video_codec=video_codec,
                audio_codec=audio_codec,
                approx_bytes=approx_bytes,
                provider_format_token=provider_token,
                protocol=protocol,
                has_drm=has_drm,
            )
        )
    return tuple(candidates)


def parse_ytdlp_json(
    raw: bytes,
    *,
    expected_provider_id: str,
    expected_canonical_url: str,
    expected_media_id: str,
    allowed_extractor_keys: frozenset[str],
    allowed_hostnames: frozenset[str],
    max_height: int,
    max_width: int,
    max_bytes: int,
    max_duration: int,
    tool_version: str | None,
    max_json_depth: int = _DEFAULT_MAX_JSON_DEPTH,
    max_json_nodes: int = _DEFAULT_MAX_JSON_NODES,
    max_mapping_keys: int = _DEFAULT_MAX_MAPPING_KEYS,
    max_list_len: int = _DEFAULT_MAX_LIST_LEN,
    max_string_len: int = _DEFAULT_MAX_STRING_LEN,
    max_format_entries: int = _DEFAULT_MAX_FORMAT_ENTRIES,
) -> ExtractedMediaDraft:
    """Parse stdout JSON. Raw bytes never leave this function into errors."""
    if not raw:
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_INVALID_OUTPUT,
            internal_reason="EMPTY_STDOUT",
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_INVALID_OUTPUT,
            internal_reason="STDOUT_NOT_UTF8",
        )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_INVALID_OUTPUT,
            internal_reason="STDOUT_NOT_JSON",
        )
    except RecursionError:
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_INVALID_OUTPUT,
            internal_reason="JSON_RECURSION",
        )
    if not isinstance(payload, dict):
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_INVALID_OUTPUT,
            internal_reason="STDOUT_NOT_OBJECT",
        )

    _assert_json_structure(
        payload,
        max_depth=max_json_depth,
        max_nodes=max_json_nodes,
        max_mapping_keys=max_mapping_keys,
        max_list_len=max_list_len,
        max_string_len=max_string_len,
    )
    _reject_playlist(payload)
    allowed = {k.lower() for k in allowed_extractor_keys}
    extractor = _extractor_key(payload, allowed_extractor_keys=allowed)
    if extractor not in allowed:
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH,
            internal_reason="EXTRACTOR_NOT_ALLOWED",
        )

    _bind_result_identity(
        payload,
        expected_provider_id=expected_provider_id,
        expected_media_id=expected_media_id,
        allowed_hostnames=allowed_hostnames,
    )

    duration_raw = _finite_number(payload.get("duration"))
    if duration_raw is not None and duration_raw > max_duration:
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_POLICY_REJECTED,
            internal_reason="DURATION_TOO_LONG",
        )
    duration = None if duration_raw is None else int(duration_raw)

    title = payload.get("title")
    title_str = title if isinstance(title, str) else None

    candidates = _parse_formats(
        payload,
        max_height=max_height,
        max_width=max_width,
        max_bytes=max_bytes,
        max_format_entries=max_format_entries,
        duration_seconds=duration,
    )
    if not candidates:
        raise_inspection_error(
            InspectionErrorKind.INSPECTION_MEDIA_UNAVAILABLE,
            internal_reason="NO_FORMAT_CANDIDATES",
        )

    # Canonical always comes from the trusted target — never from extractor URLs.
    return ExtractedMediaDraft(
        provider_id=expected_provider_id,
        canonical_provider_url=expected_canonical_url,
        media_id=expected_media_id,
        title=title_str,
        duration_seconds=duration,
        candidates=candidates,
        extractor_key=extractor,
        tool_version=tool_version,
    )
