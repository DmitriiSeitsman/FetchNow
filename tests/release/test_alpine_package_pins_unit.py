"""Offline contract: web/gateway Alpine security pins stay synchronized."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web" / "Dockerfile"
GATEWAY = ROOT / "deploy" / "nginx" / "Dockerfile"

# Current v3.24/main/x86_64 APKINDEX + security.alpinelinux.org fixed versions.
EXPECTED = {
    "libexpat": "2.9.0-r0",
    "pcre2": "10.49-r0",
    "tiff": "4.7.2-r0",
}
PIN_RE = re.compile(
    r"'((?:libexpat|pcre2|tiff))=([0-9][^']*)'",
)


def _pins(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    found = {m.group(1): m.group(2) for m in PIN_RE.finditer(text)}
    assert set(found) == set(EXPECTED), f"{path}: pins={found}"
    return found


def test_web_and_gateway_pins_match_expected_v324_main() -> None:
    web = _pins(WEB)
    gw = _pins(GATEWAY)
    assert web == EXPECTED
    assert gw == EXPECTED
    assert web == gw


def test_runtime_base_remains_nginx_alpine324() -> None:
    for path in (WEB, GATEWAY):
        text = path.read_text(encoding="utf-8")
        assert "nginx:1.31.6-alpine3.24" in text
        assert "edge" not in text.lower() or "no edge" in text.lower()
