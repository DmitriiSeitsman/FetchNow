"""Release revision validation (FETCHNOW_RELEASE_REVISION)."""

from __future__ import annotations

import re

from . import PLACEHOLDER_REVISION

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


class RevisionError(ValueError):
    """Invalid release revision."""


def validate_full_sha(value: str, *, allow_local: bool = False) -> str:
    text = (value or "").strip()
    if allow_local and text == "local":
        return text
    if not text:
        raise RevisionError("FETCHNOW_RELEASE_REVISION is required")
    if text == "latest":
        raise RevisionError("FETCHNOW_RELEASE_REVISION must not be 'latest'")
    if text == PLACEHOLDER_REVISION or text == "0" * 40:
        raise RevisionError(
            "FETCHNOW_RELEASE_REVISION is still the example placeholder"
        )
    if text != text.lower():
        raise RevisionError("FETCHNOW_RELEASE_REVISION must be lowercase")
    if len(text) != 40:
        raise RevisionError(
            f"FETCHNOW_RELEASE_REVISION must be a full 40-character SHA (got length {len(text)})"
        )
    if not _FULL_SHA.fullmatch(text):
        raise RevisionError(
            "FETCHNOW_RELEASE_REVISION must be hexadecimal [0-9a-f]{40}"
        )
    return text
