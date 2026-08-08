"""Immutable verification attestation records."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import (
    VERIFICATION_SCHEMA_VERSION,
    VERIFICATION_SCHEMA_VERSION_V2,
    VERIFICATIONS_DIRNAME,
)
from .alembic_graph import validate_revision_id
from .checksums import sha256_bytes
from .fs_safety import (
    PathSafetyError,
    chmod_dir_0700,
    chmod_file_0600,
    fsync_dir,
    fsync_file,
    is_safe_relative_filename,
)
from .semantic import SemanticCheckResult

class AttestationError(ValueError):
    """Invalid verification attestation document."""


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


@dataclass(frozen=True)
class VerificationAttestationV2:
    schema_version: int
    backup_id: str
    backup_sha256: str
    verified_at_utc: str
    postgres_version: str
    pg_restore_version: str
    temporary_database: str | None
    restore_duration_seconds: float | None
    expected_alembic_heads: tuple[str, ...]
    static_alembic_heads: tuple[str, ...]
    migrations_graph_sha256: str
    verified_restored_heads: tuple[str, ...] | None
    semantic_checks: list[dict[str, Any]]
    cleanup: dict[str, Any]
    result: str
    failure_reason: str | None
    tool_version: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"

    @property
    def exact_head_proof(self) -> bool:
        if self.result != "passed" or self.verified_restored_heads is None:
            return False
        expected = frozenset(self.expected_alembic_heads)
        static = frozenset(self.static_alembic_heads)
        restored = frozenset(self.verified_restored_heads)
        return expected == static == restored and bool(expected)


ParsedAttestation = VerificationAttestation | VerificationAttestationV2


def attestation_sha256(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _parse_exact_int(value: object, *, field: str) -> int:
    if type(value) is not int or isinstance(value, bool):
        raise AttestationError(f"{field} must be an exact integer")
    return value


def _parse_utc(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AttestationError(f"{field} must be canonical UTC ending with Z")
    return value


def _parse_sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise AttestationError(f"{field} must be a 64-character hex SHA-256")
    try:
        int(value, 16)
    except ValueError as exc:
        raise AttestationError(f"{field} must be lowercase hex") from exc
    if value != value.lower():
        raise AttestationError(f"{field} must be lowercase hex")
    return value


def _parse_head_list(raw: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise AttestationError(f"{field} must be a non-empty JSON array")
    seen: set[str] = set()
    items: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise AttestationError(f"{field} entries must be strings")
        rev = validate_revision_id(item.strip(), field=f"{field} entry")
        if rev in seen:
            raise AttestationError(f"{field} contains duplicate revision: {rev!r}")
        seen.add(rev)
        items.append(rev)
    sorted_items = tuple(sorted(items))
    if list(sorted_items) != list(raw):
        raise AttestationError(f"{field} must be sorted unique revision IDs")
    return sorted_items


def _parse_optional_head_list(raw: object, *, field: str) -> tuple[str, ...] | None:
    if raw is None:
        return None
    return _parse_head_list(raw, field=field)


def _load_strict_json(text: str) -> dict[str, Any]:
    def object_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [key for key, _value in pairs]
        if len(keys) != len(set(keys)):
            raise AttestationError("duplicate JSON object keys")
        return dict(pairs)

    try:
        raw = json.loads(text, object_pairs_hook=object_pairs_hook)
    except json.JSONDecodeError as exc:
        raise AttestationError(f"invalid attestation JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise AttestationError("attestation must be a JSON object")
    return raw


def _parse_semantic_checks(raw: object) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise AttestationError("semantic_checks must be a JSON array")
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise AttestationError("semantic_checks entries must be objects")
        allowed = {"name", "ok", "detail"}
        unknown = set(item) - allowed
        if unknown:
            raise AttestationError(
                f"semantic_checks entry has unknown fields: {sorted(unknown)}"
            )
        if not isinstance(item.get("name"), str):
            raise AttestationError("semantic_checks.name must be a string")
        if type(item.get("ok")) is not bool:
            raise AttestationError("semantic_checks.ok must be a boolean")
        if not isinstance(item.get("detail"), str):
            raise AttestationError("semantic_checks.detail must be a string")
        out.append(
            {
                "name": item["name"],
                "ok": item["ok"],
                "detail": item["detail"],
            }
        )
    return out


def _parse_cleanup(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise AttestationError("cleanup must be a JSON object")
    allowed = {"ok", "dropped_database"}
    unknown = set(raw) - allowed
    if unknown:
        raise AttestationError(f"cleanup has unknown fields: {sorted(unknown)}")
    if type(raw.get("ok")) is not bool:
        raise AttestationError("cleanup.ok must be a boolean")
    dropped = raw.get("dropped_database")
    if dropped is not None and not isinstance(dropped, str):
        raise AttestationError("cleanup.dropped_database must be a string or null")
    return {"ok": raw["ok"], "dropped_database": dropped}


def parse_attestation_document(raw: dict[str, Any]) -> ParsedAttestation:
    if "schema_version" not in raw:
        raise AttestationError("attestation missing schema_version")
    schema = _parse_exact_int(raw["schema_version"], field="schema_version")
    if schema == VERIFICATION_SCHEMA_VERSION:
        return _parse_attestation_v1(raw)
    if schema == VERIFICATION_SCHEMA_VERSION_V2:
        return _parse_attestation_v2(raw)
    raise AttestationError(f"unsupported attestation schema version: {schema}")


def parse_attestation_bytes(data: bytes) -> ParsedAttestation:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AttestationError("attestation is not valid UTF-8") from exc
    return parse_attestation_document(_load_strict_json(text))


def parse_attestation_file(path: Path) -> ParsedAttestation:
    return parse_attestation_bytes(path.read_bytes())


def _parse_attestation_v1(raw: dict[str, Any]) -> VerificationAttestation:
    allowed = {
        "schema_version",
        "backup_id",
        "backup_sha256",
        "verified_at_utc",
        "postgres_version",
        "pg_restore_version",
        "temporary_database",
        "restore_duration_seconds",
        "semantic_checks",
        "cleanup",
        "result",
        "failure_reason",
        "tool_version",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise AttestationError(f"v1 attestation has unknown fields: {sorted(unknown)}")
    missing = allowed - set(raw)
    if missing:
        raise AttestationError(f"v1 attestation missing fields: {sorted(missing)}")
    result = raw["result"]
    if result not in {"passed", "failed"}:
        raise AttestationError("result must be passed|failed")
    return VerificationAttestation(
        schema_version=VERIFICATION_SCHEMA_VERSION,
        backup_id=str(raw["backup_id"]),
        backup_sha256=_parse_sha256(raw["backup_sha256"], field="backup_sha256"),
        verified_at_utc=_parse_utc(raw["verified_at_utc"], field="verified_at_utc"),
        postgres_version=str(raw["postgres_version"]),
        pg_restore_version=str(raw["pg_restore_version"]),
        temporary_database=raw["temporary_database"],
        restore_duration_seconds=raw["restore_duration_seconds"],
        semantic_checks=_parse_semantic_checks(raw["semantic_checks"]),
        cleanup=_parse_cleanup(raw["cleanup"]),
        result=result,
        failure_reason=raw["failure_reason"],
        tool_version=str(raw["tool_version"]),
    )


def _parse_attestation_v2(raw: dict[str, Any]) -> VerificationAttestationV2:
    allowed = {
        "schema_version",
        "backup_id",
        "backup_sha256",
        "verified_at_utc",
        "postgres_version",
        "pg_restore_version",
        "temporary_database",
        "restore_duration_seconds",
        "expected_alembic_heads",
        "static_alembic_heads",
        "migrations_graph_sha256",
        "verified_restored_heads",
        "semantic_checks",
        "cleanup",
        "result",
        "failure_reason",
        "tool_version",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise AttestationError(f"v2 attestation has unknown fields: {sorted(unknown)}")
    missing = allowed - set(raw)
    if missing:
        raise AttestationError(f"v2 attestation missing fields: {sorted(missing)}")
    result = raw["result"]
    if result not in {"passed", "failed"}:
        raise AttestationError("result must be passed|failed")
    expected = _parse_head_list(raw["expected_alembic_heads"], field="expected_alembic_heads")
    static = _parse_head_list(raw["static_alembic_heads"], field="static_alembic_heads")
    restored = _parse_optional_head_list(
        raw["verified_restored_heads"], field="verified_restored_heads"
    )
    graph_sha = _parse_sha256(raw["migrations_graph_sha256"], field="migrations_graph_sha256")
    cleanup = _parse_cleanup(raw["cleanup"])
    semantic_checks = _parse_semantic_checks(raw["semantic_checks"])
    if result == "passed":
        if restored is None:
            raise AttestationError(
                "passed v2 attestation requires verified_restored_heads"
            )
        if frozenset(expected) != frozenset(static) or frozenset(expected) != frozenset(
            restored
        ):
            raise AttestationError(
                "passed v2 attestation requires expected, static, and restored head equality"
            )
        if not cleanup["ok"]:
            raise AttestationError("passed v2 attestation requires cleanup.ok true")
        if any(not item["ok"] for item in semantic_checks):
            raise AttestationError(
                "passed v2 attestation requires all semantic_checks ok"
            )
    return VerificationAttestationV2(
        schema_version=VERIFICATION_SCHEMA_VERSION_V2,
        backup_id=str(raw["backup_id"]),
        backup_sha256=_parse_sha256(raw["backup_sha256"], field="backup_sha256"),
        verified_at_utc=_parse_utc(raw["verified_at_utc"], field="verified_at_utc"),
        postgres_version=str(raw["postgres_version"]),
        pg_restore_version=str(raw["pg_restore_version"]),
        temporary_database=raw["temporary_database"],
        restore_duration_seconds=raw["restore_duration_seconds"],
        expected_alembic_heads=expected,
        static_alembic_heads=static,
        migrations_graph_sha256=graph_sha,
        verified_restored_heads=restored,
        semantic_checks=semantic_checks,
        cleanup=cleanup,
        result=result,
        failure_reason=raw["failure_reason"],
        tool_version=str(raw["tool_version"]),
    )


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


def build_attestation_v2(
    *,
    backup_id: str,
    backup_sha256: str,
    postgres_version: str,
    pg_restore_version: str,
    temporary_database: str | None,
    restore_duration_seconds: float | None,
    expected_alembic_heads: tuple[str, ...],
    static_alembic_heads: tuple[str, ...],
    migrations_graph_sha256: str,
    verified_restored_heads: tuple[str, ...] | None,
    semantic_checks: list[SemanticCheckResult],
    cleanup_ok: bool,
    dropped_database: str | None,
    result: str,
    failure_reason: str | None,
    tool_version: str,
) -> VerificationAttestationV2:
    if result not in {"passed", "failed"}:
        raise ValueError("result must be passed|failed")
    expected = tuple(sorted(frozenset(expected_alembic_heads)))
    static = tuple(sorted(frozenset(static_alembic_heads)))
    restored = (
        None
        if verified_restored_heads is None
        else tuple(sorted(frozenset(verified_restored_heads)))
    )
    if not expected or not static:
        raise ValueError("v2 attestation requires non-empty expected and static heads")
    if len(migrations_graph_sha256) != 64:
        raise ValueError("migrations_graph_sha256 must be a 64-character hex SHA-256")
    if result == "passed":
        if restored is None:
            raise ValueError("passed v2 attestation requires verified_restored_heads")
        if expected != static or expected != restored:
            raise ValueError(
                "passed v2 attestation requires expected, static, and restored head equality"
            )
        if not cleanup_ok:
            raise ValueError("passed v2 attestation requires successful cleanup")
        if any(not c.ok for c in semantic_checks):
            raise ValueError("passed v2 attestation requires all semantic checks ok")
        if temporary_database is None:
            raise ValueError("passed v2 attestation requires temporary database name")
    return VerificationAttestationV2(
        schema_version=VERIFICATION_SCHEMA_VERSION_V2,
        backup_id=backup_id,
        backup_sha256=backup_sha256,
        verified_at_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        postgres_version=postgres_version,
        pg_restore_version=pg_restore_version,
        temporary_database=temporary_database,
        restore_duration_seconds=restore_duration_seconds,
        expected_alembic_heads=expected,
        static_alembic_heads=static,
        migrations_graph_sha256=migrations_graph_sha256,
        verified_restored_heads=restored,
        semantic_checks=[
            {"name": c.name, "ok": c.ok, "detail": c.detail} for c in semantic_checks
        ],
        cleanup={"ok": cleanup_ok, "dropped_database": dropped_database},
        result=result,
        failure_reason=failure_reason,
        tool_version=tool_version,
    )


def write_attestation(
    backup_dir: Path, attestation: VerificationAttestation | VerificationAttestationV2
) -> Path:
    ver_dir = backup_dir / VERIFICATIONS_DIRNAME
    ver_dir.mkdir(mode=0o700, exist_ok=True)
    chmod_dir_0700(ver_dir)
    stamp = attestation.verified_at_utc.replace(":", "").replace("-", "")
    name = f"verify_{stamp}_{attestation.result}.json"
    if not is_safe_relative_filename(name):
        raise PathSafetyError(f"unsafe attestation name: {name}")
    path = ver_dir / name
    if path.exists():
        n = 2
        while True:
            candidate = ver_dir / f"verify_{stamp}_{attestation.result}_{n}.json"
            if not candidate.exists():
                path = candidate
                break
            n += 1
            if n > 10_000:
                raise PathSafetyError("could not allocate unique attestation filename")
    if path.exists():
        raise PathSafetyError(f"refusing to overwrite existing attestation: {path}")
    tmp = ver_dir / f".incomplete-{path.name}"
    tmp.write_text(attestation.to_json(), encoding="utf-8")
    chmod_file_0600(tmp)
    fsync_file(tmp)
    os.replace(tmp, path)
    chmod_file_0600(path)
    fsync_file(path)
    fsync_dir(ver_dir)
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
