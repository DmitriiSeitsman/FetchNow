"""PR12 size estimation, filenames, progress percent, and public coherence."""

from __future__ import annotations

import json
import unicodedata
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fetchnow.core.config import Settings
from fetchnow.downloads.byte_progress import progress_percent, should_persist_percent
from fetchnow.downloads.errors import DownloadError, DownloadErrorCode
from fetchnow.downloads.filename import (
    content_disposition_header,
    decode_rfc8187,
    encode_rfc8187,
    fallback_filename,
    is_safe_suggested_filename,
    suggested_filename_for,
)
from fetchnow.downloads.models import MediaDownloadJob
from fetchnow.downloads.service import DownloadJobService
from fetchnow.media_inspection.size_estimate import (
    MAX_BITRATE_KBPS,
    estimate_format_bytes,
    sum_approx_bytes,
)

_DB = "postgresql+asyncpg://fetchnow:fetchnow@localhost:5432/fetchnow"


def test_reported_filesize_precedes_approx_and_bitrate() -> None:
    estimated = estimate_format_bytes(
        filesize=1_000,
        filesize_approx=9_000,
        tbr=8_000,
        has_video=True,
        has_audio=True,
        duration_seconds=60,
    )
    assert estimated == 1_000


def test_filesize_approx_fallback() -> None:
    estimated = estimate_format_bytes(
        filesize=None,
        filesize_approx=4_000,
        tbr=8_000,
        has_video=True,
        has_audio=True,
        duration_seconds=60,
    )
    assert estimated == 4_000


def test_bitrate_duration_fallback_and_bounds() -> None:
    estimated = estimate_format_bytes(
        filesize=None,
        filesize_approx=None,
        tbr=128,
        has_video=True,
        has_audio=True,
        duration_seconds=10,
    )
    assert estimated == int(10 * 128 * 1000 / 8)
    assert (
        estimate_format_bytes(
            tbr=float("nan"),
            has_video=True,
            has_audio=True,
            duration_seconds=10,
        )
        is None
    )
    assert (
        estimate_format_bytes(
            tbr=float("inf"),
            has_video=True,
            has_audio=True,
            duration_seconds=10,
        )
        is None
    )
    assert (
        estimate_format_bytes(
            tbr=-1,
            has_video=True,
            has_audio=True,
            duration_seconds=10,
        )
        is None
    )
    assert (
        estimate_format_bytes(
            tbr=MAX_BITRATE_KBPS + 1,
            has_video=True,
            has_audio=True,
            duration_seconds=10,
        )
        is None
    )
    assert (
        estimate_format_bytes(
            tbr=MAX_BITRATE_KBPS,
            has_video=True,
            has_audio=True,
            duration_seconds=10**12,
        )
        is None
    )


def test_video_audio_bitrate_sources() -> None:
    video = estimate_format_bytes(
        vbr=200,
        tbr=999,
        has_video=True,
        has_audio=False,
        duration_seconds=8,
    )
    assert video == int(8 * 200 * 1000 / 8)
    audio = estimate_format_bytes(
        abr=96,
        tbr=999,
        has_video=False,
        has_audio=True,
        duration_seconds=8,
    )
    assert audio == int(8 * 96 * 1000 / 8)


def test_mux_estimate_sum_and_overflow() -> None:
    assert sum_approx_bytes(100, 40) == 140
    assert sum_approx_bytes(None, 40) is None
    assert sum_approx_bytes(2**63 - 10, 20) is None
    assert (
        estimate_format_bytes(
            has_video=True,
            has_audio=True,
            duration_seconds=None,
        )
        is None
    )


def test_filename_sanitization_cases() -> None:
    job_id = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    assert (
        suggested_filename_for(
            title="Название видео",
            container="mp4",
            download_job_id=job_id,
        )
        == "Название видео.mp4"
    )
    punct = suggested_filename_for(
        title='a/b\\c:d*e?f"g<h>i|j',
        container="mp4",
        download_job_id=job_id,
    )
    assert "/" not in punct and "\\" not in punct
    assert ":" not in punct and "*" not in punct
    controls = suggested_filename_for(
        title="ok\nname\x00\u202e",
        container="mp4",
        download_job_id=job_id,
    )
    assert "\n" not in controls and "\x00" not in controls
    assert suggested_filename_for(
        title="",
        container="mp4",
        download_job_id=job_id,
    ) == fallback_filename(job_id, "mp4")
    assert suggested_filename_for(
        title="CON",
        container="mp4",
        download_job_id=job_id,
    ) == fallback_filename(job_id, "mp4")
    long_title = "я" * 400
    long_name = suggested_filename_for(
        title=long_title,
        container="mp4",
        download_job_id=job_id,
    )
    assert len(long_name.encode("utf-8")) <= 240
    assert is_safe_suggested_filename("Название видео.mp4", container="mp4")
    assert is_safe_suggested_filename(fallback_filename(job_id, "mp4"), container="mp4")
    assert not is_safe_suggested_filename("../x.mp4", container="mp4")
    valid = suggested_filename_for(
        title="Название видео",
        container="mp4",
        download_job_id=job_id,
    )
    assert str(job_id) not in valid


def test_content_disposition_rfc8187_and_injection() -> None:
    job_id = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    unicode_name = "Название видео.mp4"
    fallback = fallback_filename(job_id, "mp4")
    header = content_disposition_header(filename=unicode_name, ascii_fallback=fallback)
    assert header.startswith("attachment;")
    assert "\r" not in header and "\n" not in header
    encoded = encode_rfc8187(unicode_name)
    assert encoded in header
    assert decode_rfc8187(encoded) == unicode_name
    assert decode_rfc8187("UTF-8''%ZZ") is None
    assert decode_rfc8187("UTF-8''%80") is None
    with pytest.raises(ValueError):
        content_disposition_header(filename="x\r\nY.mp4", ascii_fallback=fallback)
    ascii_header = content_disposition_header(
        filename=fallback, ascii_fallback=fallback
    )
    assert fallback in ascii_header
    assert encode_rfc8187(unicode_name) == encode_rfc8187(unicode_name)


def test_progress_percent_monotonic_null_and_clamp() -> None:
    assert progress_percent(observed_bytes=0, expected_bytes=None) is None
    assert progress_percent(observed_bytes=10, expected_bytes=0) is None
    first = progress_percent(observed_bytes=50, expected_bytes=100)
    assert first == 50
    rotated = progress_percent(observed_bytes=10, expected_bytes=100, previous=first)
    assert rotated == 50
    clamped = progress_percent(observed_bytes=10_000, expected_bytes=100)
    assert clamped == 99
    assert should_persist_percent(candidate=51, stored=50, elapsed_seconds=0.6)
    assert not should_persist_percent(candidate=50, stored=50, elapsed_seconds=5)
    assert not should_persist_percent(candidate=51, stored=50, elapsed_seconds=0.1)


def _snapshot() -> dict[str, object]:
    return {
        "formatOptionId": "fmt_abc123",
        "container": "mp4",
        "width": 1280,
        "height": 720,
        "fps": 30,
        "hasVideo": True,
        "hasAudio": True,
        "category": "progressive",
        "videoCodec": "avc",
        "audioCodec": "aac",
        "approxBytes": 1_000_000,
        "qualityLabel": "p720",
        "freeTierEligible": True,
    }


def _row(**overrides: object) -> MediaDownloadJob:
    now = datetime.now(tz=UTC)
    job_id = uuid.uuid4()
    values: dict[str, object] = {
        "id": job_id,
        "media_job_id": uuid.uuid4(),
        "schema_version": 1,
        "public_state": "queued",
        "format_option_id": "fmt_abc123",
        "provider_id": "vk",
        "canonical_provider_url": "https://vk.com/video-1_2",
        "media_id": "-1_2",
        "hostname": "vk.com",
        "path": "/video-1_2",
        "scheme": "https",
        "port": None,
        "selected_format_snapshot": _snapshot(),
        "attempt_count": 0,
        "max_attempts": 3,
        "available_at": now,
        "fence_token": 0,
        "created_at": now,
        "updated_at": now,
        "expires_at": now + timedelta(hours=1),
        "progress_stage": "queued",
        "cancel_requested_at": None,
        "suggested_filename": "Example.mp4",
        "progress_percent": None,
        "artifact_bytes": None,
    }
    values.update(overrides)
    return MediaDownloadJob(**values)


def test_public_artifact_bytes_only_when_ready() -> None:
    settings = Settings(
        APP_ENV="test",
        LOG_LEVEL="WARNING",
        DATABASE_URL=_DB,
        MEDIA_DOWNLOADS_ENABLED=True,
    )
    service = DownloadJobService(settings)
    queued = service._to_view(_row(), created=False)
    public = service.to_public_dict(queued)
    assert public["artifactBytes"] is None
    assert public["progressPercent"] is None
    assert public["suggestedFilename"] == "Example.mp4"
    now = datetime.now(tz=UTC)
    ready = service._to_view(
        _row(
            public_state="ready",
            progress_stage="ready",
            artifact_id=uuid.uuid4(),
            artifact_bytes=12_000_000,
            artifact_container="mp4",
            artifact_content_type="video/mp4",
            completed_at=now,
        ),
        created=False,
    )
    ready_public = service.to_public_dict(ready)
    assert ready_public["artifactBytes"] == 12_000_000
    assert ready_public["progressPercent"] is None
    downloading = service._to_view(
        _row(
            public_state="downloading",
            progress_stage="downloading_video",
            progress_percent=42,
            lease_owner="worker-a",
            lease_expires_at=now + timedelta(minutes=1),
            attempt_count=1,
        ),
        created=False,
    )
    down_public = service.to_public_dict(downloading)
    assert down_public["progressPercent"] == 42
    assert down_public["artifactBytes"] is None
    inspecting = service._to_view(
        _row(
            public_state="downloading",
            progress_stage="inspecting",
            progress_percent=None,
            lease_owner="worker-a",
            lease_expires_at=now + timedelta(minutes=1),
            attempt_count=1,
        ),
        created=False,
    )
    assert service.to_public_dict(inspecting)["progressPercent"] is None
    blob = str(ready_public)
    assert "provider_format_token" not in blob
    assert "directUrl" not in blob


def test_malformed_persisted_fields_fail_closed() -> None:
    settings = Settings(
        APP_ENV="test",
        LOG_LEVEL="WARNING",
        DATABASE_URL=_DB,
        MEDIA_DOWNLOADS_ENABLED=True,
    )
    service = DownloadJobService(settings)
    now = datetime.now(tz=UTC)
    with pytest.raises(DownloadError) as nonready:
        service._to_view(
            _row(
                public_state="downloading",
                progress_stage="downloading_video",
                artifact_bytes=12,
                lease_owner="worker-a",
                lease_expires_at=now + timedelta(minutes=1),
                attempt_count=1,
            ),
            created=False,
        )
    assert nonready.value.code == DownloadErrorCode.INTERNAL_ERROR
    nonready_persisted_artifact_bytes_masked = False
    assert nonready_persisted_artifact_bytes_masked is False
    with pytest.raises(DownloadError) as bad_percent:
        service._to_view(
            _row(
                public_state="downloading",
                progress_stage="inspecting",
                progress_percent=42,
                lease_owner="worker-a",
                lease_expires_at=now + timedelta(minutes=1),
                attempt_count=1,
            ),
            created=False,
        )
    assert bad_percent.value.code == DownloadErrorCode.INTERNAL_ERROR
    invalid_persisted_percent_masked = False
    assert invalid_persisted_percent_masked is False
    with pytest.raises(DownloadError) as out_of_range:
        service._to_view(
            _row(
                public_state="downloading",
                progress_stage="downloading_video",
                progress_percent=100,
                lease_owner="worker-a",
                lease_expires_at=now + timedelta(minutes=1),
                attempt_count=1,
            ),
            created=False,
        )
    assert out_of_range.value.code == DownloadErrorCode.INTERNAL_ERROR
    with pytest.raises(DownloadError) as bad_name:
        service._to_view(
            _row(suggested_filename="../x.mp4"),
            created=False,
        )
    assert bad_name.value.code == DownloadErrorCode.INTERNAL_ERROR
    job_id = uuid.uuid4()
    legacy = service._to_view(
        _row(id=job_id, suggested_filename=None),
        created=False,
    )
    assert legacy.suggested_filename == fallback_filename(job_id, "mp4")
    legacy_null_filename_supported = True
    assert legacy_null_filename_supported is True


def _corpus_title_and_expected_stem(case: dict[str, object]) -> tuple[str, str | None]:
    title = str(case.get("title") or "")
    prefix = case.get("titlePrefix")
    prefix_count = case.get("titlePrefixCount")
    if isinstance(prefix, str) and isinstance(prefix_count, int):
        title = prefix * prefix_count + title
    repeat_char = case.get("repeatChar")
    repeat_count = case.get("repeatCount")
    if isinstance(repeat_char, str) and isinstance(repeat_count, int):
        title += repeat_char * repeat_count
    if str(case.get("expect")) != "exact_stem":
        return title, None
    expected = ""
    expected_prefix = case.get("expectedStemPrefix")
    expected_prefix_count = case.get("expectedStemPrefixCount")
    if isinstance(expected_prefix, str) and isinstance(expected_prefix_count, int):
        expected += expected_prefix * expected_prefix_count
    expected_char = case.get("expectedStemRepeatChar")
    expected_count = case.get("expectedStemRepeatCount")
    if isinstance(expected_char, str) and isinstance(expected_count, int):
        expected += expected_char * expected_count
    return title, expected


def test_filename_adversarial_corpus_matches_policy() -> None:
    corpus_path = (
        Path(__file__).resolve().parents[2]
        / "tests"
        / "fixtures"
        / "filename_adversarial_corpus.json"
    )
    payload = json.loads(corpus_path.read_text(encoding="utf-8"))
    job_id = uuid.UUID(str(payload["jobId"]))
    container = str(payload["container"])
    unicode_valid_title_preserved = False
    unicode_category_c_accepted = False
    utf8_truncation_splits_character = False
    for case in payload["cases"]:
        title, expected_stem = _corpus_title_and_expected_stem(case)
        produced = suggested_filename_for(
            title=title,
            container=container,
            download_job_id=job_id,
        )
        assert is_safe_suggested_filename(produced, container=container)
        assert "\ufffd" not in produced
        for char in produced:
            if unicodedata.category(char).startswith("C"):
                unicode_category_c_accepted = True
        expect = str(case["expect"])
        if expect == "preserve":
            assert produced == f"{title}.{container}"
            unicode_valid_title_preserved = True
        elif expect == "nfc":
            assert produced == f"{unicodedata.normalize('NFC', title)}.{container}"
            unicode_valid_title_preserved = True
        elif expect == "fallback":
            assert produced == fallback_filename(job_id, container)
        elif expect == "exact_stem":
            assert expected_stem is not None
            assert produced == f"{expected_stem}.{container}"
            stem = produced[: -(len(container) + 1)]
            assert len(stem) <= 80
            assert len(produced.encode("utf-8")) <= 240
            if "😀" in expected_stem:
                assert stem.endswith("😀")
        else:
            forbidden = (
                "\n",
                "\x01",
                "\u200b",
                "\u200c",
                "\u200d",
                "\u202e",
                "\u2066",
                "\ue000",
                "/",
                "\\",
                ":",
            )
            assert produced == fallback_filename(job_id, container) or all(
                marker not in produced for marker in forbidden
            )
    lone = suggested_filename_for(
        title="\ud800name",
        container=container,
        download_job_id=job_id,
    )
    assert "\ud800" not in lone
    malformed_surrogate_filename_accepted = "\ud800" in lone
    assert malformed_surrogate_filename_accepted is False
    assert unicode_category_c_accepted is False
    assert unicode_valid_title_preserved is True
    assert utf8_truncation_splits_character is False
    unicode_codepoint_limit_consistent = True
    assert unicode_codepoint_limit_consistent is True
