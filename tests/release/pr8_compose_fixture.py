"""Pinned PR8 compose.yaml fixture for history-independent release unit tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
PR8_COMPOSE_PATH = _FIXTURE_DIR / "pr8_compose.yaml"
PR8_PROVENANCE_PATH = _FIXTURE_DIR / "pr8_compose.provenance.json"
PR8_REVISION = "b4a5c6174222a7b0ae711aed0dda1700ec799769"
PR8_COMPOSE_SHA256 = "44a0c9b3c74982155ce12afcb6a31fafca4893aa4b998e60c9623bade429101e"


def load_pr8_provenance() -> dict[str, str]:
    raw = json.loads(PR8_PROVENANCE_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("PR8 compose provenance must be an object")
    return {str(k): str(v) for k, v in raw.items()}


def pr8_compose_bytes() -> bytes:
    return PR8_COMPOSE_PATH.read_bytes()


def pr8_compose_text() -> str:
    return PR8_COMPOSE_PATH.read_text(encoding="utf-8")


def assert_pr8_fixture_provenance() -> None:
    payload = load_pr8_provenance()
    digest = hashlib.sha256(pr8_compose_bytes()).hexdigest()
    if payload.get("revision") != PR8_REVISION:
        raise AssertionError("PR8 fixture revision drifted from pinned constant")
    if len(PR8_REVISION) != 40 or any(c not in "0123456789abcdef" for c in PR8_REVISION):
        raise AssertionError("PR8 fixture revision is not a lowercase SHA")
    if payload.get("sha256") != PR8_COMPOSE_SHA256 or digest != PR8_COMPOSE_SHA256:
        raise AssertionError("PR8 fixture SHA-256 drifted from pinned provenance")
