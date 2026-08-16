"""Public capability projection tests (PR-B)."""

from __future__ import annotations

from enum import Enum

from fetchnow.capabilities.public import project_provider_capabilities
from fetchnow.capabilities.registry import ProviderCapabilityRegistry
from fetchnow.url.models import ProviderID

_EXPECTED_OPS = {
    "downloadVideo": "enabled",
    "extractAudio": "disabled",
    "selectQuality": "enabled",
    "selectContainer": "disabled",
}
_EXPECTED_KINDS = {
    "video": "enabled",
    "clip": "enabled",
    "live": "disabled",
    "playlist": "disabled",
}
_EXPECTED_META = {
    "title": "enabled",
    "duration": "enabled",
    "thumbnail": "planned",
}


def _assert_plain_jsonable(value: object) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            assert isinstance(key, str)
            _assert_plain_jsonable(nested)
        return
    if isinstance(value, list):
        for nested in value:
            _assert_plain_jsonable(nested)
        return
    assert isinstance(value, str | int | float | bool | type(None))
    assert not isinstance(value, Enum)


_EXPECTED_OK_KINDS = {
    "video": "enabled",
    "clip": "disabled",
    "live": "disabled",
    "playlist": "disabled",
}


def test_project_vk_and_rutube_match_current_policy() -> None:
    registry = ProviderCapabilityRegistry.default()
    for provider_id in (ProviderID.VK.value, ProviderID.RUTUBE.value):
        projected = project_provider_capabilities(registry, provider_id)
        assert projected is not None
        assert projected == {
            "providerId": provider_id,
            "operations": _EXPECTED_OPS,
            "contentKinds": _EXPECTED_KINDS,
            "metadata": _EXPECTED_META,
        }
        _assert_plain_jsonable(projected)


def test_project_ok_matches_current_policy() -> None:
    registry = ProviderCapabilityRegistry.default()
    projected = project_provider_capabilities(registry, ProviderID.OK.value)
    assert projected is not None
    assert projected == {
        "providerId": "ok",
        "operations": _EXPECTED_OPS,
        "contentKinds": _EXPECTED_OK_KINDS,
        "metadata": _EXPECTED_META,
    }
    _assert_plain_jsonable(projected)


def test_project_unknown_provider_returns_none() -> None:
    registry = ProviderCapabilityRegistry.default()
    assert project_provider_capabilities(registry, "unknown_provider") is None
    assert project_provider_capabilities(registry, "") is None
    assert project_provider_capabilities(registry, "!!!") is None


def test_project_normalizes_provider_case() -> None:
    registry = ProviderCapabilityRegistry.default()
    projected = project_provider_capabilities(registry, "VK")
    assert projected is not None
    assert projected["providerId"] == "vk"
