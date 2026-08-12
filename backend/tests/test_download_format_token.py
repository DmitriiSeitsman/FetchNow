"""Conservative provider format token grammar tests."""

from __future__ import annotations

import pytest

from fetchnow.downloads.format_token import validate_provider_format_token


@pytest.mark.parametrize(
    "token",
    [
        "url720",
        "url360",
        "fmt_12.3",
        "a:b_c-1",
        "MP4_1080",
    ],
)
def test_accepts_conservative_tokens(token: str) -> None:
    assert validate_provider_format_token(token) == token


@pytest.mark.parametrize(
    "token",
    [
        "best",
        "worst",
        "bestvideo",
        "bestaudio",
        "Best",
        "a+b",
        "a/b",
        "a,b",
        "a[b]",
        "a b",
        " leading",
        "trailing ",
        "-dash",
        "",
        "has space",
        "a|b",
        "a(b)",
        "a{b}",
    ],
)
def test_rejects_selectors_and_forbidden_forms(token: str) -> None:
    with pytest.raises(ValueError):
        validate_provider_format_token(token)
