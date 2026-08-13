"""Trusted ffmpeg/ffprobe executable policy."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fetchnow.downloads.errors import DownloadError, DownloadErrorCode
from fetchnow.downloads.tool_executable import validate_trusted_executable


def _make(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(b"#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def test_accepts_regular_named_binary(tmp_path: Path) -> None:
    path = _make(tmp_path, "ffmpeg")
    assert validate_trusted_executable(str(path), kind="ffmpeg") == str(path)


def test_rejects_symlink_and_wrong_name(tmp_path: Path) -> None:
    real = _make(tmp_path, "ffmpeg")
    link = tmp_path / "ffmpeg-link"
    link.symlink_to(real)
    with pytest.raises(DownloadError) as exc:
        validate_trusted_executable(str(link), kind="ffmpeg")
    assert exc.value.code == DownloadErrorCode.MUXING_UNAVAILABLE
    wrong = _make(tmp_path, "not-ffmpeg")
    with pytest.raises(DownloadError):
        validate_trusted_executable(str(wrong), kind="ffmpeg")


def test_rejects_group_writable(tmp_path: Path) -> None:
    path = _make(tmp_path, "ffprobe")
    os.chmod(path, 0o775)
    with pytest.raises(DownloadError) as exc:
        validate_trusted_executable(str(path), kind="ffprobe")
    assert exc.value.code == DownloadErrorCode.MUXING_UNAVAILABLE
