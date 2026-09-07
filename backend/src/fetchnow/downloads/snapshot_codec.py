"""Strict codec for durable selected-format snapshots (no tokens/URLs)."""

from __future__ import annotations

import json
import math
import uuid
from datetime import UTC, datetime
from typing import Any

from fetchnow.downloads.errors import (
    DownloadError,
    DownloadErrorCode,
    raise_download_error,
)
from fetchnow.media_inspection.models import (
    CodecFamily,
    FormatCategory,
    MediaFormat,
    MediaKind,
)
from fetchnow.quota.policy import EffectiveDownloadPolicy

_SNAPSHOT_KEYS = frozenset(
    {
        "formatOptionId",
        "container",
        "width",
        "height",
        "fps",
        "hasVideo",
        "hasAudio",
        "category",
        "videoCodec",
        "audioCodec",
        "approxBytes",
        "qualityLabel",
        "freeTierEligible",
        "mediaKind",
        "requiresPremium",
        "bitrateKbps",
    }
)
_LEGACY_SNAPSHOT_KEYS = _SNAPSHOT_KEYS - {
    "mediaKind",
    "requiresPremium",
    "bitrateKbps",
}
_POLICY_KEY = "effectiveDownloadPolicy"
_POLICY_KEYS = frozenset(
    {
        "tier",
        "downloadLimit",
        "quotaWindowSeconds",
        "deliveryRateBytesPerSecond",
        "premiumExpiresAt",
        "allowCombined",
        "allowAudioOnly",
        "allowVideoOnly",
        "authorizedIdentityId",
    }
)
_LEGACY_POLICY_KEYS = _POLICY_KEYS - {
    "allowCombined",
    "allowAudioOnly",
    "allowVideoOnly",
    "authorizedIdentityId",
}

_FORBIDDEN_KEYS = frozenset(
    {
        "url",
        "urls",
        "directUrl",
        "directUrls",
        "signedUrl",
        "downloadUrl",
        "streamUrl",
        "manifestUrl",
        "cookie",
        "cookies",
        "authorization",
        "token",
        "providerFormatToken",
        "provider_format_token",
        "accessToken",
        "password",
        "secret",
        "stderr",
        "stdout",
        "raw",
        "path",
        "filepath",
        "tempPath",
        "query",
        "fragment",
        "headers",
        "argv",
        "command",
    }
)

_DEFAULT_MAX_JSON_BYTES = 4096
_MAX_STRING_LEN = 64
_MAX_DIMENSION = 16_384
_MAX_FPS = 240.0
_MAX_APPROX_BYTES = 10**12
_MAX_DOWNLOAD_LIMIT = 100
_MAX_QUOTA_WINDOW_SECONDS = 604_800
_MAX_DELIVERY_RATE = 67_108_864


def _json_byte_size(payload: Any) -> int:
    return len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def _reject_keys(mapping: dict[str, Any]) -> None:
    for key in mapping:
        if not isinstance(key, str):
            raise_download_error(
                DownloadErrorCode.FORMAT_UNAVAILABLE,
                internal_reason="SNAPSHOT_KEY_TYPE",
            )
        lowered = key.casefold()
        if key in _FORBIDDEN_KEYS or lowered in {k.casefold() for k in _FORBIDDEN_KEYS}:
            raise_download_error(
                DownloadErrorCode.FORMAT_UNAVAILABLE,
                internal_reason="SNAPSHOT_FORBIDDEN_KEY",
            )
        if key not in _SNAPSHOT_KEYS and key != _POLICY_KEY:
            raise_download_error(
                DownloadErrorCode.FORMAT_UNAVAILABLE,
                internal_reason="SNAPSHOT_UNKNOWN_KEY",
            )


def _require_bool(value: Any, *, field: str) -> bool:
    del field
    if type(value) is not bool:
        raise_download_error(
            DownloadErrorCode.FORMAT_UNAVAILABLE,
            internal_reason="SNAPSHOT_BOOL",
        )
    return value


def _require_optional_int(value: Any, *, field: str, maximum: int) -> int | None:
    del field
    if value is None:
        return None
    if type(value) is not int or isinstance(value, bool):
        raise_download_error(
            DownloadErrorCode.FORMAT_UNAVAILABLE,
            internal_reason="SNAPSHOT_INT",
        )
    if value < 0 or value > maximum:
        raise_download_error(
            DownloadErrorCode.FORMAT_UNAVAILABLE,
            internal_reason="SNAPSHOT_INT_BOUNDS",
        )
    return value


def _require_optional_float(value: Any, *, field: str, maximum: float) -> float | None:
    del field
    if value is None:
        return None
    if type(value) is bool:
        raise_download_error(
            DownloadErrorCode.FORMAT_UNAVAILABLE,
            internal_reason="SNAPSHOT_FLOAT",
        )
    if type(value) is int:
        number = float(value)
    elif type(value) is float:
        number = value
    else:
        raise_download_error(
            DownloadErrorCode.FORMAT_UNAVAILABLE,
            internal_reason="SNAPSHOT_FLOAT",
        )
    if not math.isfinite(number) or number < 0 or number > maximum:
        raise_download_error(
            DownloadErrorCode.FORMAT_UNAVAILABLE,
            internal_reason="SNAPSHOT_FLOAT_BOUNDS",
        )
    return number


def _require_string(value: Any, *, field: str) -> str:
    del field
    if not isinstance(value, str) or not value or len(value) > _MAX_STRING_LEN:
        raise_download_error(
            DownloadErrorCode.FORMAT_UNAVAILABLE,
            internal_reason="SNAPSHOT_STRING",
        )
    if any(ord(ch) < 32 for ch in value):
        raise_download_error(
            DownloadErrorCode.FORMAT_UNAVAILABLE,
            internal_reason="SNAPSHOT_STRING",
        )
    return value


def encode_selected_format_snapshot(
    fmt: MediaFormat,
    *,
    max_bytes: int = _DEFAULT_MAX_JSON_BYTES,
) -> dict[str, object]:
    """Serialize bounded public format fields (no provider token)."""
    if not isinstance(fmt, MediaFormat):
        raise_download_error(
            DownloadErrorCode.INTERNAL_ERROR,
            internal_reason="SNAPSHOT_TYPE",
        )
    payload: dict[str, object] = {
        "formatOptionId": fmt.format_option_id,
        "container": fmt.container,
        "width": fmt.width,
        "height": fmt.height,
        "fps": fmt.fps,
        "hasVideo": fmt.has_video,
        "hasAudio": fmt.has_audio,
        "category": fmt.category.value,
        "videoCodec": fmt.video_codec.value,
        "audioCodec": fmt.audio_codec.value,
        "approxBytes": fmt.approx_bytes,
        "qualityLabel": fmt.quality_label,
        "freeTierEligible": fmt.free_tier_eligible,
        "mediaKind": fmt.media_kind.value,
        "requiresPremium": fmt.requires_premium,
        "bitrateKbps": fmt.bitrate_kbps,
    }
    if set(payload) != _SNAPSHOT_KEYS:
        raise_download_error(
            DownloadErrorCode.INTERNAL_ERROR,
            internal_reason="SNAPSHOT_ENCODE_KEYS",
        )
    size = _json_byte_size(payload)
    if size > max_bytes:
        raise_download_error(
            DownloadErrorCode.INTERNAL_ERROR,
            internal_reason="SNAPSHOT_TOO_LARGE",
        )
    return payload


def decode_selected_format_snapshot(
    payload: Any,
    *,
    expected_format_option_id: str,
    max_bytes: int = _DEFAULT_MAX_JSON_BYTES,
) -> MediaFormat:
    """Load a MediaFormat from a durable snapshot; reject unknown/coerced types."""
    if not isinstance(payload, dict):
        raise_download_error(
            DownloadErrorCode.FORMAT_UNAVAILABLE,
            internal_reason="SNAPSHOT_PAYLOAD_TYPE",
        )
    size = _json_byte_size(payload)
    if size > max_bytes:
        raise_download_error(
            DownloadErrorCode.FORMAT_UNAVAILABLE,
            internal_reason="SNAPSHOT_TOO_LARGE",
        )
    actual_keys = set(payload)
    allowed_key_sets = (
        _LEGACY_SNAPSHOT_KEYS,
        _LEGACY_SNAPSHOT_KEYS | {_POLICY_KEY},
        _SNAPSHOT_KEYS,
        _SNAPSHOT_KEYS | {_POLICY_KEY},
    )
    if actual_keys not in allowed_key_sets:
        _reject_keys(payload)
        if actual_keys not in allowed_key_sets:
            raise_download_error(
                DownloadErrorCode.FORMAT_UNAVAILABLE,
                internal_reason="SNAPSHOT_KEY_SET",
            )
    _reject_keys(payload)

    try:
        format_option_id = _require_string(
            payload["formatOptionId"], field="formatOptionId"
        )
        if format_option_id != expected_format_option_id:
            raise_download_error(
                DownloadErrorCode.FORMAT_UNAVAILABLE,
                internal_reason="SNAPSHOT_OPTION_MISMATCH",
            )
        category = FormatCategory(
            _require_string(payload["category"], field="category")
        )
        # A3.1 and older snapshots represented only ordinary finished A/V.
        # Missing A3.2 fields therefore mean NORMAL_VIDEO; never reinterpret
        # historical data as a newly authorized Premium capability.
        legacy_kind = MediaKind.NORMAL_VIDEO
        media_kind = MediaKind(
            _require_string(
                payload.get("mediaKind", legacy_kind.value), field="mediaKind"
            )
        )
        fmt = MediaFormat(
            format_option_id=format_option_id,
            container=_require_string(payload["container"], field="container"),
            width=_require_optional_int(
                payload["width"], field="width", maximum=_MAX_DIMENSION
            ),
            height=_require_optional_int(
                payload["height"], field="height", maximum=_MAX_DIMENSION
            ),
            fps=_require_optional_float(payload["fps"], field="fps", maximum=_MAX_FPS),
            has_video=_require_bool(payload["hasVideo"], field="hasVideo"),
            has_audio=_require_bool(payload["hasAudio"], field="hasAudio"),
            category=category,
            video_codec=CodecFamily(
                _require_string(payload["videoCodec"], field="videoCodec")
            ),
            audio_codec=CodecFamily(
                _require_string(payload["audioCodec"], field="audioCodec")
            ),
            approx_bytes=_require_optional_int(
                payload["approxBytes"],
                field="approxBytes",
                maximum=_MAX_APPROX_BYTES,
            ),
            quality_label=_require_string(
                payload["qualityLabel"], field="qualityLabel"
            ),
            free_tier_eligible=_require_bool(
                payload["freeTierEligible"], field="freeTierEligible"
            ),
            media_kind=media_kind,
            requires_premium=_require_bool(
                payload.get(
                    "requiresPremium", media_kind is not MediaKind.NORMAL_VIDEO
                ),
                field="requiresPremium",
            ),
            bitrate_kbps=_require_optional_int(
                payload.get("bitrateKbps"),
                field="bitrateKbps",
                maximum=100_000,
            ),
        )
    except DownloadError:
        raise
    except (KeyError, TypeError, ValueError):
        raise_download_error(
            DownloadErrorCode.FORMAT_UNAVAILABLE,
            internal_reason="SNAPSHOT_INVALID",
        )
    return fmt


def attach_effective_policy_snapshot(
    payload: dict[str, object],
    policy: EffectiveDownloadPolicy,
    *,
    authorized_identity_id: uuid.UUID | None = None,
) -> dict[str, object]:
    """Attach bounded server-generated policy data to a format snapshot."""
    decode_selected_format_snapshot(
        payload,
        expected_format_option_id=str(payload.get("formatOptionId") or ""),
    )
    expires_at = policy.premium_expires_at
    policy_payload: dict[str, object] = {
        "tier": policy.tier,
        "downloadLimit": policy.download_limit,
        "quotaWindowSeconds": policy.quota_window_seconds,
        "deliveryRateBytesPerSecond": policy.delivery_rate_bytes_per_second,
        "premiumExpiresAt": (
            expires_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
            if expires_at is not None
            else None
        ),
        "allowCombined": policy.allow_combined,
        "allowAudioOnly": policy.allow_audio_only,
        "allowVideoOnly": policy.allow_video_only,
        "authorizedIdentityId": (
            str(authorized_identity_id) if authorized_identity_id is not None else None
        ),
    }
    result = dict(payload)
    result[_POLICY_KEY] = policy_payload
    # Decode the result before persistence so encoder and decoder invariants agree.
    decode_effective_policy_snapshot(result)
    if _json_byte_size(result) > _DEFAULT_MAX_JSON_BYTES:
        raise_download_error(
            DownloadErrorCode.INTERNAL_ERROR,
            internal_reason="SNAPSHOT_TOO_LARGE",
        )
    return result


def decode_effective_policy_snapshot(
    payload: Any,
) -> EffectiveDownloadPolicy | None:
    """Decode an internal policy snapshot; ``None`` denotes a legacy job."""
    if not isinstance(payload, dict):
        raise_download_error(
            DownloadErrorCode.FORMAT_UNAVAILABLE,
            internal_reason="SNAPSHOT_PAYLOAD_TYPE",
        )
    raw = payload.get(_POLICY_KEY)
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) not in {
        _LEGACY_POLICY_KEYS,
        _POLICY_KEYS,
    }:
        raise_download_error(
            DownloadErrorCode.FORMAT_UNAVAILABLE,
            internal_reason="POLICY_SNAPSHOT_KEY_SET",
        )
    tier = raw.get("tier")
    limit = raw.get("downloadLimit")
    window = raw.get("quotaWindowSeconds")
    rate = raw.get("deliveryRateBytesPerSecond")
    expiry_raw = raw.get("premiumExpiresAt")
    legacy = set(raw) == _LEGACY_POLICY_KEYS
    allow_combined = True if legacy else raw.get("allowCombined")
    allow_audio_only = False if legacy else raw.get("allowAudioOnly")
    allow_video_only = False if legacy else raw.get("allowVideoOnly")
    authorized_identity_raw = None if legacy else raw.get("authorizedIdentityId")
    if any(
        type(value) is not bool
        for value in (allow_combined, allow_audio_only, allow_video_only)
    ):
        raise_download_error(
            DownloadErrorCode.FORMAT_UNAVAILABLE,
            internal_reason="POLICY_SNAPSHOT_INVALID",
        )
    if tier == "free":
        if (
            type(limit) is not int
            or not 1 <= limit <= _MAX_DOWNLOAD_LIMIT
            or type(window) is not int
            or not 60 <= window <= _MAX_QUOTA_WINDOW_SECONDS
            or (
                rate is not None
                and (type(rate) is not int or not 262_144 <= rate <= _MAX_DELIVERY_RATE)
            )
            or expiry_raw is not None
            or authorized_identity_raw is not None
            or allow_combined is not True
            or allow_audio_only is not False
            or allow_video_only is not False
        ):
            raise_download_error(
                DownloadErrorCode.FORMAT_UNAVAILABLE,
                internal_reason="POLICY_SNAPSHOT_INVALID",
            )
        return EffectiveDownloadPolicy(
            tier="free",
            download_limit=limit,
            quota_window_seconds=window,
            delivery_rate_bytes_per_second=rate,
            premium_expires_at=None,
            allow_combined=True,
            allow_audio_only=False,
            allow_video_only=False,
        )
    if tier == "premium":
        if (
            limit is not None
            or window is not None
            or rate is not None
            or allow_combined is not True
        ):
            raise_download_error(
                DownloadErrorCode.FORMAT_UNAVAILABLE,
                internal_reason="POLICY_SNAPSHOT_INVALID",
            )
        if not isinstance(expiry_raw, str) or len(expiry_raw) > _MAX_STRING_LEN:
            raise_download_error(
                DownloadErrorCode.FORMAT_UNAVAILABLE,
                internal_reason="POLICY_SNAPSHOT_INVALID",
            )
        try:
            expires_at = datetime.fromisoformat(expiry_raw.replace("Z", "+00:00"))
        except ValueError:
            raise_download_error(
                DownloadErrorCode.FORMAT_UNAVAILABLE,
                internal_reason="POLICY_SNAPSHOT_INVALID",
            )
        if expires_at.tzinfo is None:
            raise_download_error(
                DownloadErrorCode.FORMAT_UNAVAILABLE,
                internal_reason="POLICY_SNAPSHOT_INVALID",
            )
        if not legacy:
            try:
                uuid.UUID(str(authorized_identity_raw))
            except (TypeError, ValueError, AttributeError):
                raise_download_error(
                    DownloadErrorCode.FORMAT_UNAVAILABLE,
                    internal_reason="POLICY_SNAPSHOT_INVALID",
                )
        return EffectiveDownloadPolicy(
            tier="premium",
            download_limit=None,
            quota_window_seconds=None,
            delivery_rate_bytes_per_second=None,
            premium_expires_at=expires_at,
            allow_combined=True,
            allow_audio_only=bool(allow_audio_only),
            allow_video_only=bool(allow_video_only),
        )
    raise_download_error(
        DownloadErrorCode.FORMAT_UNAVAILABLE,
        internal_reason="POLICY_SNAPSHOT_INVALID",
    )


def strip_effective_policy_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    """Return only the immutable format portion for idempotency comparison."""
    return {key: value for key, value in payload.items() if key != _POLICY_KEY}


def decode_authorized_identity_id(payload: Any) -> uuid.UUID | None:
    """Return the server-bound Premium identity, absent for Free/legacy jobs."""
    policy = decode_effective_policy_snapshot(payload)
    if policy is None or policy.tier != "premium":
        return None
    raw = payload.get(_POLICY_KEY)
    if not isinstance(raw, dict):
        return None
    value = raw.get("authorizedIdentityId")
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise_download_error(
            DownloadErrorCode.FORMAT_UNAVAILABLE,
            internal_reason="POLICY_SNAPSHOT_INVALID",
        )
