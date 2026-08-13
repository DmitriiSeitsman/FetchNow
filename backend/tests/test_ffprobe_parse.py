"""Bounded ffprobe JSON parser proofs. Raw JSON is never returned."""

from __future__ import annotations

import json
import math

import pytest

from fetchnow.downloads.errors import DownloadError, DownloadErrorCode
from fetchnow.downloads.ffprobe_parse import parse_ffprobe_json

_VALID = {
    "streams": [
        {"codec_type": "video", "codec_name": "h264"},
        {"codec_type": "audio", "codec_name": "aac"},
    ],
    "format": {
        "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
        "duration": "12.5",
        "size": "1000",
    },
}


def _parse(payload: object, **overrides: object):
    if not isinstance(payload, bytes):
        raw = json.dumps(payload).encode("utf-8")
    else:
        raw = payload
    return parse_ffprobe_json(
        raw,
        expected_container=str(overrides.get("container", "mp4")),
        expected_size_bytes=int(overrides.get("size", 1000)),
        expected_duration_seconds=overrides.get("duration", 12.5),  # type: ignore[arg-type]
        duration_tolerance_seconds=float(overrides.get("tolerance", 2.0)),
        max_bytes=int(overrides.get("max_bytes", 10_000)),
    )


def test_valid_muxed_probe() -> None:
    result = _parse(_VALID)
    assert result.video_streams == 1
    assert result.audio_streams == 1
    assert result.other_streams == 0
    assert "codec_name" not in repr(result)
    assert "h264" not in repr(result)


def test_missing_audio_rejected() -> None:
    payload = {
        "streams": [{"codec_type": "video"}],
        "format": _VALID["format"],
    }
    with pytest.raises(DownloadError) as exc:
        _parse(payload)
    assert exc.value.code == DownloadErrorCode.MUXED_OUTPUT_INVALID


def test_missing_video_rejected() -> None:
    payload = {
        "streams": [{"codec_type": "audio"}],
        "format": _VALID["format"],
    }
    with pytest.raises(DownloadError) as exc:
        _parse(payload)
    assert exc.value.code == DownloadErrorCode.MUXED_OUTPUT_INVALID


def test_subtitle_data_attachment_rejected() -> None:
    for extra in ("subtitle", "data", "attachment"):
        payload = {
            "streams": [
                {"codec_type": "video"},
                {"codec_type": "audio"},
                {"codec_type": extra},
            ],
            "format": _VALID["format"],
        }
        with pytest.raises(DownloadError) as exc:
            _parse(payload)
        assert exc.value.code == DownloadErrorCode.MUXED_OUTPUT_INVALID


def test_wrong_container_rejected() -> None:
    payload = copy_with_format("webm")
    with pytest.raises(DownloadError) as exc:
        _parse(payload)
    assert exc.value.code == DownloadErrorCode.MUXED_OUTPUT_INVALID
    substring = copy_with_format("notmp4really")
    with pytest.raises(DownloadError) as sub_exc:
        _parse(substring)
    assert sub_exc.value.code == DownloadErrorCode.MUXED_OUTPUT_INVALID


def test_two_audio_streams_rejected() -> None:
    payload = {
        "streams": [
            {"codec_type": "video", "codec_name": "h264"},
            {"codec_type": "audio", "codec_name": "aac"},
            {"codec_type": "audio", "codec_name": "aac"},
        ],
        "format": _VALID["format"],
    }
    with pytest.raises(DownloadError) as exc:
        _parse(payload)
    assert exc.value.code == DownloadErrorCode.MUXED_OUTPUT_INVALID


def test_webm_codecs_accepted_and_mp4_policy_rejects_vp9() -> None:
    webm = {
        "streams": [
            {"codec_type": "video", "codec_name": "vp9"},
            {"codec_type": "audio", "codec_name": "opus"},
        ],
        "format": {
            "format_name": "matroska,webm",
            "duration": "12.5",
            "size": "1000",
        },
    }
    result = _parse(webm, container="webm")
    assert result.video_streams == 1
    assert result.audio_streams == 1
    mixed = {
        "streams": [
            {"codec_type": "video", "codec_name": "vp9"},
            {"codec_type": "audio", "codec_name": "opus"},
        ],
        "format": _VALID["format"],
    }
    with pytest.raises(DownloadError) as exc:
        _parse(mixed)
    assert exc.value.code == DownloadErrorCode.MUXED_OUTPUT_INVALID


def test_probe_result_omits_filename_and_raw_json() -> None:
    payload = {
        "streams": _VALID["streams"],
        "format": {
            **_VALID["format"],  # type: ignore[dict-item]
            "filename": "/secret/path/artifact.mp4?token=leak",
        },
    }
    result = _parse(payload)
    dumped = repr(result)
    assert "secret" not in dumped
    assert "token=leak" not in dumped
    assert "/secret/path" not in dumped


def test_incompatible_probe_codec_rejected() -> None:
    payload = {
        "streams": [
            {"codec_type": "video", "codec_name": "hevc"},
            {"codec_type": "audio", "codec_name": "aac"},
        ],
        "format": _VALID["format"],
    }
    with pytest.raises(DownloadError) as exc:
        _parse(payload)
    assert exc.value.code == DownloadErrorCode.MUXED_OUTPUT_INVALID


def test_probe_size_mismatch_rejected() -> None:
    with pytest.raises(DownloadError) as exc:
        _parse(_VALID, size=16)
    assert exc.value.code == DownloadErrorCode.MUXED_OUTPUT_INVALID


def copy_with_format(name: str) -> dict[str, object]:
    return {
        "streams": _VALID["streams"],
        "format": {**_VALID["format"], "format_name": name},  # type: ignore[dict-item]
    }


def test_oversized_output_rejected() -> None:
    payload = {
        "streams": _VALID["streams"],
        "format": {**_VALID["format"], "size": "99999"},  # type: ignore[dict-item]
    }
    with pytest.raises(DownloadError) as exc:
        _parse(payload, max_bytes=1000)
    assert exc.value.code == DownloadErrorCode.MUXED_OUTPUT_INVALID


def test_short_invalid_json_rejected() -> None:
    with pytest.raises(DownloadError):
        _parse(b"{")
    with pytest.raises(DownloadError):
        _parse(b"")
    with pytest.raises(DownloadError):
        _parse(b"not-json")


def test_nan_inf_depth_node_string_bombs_rejected() -> None:
    with pytest.raises(DownloadError):
        parse_ffprobe_json(
            b'{"streams":[{"codec_type":"video"}],"format":{"duration":"NaN"}}',
            expected_container="mp4",
            expected_size_bytes=1,
            expected_duration_seconds=1.0,
            duration_tolerance_seconds=1.0,
            max_bytes=10_000,
        )
    inf_payload = {
        "streams": _VALID["streams"],
        "format": {**_VALID["format"], "duration": "Infinity"},  # type: ignore[dict-item]
    }
    with pytest.raises(DownloadError):
        _parse(inf_payload)
    deep = {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": {"i": 1}}}}}}}}}
    with pytest.raises(DownloadError):
        _parse(deep)
    huge_list = {"streams": list(range(64)), "format": {}}
    with pytest.raises(DownloadError):
        _parse(huge_list)
    bomb = {"streams": [{"codec_type": "x" * 512}], "format": {}}
    with pytest.raises(DownloadError):
        _parse(bomb)
    assert not math.isnan(12.5)


def test_process_result_repr_omits_stdout() -> None:
    from fetchnow.media_inspection.protocols import ProcessResult

    result = ProcessResult(
        exit_code=0,
        stdout=b'{"filename":"/secret/artifact.mp4?token=leak"}',
        stderr=b"",
        timed_out=False,
        cancelled=False,
    )
    dumped = repr(result)
    assert "/secret" not in dumped
    assert "token=leak" not in dumped
    assert "filename" not in dumped
    with pytest.raises(DownloadError) as exc:
        _parse(_VALID, duration=1.0, tolerance=0.1)
    assert exc.value.code == DownloadErrorCode.MUXED_OUTPUT_INVALID
