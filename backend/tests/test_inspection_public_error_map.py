"""Public mapping from inspection failures to job error codes."""

from __future__ import annotations

from fetchnow.core.config import Settings
from fetchnow.jobs.errors import JobErrorCode, public_message_for
from fetchnow.jobs.worker_loop import _public_inspection_error_code
from fetchnow.media_inspection.errors import InspectionErrorKind


def test_duration_policy_maps_to_duration_too_long() -> None:
    assert (
        _public_inspection_error_code(
            InspectionErrorKind.INSPECTION_POLICY_REJECTED,
            internal_reason="DURATION_TOO_LONG",
        )
        is JobErrorCode.DURATION_TOO_LONG
    )


def test_other_policy_rejects_stay_generic() -> None:
    assert (
        _public_inspection_error_code(
            InspectionErrorKind.INSPECTION_POLICY_REJECTED,
            internal_reason="MAX_HEIGHT",
        )
        is JobErrorCode.MEDIA_INSPECTION_FAILED
    )


def test_duration_message_states_the_default_maximum() -> None:
    """The public text names a concrete limit, so it must track the default."""
    default_seconds = Settings.model_fields["max_source_duration_seconds"].default
    hours = default_seconds // 3600
    assert default_seconds % 3600 == 0
    assert f"{hours}-hour" in public_message_for(JobErrorCode.DURATION_TOO_LONG)


def test_tool_failures_stay_generic() -> None:
    assert (
        _public_inspection_error_code(
            InspectionErrorKind.INSPECTION_INVALID_OUTPUT,
            internal_reason=None,
        )
        is JobErrorCode.MEDIA_INSPECTION_FAILED
    )
