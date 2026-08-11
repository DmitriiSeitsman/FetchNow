"""Sanitized evidence-derived fixtures for Yandex Video Preview → VK.

Provenance (live observation 2026-08-11):
- Public page ``https://yandex.ru/video/preview/17386519177293757127``.
- Live layout is **not** dups-only: ``preloadedState.clips.dups`` and
  ``preloadedState.viewer.clips.dups`` were empty.
- Preview binding observed via data-state root field
  ``location == /video/preview/<preview_id>`` together with
  ``preloadedState.viewer`` (``viewer.internal.videoId`` present but empty).
- Stable source path:
  ``iframe[src]`` → protocol-relative ``yastatic.net`` VK player →
  fragment field ``counters`` (JSON) → ``videoUrl`` =
  ``http://vk.com/video-161264992_456240043``.
- Nested fragment ``html`` may embed ``/video_ext.php`` decoy iframe; never a
  terminal result.
- Volatile build segment / hash / reqid replaced with placeholders matching
  production regexes. No cookies, tokens, request IDs, or full page snapshot.
- CI remains offline; this module is fixture-only.

``DUPS_COMPAT_HTML`` preserves the older script-JSON ``dups[preview_id]``
shape for compatibility tests and must not be described as the current live
format. ``REGRESSION_HTML`` is the live-shaped iframe fixture.
"""

from __future__ import annotations

import html
import json
from urllib.parse import quote

PREVIEW_ID = "17386519177293757127"
EXPECTED_CANONICAL = "https://vk.com/video-161264992_456240043"
EXPECTED_VK_IDENTITY_PATH = "/video-161264992_456240043"
SUBMITTED_URL = f"https://yandex.ru/video/preview/{PREVIEW_ID}"
VOLATILE_MARKER = "FIXTURE_HASH_PLACEHOLDER"
RELATED_DECOY = "https://vk.com/video-999999999_456239000"
EMBED_DECOY = (
    f"https://vkvideo.ru/video_ext.php?oid=-161264992&id=456240043"
    f"&hash={VOLATILE_MARKER}&hd=2"
)
BUILD_SEGMENT = "0xdeadbeef012"
PLAYER_PATH = f"/video-player/{BUILD_SEGMENT}/pages-common/vk/vk.html"

_COUNTERS = {
    "duration": 1,
    "extraParams": {"from": "fixture"},
    "heartbeats": [],
    "live": False,
    "reqid": "FIXTURE_REQID",
    "table": "fixture",
    "videoUrl": "http://vk.com/video-161264992_456240043",
}
_HTML_DECOY = (
    f"<iframe src='{EMBED_DECOY}' allowfullscreen width='100%' height='100%'>"
    "</iframe>"
)
_FRAGMENT = (
    f"html={quote(_HTML_DECOY, safe='')}"
    f"&event_prefix=fixture"
    f"&restore_mute_state=1"
    f"&init_timeout=10000"
    f"&counters={quote(json.dumps(_COUNTERS, separators=(',', ':')), safe='')}"
    f"&service=fixture"
    f"&from=fixture"
)
TRUSTED_IFRAME_SRC = f"//yastatic.net{PLAYER_PATH}#{_FRAGMENT}"

_VIEWER_STATE = {
    "location": f"/video/preview/{PREVIEW_ID}",
    "disableSuspense": True,
    "preloadedState": {
        "clips": {"dups": {}, "items": []},
        "viewer": {
            "internal": {"videoId": "", "isEmbedded": False},
            "clips": {"dups": {}},
            "related": {
                "items": [
                    {
                        "videoId": "00000000000000000000",
                        "url": RELATED_DECOY,
                    }
                ]
            },
        },
    },
}


def _data_state_attr(payload: object) -> str:
    return html.escape(json.dumps(payload, separators=(",", ":")), quote=True)


REGRESSION_HTML = (
    "<!DOCTYPE html><html><head><title>preview</title></head><body>"
    f'<div data-state="{_data_state_attr({"noise": True})}"></div>'
    f'<div data-state="{_data_state_attr(_VIEWER_STATE)}"></div>'
    f'<iframe src="{html.escape(TRUSTED_IFRAME_SRC, quote=True)}"></iframe>'
    "</body></html>"
).encode()

_DUPS_STATE = {
    "dups": {
        PREVIEW_ID: {
            "videoId": PREVIEW_ID,
            "player": {
                "videoUrl": "http://vk.com/video-161264992_456240043",
                "playerUri": f"<iframe src='{EMBED_DECOY}'></iframe>",
            },
            "embedUrl": EMBED_DECOY,
            "url": EMBED_DECOY,
            "related": [
                {
                    "videoId": "00000000000000000000",
                    "player": {"videoUrl": RELATED_DECOY},
                }
            ],
        }
    }
}

DUPS_COMPAT_HTML = (
    "<!DOCTYPE html><html><body>"
    '<script type="application/json">'
    + json.dumps(_DUPS_STATE, separators=(",", ":"))
    + "</script></body></html>"
).encode()
