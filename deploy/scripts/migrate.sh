#!/usr/bin/env sh
set -eu

# Wait for PostgreSQL, then apply Alembic migrations.
# Intended for explicit `make migrate` / one-shot jobs — not API startup.

echo "Waiting for database..."
python - <<'PY'
import asyncio
import os
import sys

import asyncpg

url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")

async def main() -> None:
    for attempt in range(30):
        try:
            conn = await asyncpg.connect(url)
            await conn.close()
            print("database is ready")
            return
        except Exception as exc:  # noqa: BLE001
            print(f"waiting ({attempt + 1}/30): {exc}", file=sys.stderr)
            await asyncio.sleep(1)
    raise SystemExit("database not ready")

asyncio.run(main())
PY

cd /app
alembic upgrade head
echo "migrations applied"
