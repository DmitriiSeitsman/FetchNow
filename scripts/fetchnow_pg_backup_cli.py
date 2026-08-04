#!/usr/bin/env python3
"""Entry point: python3 scripts/fetchnow_pg_backup_cli.py …"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from fetchnow_pg_backup.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
