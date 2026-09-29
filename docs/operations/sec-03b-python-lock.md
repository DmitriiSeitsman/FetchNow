# SEC-03B1 — Python lock and frozen install

Status: implementation in the SEC-03B1 worktree. Not committed. SEC-03B2 (audit CI) and SEC-03C (image scanning) are not started.

This change makes `backend/uv.lock` the install source for the API image, backend CI, and `make setup`. It does not bump Python, FFmpeg, yt-dlp, or any direct dependency pin. It does not scan images or query a Python advisory service.

## Resolver

uv **0.12.19**, pinned by `[tool.uv] required-version`.

- Docker builder copies `/uv` from `ghcr.io/astral-sh/uv:0.12.19`. The runtime stage does not contain that binary.
- Backend CI downloads the same release from GitHub and checks the SHA-256:
  - linux x86_64 `23bf5552d220e0842b65c862097b2ebaeba0064b74eda5e565e77fd25969d8c8`
  - linux aarch64 `0804e9b164c64b6914182d5920c08551958a095986f10a3731056df701126436`
- Local setup requires `uv` 0.12.19 on `PATH`. A different uv fails `uv lock --check` / project sync via `required-version`.

## Resolution range vs tested versions

`requires-python` remains `>=3.12`. There is **no** `[tool.uv].environments` filter. The lock resolves across that declared range. Direct pins are unchanged.

`required-environments` only requires that wheels exist for Linux x86_64, Linux aarch64, and macOS arm64. It does not shrink `requires-python`.

**Declared resolution range:** CPython `>=3.12` (universal lock).

**Actually exercised in this worktree (acceptance):**

| Interpreter | Platform | Role |
| --- | --- | --- |
| Python 3.12.x | darwin arm64 | `uv sync --frozen --extra dev`, Make setup, backend pytest |
| Python 3.14.6 | darwin arm64 | local runtime `uv sync --frozen --no-dev --no-editable` |
| Python 3.14.6 | linux arm64 (image `python:3.14.6-slim-bookworm`) | Docker runtime image |

Linux x86_64 is required in the lock for wheel availability. It is **not** treated as locally verified here; CI on `ubuntu-latest` (typically x86_64) remains the gate for that install path.

## How install uses the lock

| Place | Command |
| --- | --- |
| `backend/Dockerfile` builder | `uv lock --check` then `uv sync --frozen --no-dev --no-editable --python /usr/local/bin/python` into `/opt/venv` |
| Backend CI and `make setup` | `uv lock --check` then `uv sync --frozen --extra dev` on Python 3.12 |
| Pytest-only CI jobs | `python -m pip install --require-hashes --no-deps -r backend/ci/pytest-requirements.txt` |

`--frozen` alone does not reject a stale lock. Install paths therefore run `uv lock --check` (or an equivalent locked mode) first. Do not put `pip install .` or `pip install -e ".[dev]"` after sync.

The `dev` extra is not installed unless the command passes `--extra dev`.

## Pytest CI export

`backend/ci/pytest-requirements.txt` is generated from `uv.lock`. It is not hand-edited.

Executable regeneration (from `backend/`, uv 0.12.19 on `PATH`):

```bash
python3 - <<'PY'
from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

HEADER = (
    "# Generated from backend/uv.lock (pytest transitive closure).\n"
    "# Do not edit by hand. Regeneration: see docs/operations/sec-03b-python-lock.md\n"
    "# Install: python -m pip install --require-hashes --no-deps -r backend/ci/pytest-requirements.txt\n"
    "#\n"
)

lock = tomllib.loads(Path("uv.lock").read_text())
pkgs = {p["name"]: p for p in lock["package"]}

def closure(name: str, acc: set[str] | None = None) -> set[str]:
    acc = set() if acc is None else acc
    if name in acc:
        return acc
    acc.add(name)
    for dep in pkgs[name].get("dependencies") or []:
        closure(dep["name"], acc)
    return acc

wanted = closure("pytest")
export = subprocess.check_output(
    [
        "uv",
        "export",
        "--frozen",
        "--extra",
        "dev",
        "--no-emit-project",
        "--no-header",
        "--no-annotate",
    ],
    text=True,
)
blocks: dict[str, str] = {}
current: list[str] = []

def flush() -> None:
    if not current:
        return
    name = current[0].split("==", 1)[0].strip()
    blocks[name] = "".join(current).rstrip() + "\n"
    current.clear()

for line in export.splitlines(True):
    if not line.strip() or line.lstrip().startswith("#"):
        continue
    if line[:1].isspace():
        if current:
            current.append(line)
        continue
    flush()
    current = [line]
flush()

missing = wanted - set(blocks)
if missing:
    raise SystemExit(f"export missing: {sorted(missing)}")
for name in sorted(wanted):
    if not blocks[name].startswith(f"{name}=={pkgs[name]['version']}"):
        raise SystemExit(f"pin mismatch: {name}")

text = HEADER + "\n".join(blocks[n] for n in sorted(wanted))
Path("ci/pytest-requirements.txt").write_text(text)
print("wrote ci/pytest-requirements.txt", sorted(wanted))
PY
```

That algorithm:

1. Takes the full transitive pytest set from the lock (including marker-gated edges such as `colorama` on Windows).
2. Keeps versions, markers, and hashes from `uv export`.
3. Omits every package outside that closure (no FastAPI, ruff, mypy, …).
4. Sorts by package name so the file is deterministic.

CI regenerates the same bytes and compares them to the committed file. A changed transitive version, missing package, changed hash, or stale export fails the job.

## Direct pins

Unchanged: fastapi 0.141.1, uvicorn[standard] 0.35.0, sqlalchemy[asyncio] 2.0.42, alembic 1.16.4, asyncpg 0.31.0, pydantic-settings 2.14.2, httpx 0.28.1, yt-dlp 2026.7.4, pytest 8.4.1, pytest-asyncio 1.1.0, ruff 0.12.7, mypy 1.17.1.

The first `uv lock` selected transitive versions that were previously floating. Keeping these direct pins does not mean those transitives match any earlier `pip install`. There was no saved production install inventory to diff against. The locked set is the new baseline.

Build backend: `hatchling==1.32.4` in `[build-system].requires`. Its dependencies are hashed in `[tool.uv] build-constraint-dependencies` (hatchling, packaging 26.3, pathspec 1.1.1, pluggy 1.6.0, tomlkit 0.15.1, trove-classifiers 2026.9.21.13). That set is the isolated build environment, not the runtime venv.

## What this does not reproduce

Lock, install, and the normalized package inventory are the reproducibility claim. Debian packages, the unpinned `ffmpeg` apt install, base image digests, and production hosts are outside this change.

## Update

1. Change a direct pin in `backend/pyproject.toml` only when that bump is approved.
2. Run `uv lock --upgrade-package <name>` with uv 0.12.19 (or a reviewed `uv lock --upgrade`).
3. Regenerate `backend/ci/pytest-requirements.txt` with the algorithm above.
4. `uv lock --check` must pass. CI does not upgrade the lock.

## Not in this change

- SEC-03B2 audit wrapper and advisory queries
- SEC-03C image or OS scanning
- Fixes for whatever a future Python audit reports
