"""Manifest model and strict validation (schema version 1)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import (
    ARCHIVE_FORMAT,
    DUMP_FILENAME,
    MANIFEST_FILENAME,
    MANIFEST_SCHEMA_VERSION,
)
from .fs_safety import PathSafetyError, is_safe_relative_filename


class ManifestError(ValueError):
    """Invalid or unsupported manifest."""


@dataclass(frozen=True)
class StructuralValidation:
    ok: bool
    tool: str
    entry_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "tool": self.tool,
            "entry_count": self.entry_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StructuralValidation:
        if not isinstance(data, dict):
            raise ManifestError("structural_validation must be an object")
        ok = data.get("ok")
        tool = data.get("tool")
        entry_count = data.get("entry_count")
        if not isinstance(ok, bool):
            raise ManifestError("structural_validation.ok must be bool")
        if not isinstance(tool, str) or not tool:
            raise ManifestError("structural_validation.tool must be non-empty string")
        if not isinstance(entry_count, int) or entry_count < 0:
            raise ManifestError("structural_validation.entry_count must be >= 0")
        return cls(ok=ok, tool=tool, entry_count=entry_count)


@dataclass(frozen=True)
class ManifestV1:
    schema_version: int
    backup_id: str
    created_at_utc: str
    compose_project: str
    database_name: str
    postgres_version: str
    pg_dump_version: str
    archive_format: str
    compression: str
    dump_filename: str
    dump_size_bytes: int
    sha256: str
    git_revision: str | None
    structural_validation: StructuralValidation
    duration_seconds: float
    tool_version: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["structural_validation"] = self.structural_validation.to_dict()
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ManifestV1:
        if not isinstance(data, dict):
            raise ManifestError("manifest must be a JSON object")
        version = data.get("schema_version")
        if version != MANIFEST_SCHEMA_VERSION:
            raise ManifestError(
                f"unsupported manifest schema_version: {version!r} "
                f"(supported: {MANIFEST_SCHEMA_VERSION})"
            )
        required_str = (
            "backup_id",
            "created_at_utc",
            "compose_project",
            "database_name",
            "postgres_version",
            "pg_dump_version",
            "archive_format",
            "compression",
            "dump_filename",
            "sha256",
            "tool_version",
        )
        values: dict[str, Any] = {"schema_version": MANIFEST_SCHEMA_VERSION}
        for key in required_str:
            value = data.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ManifestError(f"manifest.{key} must be a non-empty string")
            values[key] = value.strip()

        dump_filename = values["dump_filename"]
        if not is_safe_relative_filename(dump_filename):
            raise ManifestError(f"unsafe dump_filename: {dump_filename!r}")
        if dump_filename != DUMP_FILENAME:
            # Allow only the canonical dump name for v1.
            raise ManifestError(f"unexpected dump_filename: {dump_filename!r}")

        size = data.get("dump_size_bytes")
        if not isinstance(size, int) or size <= 0:
            raise ManifestError("manifest.dump_size_bytes must be a positive int")
        values["dump_size_bytes"] = size

        sha = values["sha256"]
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha.lower()):
            raise ManifestError("manifest.sha256 must be a 64-char hex digest")
        values["sha256"] = sha.lower()

        if values["archive_format"] != ARCHIVE_FORMAT:
            raise ManifestError(
                f"unsupported archive_format: {values['archive_format']!r}"
            )

        git_revision = data.get("git_revision")
        if git_revision is not None and not isinstance(git_revision, str):
            raise ManifestError("manifest.git_revision must be string or null")
        values["git_revision"] = git_revision

        duration = data.get("duration_seconds")
        if not isinstance(duration, (int, float)) or duration < 0:
            raise ManifestError("manifest.duration_seconds must be >= 0")
        values["duration_seconds"] = float(duration)

        structural = StructuralValidation.from_dict(data.get("structural_validation"))  # type: ignore[arg-type]
        if not structural.ok:
            raise ManifestError("manifest structural_validation.ok must be true")
        values["structural_validation"] = structural

        # Reject secret-like keys if somehow present.
        forbidden = {
            "password",
            "database_url",
            "POSTGRES_PASSWORD",
            "env",
            "environment",
        }
        for key in data:
            if (
                key.lower() in {f.lower() for f in forbidden}
                or "password" in key.lower()
            ):
                raise ManifestError(f"manifest must not contain secret field: {key}")

        return cls(**values)

    @classmethod
    def load(cls, path: Path) -> ManifestV1:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ManifestError(f"invalid manifest JSON: {exc}") from exc
        return cls.from_dict(raw)


def write_manifest(path: Path, manifest: ManifestV1) -> None:
    if path.name != MANIFEST_FILENAME:
        raise PathSafetyError(f"manifest must be named {MANIFEST_FILENAME}")
    path.write_text(manifest.to_json(), encoding="utf-8")
