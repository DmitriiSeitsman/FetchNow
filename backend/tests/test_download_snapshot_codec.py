"""Strict selected-format snapshot codec tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fetchnow.downloads.errors import DownloadError, DownloadErrorCode
from fetchnow.downloads.snapshot_codec import (
    attach_effective_policy_snapshot,
    decode_effective_policy_snapshot,
    decode_selected_format_snapshot,
    encode_selected_format_snapshot,
)
from fetchnow.media_inspection.models import (
    CodecFamily,
    FormatCategory,
    MediaFormat,
)
from fetchnow.quota.policy import EffectiveDownloadPolicy


def _format(**overrides: object) -> MediaFormat:
    base = dict(
        format_option_id="fmt_abc123def4567890abcdef123456",
        container="mp4",
        width=1280,
        height=720,
        fps=30.0,
        has_video=True,
        has_audio=True,
        category=FormatCategory.PROGRESSIVE,
        video_codec=CodecFamily.AVC,
        audio_codec=CodecFamily.AAC,
        approx_bytes=1_000_000,
        quality_label="p720",
        free_tier_eligible=True,
    )
    base.update(overrides)
    return MediaFormat(**base)  # type: ignore[arg-type]


def _payload(**overrides: object) -> dict[str, object]:
    encoded = encode_selected_format_snapshot(_format())
    encoded.update(overrides)
    return encoded


def test_roundtrip_encode_decode() -> None:
    fmt = _format()
    payload = encode_selected_format_snapshot(fmt)
    decoded = decode_selected_format_snapshot(
        payload, expected_format_option_id=fmt.format_option_id
    )
    assert decoded.format_option_id == fmt.format_option_id
    assert decoded.has_video is True
    assert decoded.free_tier_eligible is True


def test_string_false_for_has_video_rejected() -> None:
    payload = _payload(hasVideo="false")
    with pytest.raises(DownloadError) as exc:
        decode_selected_format_snapshot(
            payload, expected_format_option_id=str(payload["formatOptionId"])
        )
    assert exc.value.code == DownloadErrorCode.FORMAT_UNAVAILABLE


def test_exact_bool_required_for_flags() -> None:
    for field, bad in (
        ("hasVideo", 1),
        ("hasAudio", 0),
        ("freeTierEligible", "true"),
    ):
        payload = _payload(**{field: bad})
        with pytest.raises(DownloadError) as exc:
            decode_selected_format_snapshot(
                payload, expected_format_option_id=str(payload["formatOptionId"])
            )
        assert exc.value.code == DownloadErrorCode.FORMAT_UNAVAILABLE


def test_unknown_keys_rejected() -> None:
    payload = _payload(extraField="nope")
    with pytest.raises(DownloadError) as exc:
        decode_selected_format_snapshot(
            payload, expected_format_option_id=str(payload["formatOptionId"])
        )
    assert exc.value.code == DownloadErrorCode.FORMAT_UNAVAILABLE


def test_option_id_mismatch_rejected() -> None:
    payload = _payload()
    with pytest.raises(DownloadError) as exc:
        decode_selected_format_snapshot(
            payload, expected_format_option_id="fmt_other_option_id_xxxxx"
        )
    assert exc.value.code == DownloadErrorCode.FORMAT_UNAVAILABLE


def test_internal_free_policy_roundtrip_persists_rate() -> None:
    payload = attach_effective_policy_snapshot(
        encode_selected_format_snapshot(_format()),
        EffectiveDownloadPolicy(
            tier="free",
            download_limit=3,
            quota_window_seconds=86_400,
            delivery_rate_bytes_per_second=524_288,
            premium_expires_at=None,
        ),
    )
    policy = decode_effective_policy_snapshot(payload)
    assert policy is not None
    assert policy.tier == "free"
    assert policy.delivery_rate_bytes_per_second == 524_288
    assert payload["effectiveDownloadPolicy"]["deliveryRateBytesPerSecond"] == 524_288


def test_internal_premium_policy_roundtrip_is_not_in_public_format() -> None:
    expires_at = datetime(2026, 9, 4, tzinfo=UTC)
    payload = attach_effective_policy_snapshot(
        encode_selected_format_snapshot(_format()),
        EffectiveDownloadPolicy(
            tier="premium",
            download_limit=None,
            quota_window_seconds=None,
            delivery_rate_bytes_per_second=None,
            premium_expires_at=expires_at,
        ),
    )
    policy = decode_effective_policy_snapshot(payload)
    assert policy is not None
    assert policy.tier == "premium"
    assert policy.delivery_rate_bytes_per_second is None
    assert policy.premium_expires_at == expires_at
    decoded = decode_selected_format_snapshot(
        payload, expected_format_option_id=_format().format_option_id
    )
    assert "effectiveDownloadPolicy" not in encode_selected_format_snapshot(decoded)


def test_malformed_internal_policy_fails_closed() -> None:
    payload = encode_selected_format_snapshot(_format())
    payload["effectiveDownloadPolicy"] = {
        "tier": "premium",
        "downloadLimit": None,
        "quotaWindowSeconds": None,
        "deliveryRateBytesPerSecond": 0,
        "premiumExpiresAt": "2026-09-04T00:00:00Z",
    }
    with pytest.raises(DownloadError):
        decode_effective_policy_snapshot(payload)
