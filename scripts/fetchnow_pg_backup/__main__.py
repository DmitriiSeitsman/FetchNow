"""python -m fetchnow_pg_backup (with scripts/ on PYTHONPATH)."""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
