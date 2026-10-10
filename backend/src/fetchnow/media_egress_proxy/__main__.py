"""Entrypoint: python -m fetchnow.media_egress_proxy"""

from __future__ import annotations

from fetchnow.media_egress_proxy.server import main

if __name__ == "__main__":
    raise SystemExit(main())
