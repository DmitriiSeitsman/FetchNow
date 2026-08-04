"""Immutable verification attestation records."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import VERIFICATION_SCHEMA_VERSION, VERIFICATIONS_DIRNAME
from .fs_safety import (
    PathSafetyError,
    chmod_dir_0700,
    chmod_file_0600,
    fsync_file,
    is_safe_relative_filename,
)
from .semantic import SemanticCheckResult


@dataclass(frozen=True)
class VerificationAttestation:
    schema_version: int
    backup_id: str
    backup_sha256: str
    verified_at_utc: str
    postgres_version: str
    pg_restore_version: str
    temporary_database: str | None
    restore_duration_seconds: float | None
    semantic_checks: list[dict[str, Any]]
    cleanup: dict[str, Any]
    result: str
    failure_reason: str | None
    tool_version: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"


def build_attestation(
    *,
    backup_id: str,
    backup_sha256: str,
    postgres_version: str,
    pg_restore_version: str,
    temporary_database: str | None,
    restore_duration_seconds: float | None,
    semantic_checks: list[SemanticCheckResult],
    cleanup_ok: bool,
    dropped_database: str | None,
    result: str,
    failure_reason: str | None,
    tool_version: str,
) -> VerificationAttestation:
    if result not in {"passed", "failed"}:
        raise ValueError("result must be passed|failed")
    if result == "passed":
        if not cleanup_ok:
            raise ValueError("passed attestation requires successful cleanup")
        if any(not c.ok for c in semantic_checks):
            raise ValueError("passed attestation requires all semantic checks ok")
        if temporary_database is None:
            raise ValueError("passed attestation requires temporary database name")
    return VerificationAttestation(
        schema_version=VERIFICATION_SCHEMA_VERSION,
        backup_id=backup_id,
        backup_sha256=backup_sha256,
        verified_at_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        postgres_version=postgres_version,
        pg_restore_version=pg_restore_version,
        temporary_database=temporary_database,
        restore_duration_seconds=restore_duration_seconds,
        semantic_checks=[
            {"name": c.name, "ok": c.ok, "detail": c.detail} for c in semantic_checks
        ],
        cleanup={"ok": cleanup_ok, "dropped_database": dropped_database},
        result=result,
        failure_reason=failure_reason,
        tool_version=tool_version,
    )


def write_attestation(backup_dir: Path, attestation: VerificationAttestation) -> Path:
    ver_dir = backup_dir / VERIFICATIONS_DIRNAME
    ver_dir.mkdir(mode=0o700, exist_ok=True)
    chmod_dir_0700(ver_dir)
    stamp = attestation.verified_at_utc.replace(":", "").replace("-", "")
    name = f"verify_{stamp}_{attestation.result}.json"
    if not is_safe_relative_filename(name):
        raise PathSafetyError(f"unsafe attestation name: {name}")
    path = ver_dir / name
    # Avoid overwrite of an exact same-second file.
    if path.exists():
        n = 2
        while True:
            candidate = ver_dir / f"verify_{stamp}_{attestation.result}_{n}.json"
            if not candidate.exists():
                path = candidate
                break
            n += 1
    # Atomic publish within verifications/: write temp then rename.
    tmp = ver_dir / f".incomplete-{path.name}"
    tmp.write_text(attestation.to_json(), encoding="utf-8")
    chmod_file_0600(tmp)
    fsync_file(tmp)
    os.replace(tmp, path)
    chmod_file_0600(path)
    fsync_file(path)
    return path


def latest_attestation(backup_dir: Path) -> dict[str, Any] | None:
    ver_dir = backup_dir / VERIFICATIONS_DIRNAME
    if not ver_dir.is_dir():
        return None
    files = sorted(p for p in ver_dir.glob("verify_*.json") if p.is_file())
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"result": "invalid", "path": str(files[-1])}
