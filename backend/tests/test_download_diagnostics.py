"""Allowlisted download diagnostics: no secrets, deterministic classifier."""

from __future__ import annotations

import json
import logging

import pytest

from fetchnow.core.logging import JsonFormatter
from fetchnow.downloads.diagnostics import (
    DownloadStageEvent,
    FailureClass,
    classify_tool_failure,
    diagnostic_fingerprint,
    emit_stage_event,
    process_exit_category,
)


def test_classifier_never_raises_on_malformed_stderr() -> None:
    payloads = (
        b"",
        b"\xff\xfe\x00",
        b"http error 403 https://vk.com/video-1_2?token=secret\n" * 4000,
        "неклассифицируемый текст".encode(),
    )
    for blob in payloads:
        try:
            result = classify_tool_failure(stderr=blob)
        except Exception as exc:  # pragma: no cover - fail the proof flag
            raise AssertionError("classifier raised") from exc
        assert result in FailureClass
    assert classify_tool_failure(stderr=None) is FailureClass.TOOL_EXIT_NONZERO  # type: ignore[arg-type]


def test_classifier_is_deterministic_and_fail_closed() -> None:
    stderr = b"ERROR: requested format is not available\nhttps://cdn.example/file?sig=1"
    first = classify_tool_failure(stderr=stderr)
    second = classify_tool_failure(stderr=stderr)
    assert first is second is FailureClass.FORMAT_UNAVAILABLE
    unknown = classify_tool_failure(stderr=b"completely novel provider gibberish")
    assert unknown is FailureClass.TOOL_EXIT_NONZERO
    assert (
        classify_tool_failure(timed_out=True, stderr=b"http error 404")
        is FailureClass.TOOL_TIMEOUT
    )
    assert (
        classify_tool_failure(signal=9, stderr=b"http error 401")
        is FailureClass.TOOL_SIGNALLED
    )
    assert (
        classify_tool_failure(cancelled=True, stderr=b"http error 403")
        is FailureClass.CANCELLED
    )
    assert (
        classify_tool_failure(stderr=b"HTTP Error 401 Unauthorized")
        is FailureClass.HTTP_AUTH_REQUIRED
    )
    assert (
        classify_tool_failure(stderr=b"HTTP Error 403 Forbidden")
        is FailureClass.HTTP_FORBIDDEN
    )
    assert (
        classify_tool_failure(stderr=b"HTTP Error 404 Not Found")
        is FailureClass.HTTP_NOT_FOUND
    )
    assert classify_tool_failure(stderr=b"Unable to extract video data") is (
        FailureClass.PROVIDER_RESPONSE_CHANGED
    )


@pytest.mark.parametrize(
    ("stderr", "expected"),
    (
        (b"Temporary failure in name resolution", FailureClass.NETWORK_DNS_FAILED),
        (b"Network is unreachable", FailureClass.NETWORK_CONNECT_FAILED),
        (b"The read operation timed out", FailureClass.NETWORK_TIMEOUT),
        (b"certificate verify failed", FailureClass.TLS_FAILED),
        (
            b"This video is not available in your country",
            FailureClass.GEO_RESTRICTED,
        ),
        (b"Unable to download video data", FailureClass.MEDIA_TRANSFER_FAILED),
    ),
)
def test_classifier_has_safe_network_and_cdn_categories(
    stderr: bytes, expected: FailureClass
) -> None:
    assert classify_tool_failure(stderr=stderr) is expected


def test_failure_class_never_copies_secrets_or_urls() -> None:
    result = classify_tool_failure(
        stderr=b"token=abc Bearer xyz https://vk.com/video-1_2?query=1 /tmp/secret"
    )
    assert "http" not in result.value.lower()
    assert "token" not in result.value.lower()
    assert "tmp" not in result.value.lower()
    fp = diagnostic_fingerprint(
        stage="video",
        failure_class=result.value,
        exit_category="NONZERO",
        retryable=True,
        error_code="DOWNLOAD_TOOL_FAILED",
    )
    assert "vk.com" not in fp
    assert "token" not in fp


def test_stage_events_omit_forbidden_fields(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="fetchnow.downloads.diagnostics")
    emit_stage_event(
        DownloadStageEvent.VIDEO_FAILED,
        download_job_id="11111111-2222-3333-4444-555555555555",
        media_job_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        attempt_count=2,
        fence_token=7,
        stage="video",
        duration_ms=12,
        retryable=True,
        error_code="DOWNLOAD_TOOL_FAILED",
        failure_class=FailureClass.FORMAT_UNAVAILABLE,
        process_exit_category="NONZERO",
        stdout_byte_count=0,
        stderr_byte_count=128,
    )
    assert caplog.records
    blob = " ".join(record.getMessage() for record in caplog.records).lower()
    extras = [getattr(record, "download_job_id", None) for record in caplog.records]
    assert any(extras)
    assert "https://" not in blob
    assert "bearer" not in blob
    assert "/tmp/" not in blob
    assert "url720" not in blob
    for record in caplog.records:
        payload = str(getattr(record, "__dict__", {}))
        assert "stderr" not in payload or "stderr_byte_count" in payload
        assert "canonical" not in payload.lower()
        assert "provider_format" not in payload.lower()


def test_json_formatter_preserves_validated_download_diagnostics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="fetchnow.downloads.diagnostics")
    emit_stage_event(
        DownloadStageEvent.VIDEO_FAILED,
        download_job_id="11111111-2222-3333-4444-555555555555",
        media_job_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        attempt_count=2,
        fence_token=7,
        stage="downloading_video",
        retryable=True,
        error_code="DOWNLOAD_TOOL_FAILED",
        failure_class=FailureClass.HTTP_FORBIDDEN,
        process_exit_category="NONZERO",
        stdout_byte_count=0,
        stderr_byte_count=128,
    )
    payload = json.loads(JsonFormatter().format(caplog.records[-1]))
    assert payload["download_job_id"] == "11111111-2222-3333-4444-555555555555"
    assert payload["media_job_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert payload["attempt_count"] == 2
    assert payload["fence_token"] == 7
    assert payload["stage"] == "downloading_video"
    assert payload["retryable"] is True
    assert payload["error_code"] == "DOWNLOAD_TOOL_FAILED"
    assert payload["failure_class"] == "HTTP_FORBIDDEN"
    assert payload["process_exit_category"] == "NONZERO"
    assert payload["stdout_byte_count"] == 0
    assert payload["stderr_byte_count"] == 128
    assert len(payload["diagnostic_fingerprint"]) == 16


def test_json_formatter_drops_untrusted_diagnostic_extras() -> None:
    record = logging.LogRecord(
        name="fetchnow.downloads.diagnostics",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="safe",
        args=(),
        exc_info=None,
    )
    record.failure_class = "https://cdn.example/?token=secret"
    record.download_job_id = "not-a-uuid"
    record.error_code = "https://cdn.example/?token=secret"
    record.internal_reason = "https://cdn.example/?token=secret"
    payload = json.loads(JsonFormatter().format(record))
    assert "failure_class" not in payload
    assert "download_job_id" not in payload
    assert "error_code" not in payload
    assert "internal_reason" not in payload
    assert "cdn.example" not in json.dumps(payload)


def test_token_shaped_secrets_are_not_valid_diagnostic_catalog_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="fetchnow.downloads.diagnostics")
    emit_stage_event(
        DownloadStageEvent.VIDEO_FAILED,
        stage="BearerSecret123",
        error_code="ApiSecret123",
    )
    payload = json.loads(JsonFormatter().format(caplog.records[-1]))
    assert "stage" not in payload
    assert "error_code" not in payload
    assert "BearerSecret123" not in json.dumps(payload)
    assert "ApiSecret123" not in json.dumps(payload)


def test_unknown_event_name_is_fail_closed(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="fetchnow.downloads.diagnostics")
    emit_stage_event("not_an_allowlisted_event")  # type: ignore[arg-type]
    assert any(
        record.getMessage() == "download_job_failed" for record in caplog.records
    )


def test_process_exit_category_is_coarse() -> None:
    assert (
        process_exit_category(
            exit_code=0, timed_out=False, signal=None, cancelled=False
        ).value
        == "ZERO"
    )
    assert (
        process_exit_category(
            exit_code=1, timed_out=False, signal=None, cancelled=False
        ).value
        == "NONZERO"
    )
    assert (
        process_exit_category(
            exit_code=None, timed_out=True, signal=None, cancelled=False
        ).value
        == "TIMEOUT"
    )
    assert (
        process_exit_category(
            exit_code=-9, timed_out=False, signal=9, cancelled=False
        ).value
        == "SIGNALLED"
    )
    assert (
        process_exit_category(
            exit_code=1, timed_out=False, signal=None, cancelled=True
        ).value
        == "CANCELLED"
    )
