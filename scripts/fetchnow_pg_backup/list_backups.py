"""List published backups (read-only)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import INCOMPLETE_PREFIX, MANIFEST_FILENAME
from .attestation import latest_attestation
from .fs_safety import validate_backup_root
from .manifest import ManifestError, ManifestV1


@dataclass(frozen=True)
class BackupListItem:
    backup_id: str
    path: Path
    size_bytes: int | None
    sha256_prefix: str | None
    git_revision: str | None
    structural_ok: bool | None
    verify_result: str | None
    verify_at: str | None
    status: str  # valid | invalid | incomplete | unknown


@dataclass(frozen=True)
class ListResult:
    items: list[BackupListItem]
    invalid_entries: list[str]


def list_backups(*, backup_root: Path, repo_root: Path) -> ListResult:
    root = validate_backup_root(backup_root, repo_root=repo_root)
    items: list[BackupListItem] = []
    invalid: list[str] = []
    if not root.exists():
        return ListResult(items=[], invalid_entries=[])
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        name = child.name
        if name in {".", "..", ".lock"}:
            continue
        if child.is_symlink():
            invalid.append(f"symlink:{name}")
            items.append(
                BackupListItem(
                    backup_id=name,
                    path=child,
                    size_bytes=None,
                    sha256_prefix=None,
                    git_revision=None,
                    structural_ok=None,
                    verify_result=None,
                    verify_at=None,
                    status="unknown",
                )
            )
            continue
        if name.startswith(INCOMPLETE_PREFIX):
            items.append(
                BackupListItem(
                    backup_id=name,
                    path=child,
                    size_bytes=None,
                    sha256_prefix=None,
                    git_revision=None,
                    structural_ok=None,
                    verify_result=None,
                    verify_at=None,
                    status="incomplete",
                )
            )
            continue
        if not child.is_dir():
            invalid.append(f"non-dir:{name}")
            items.append(
                BackupListItem(
                    backup_id=name,
                    path=child,
                    size_bytes=None,
                    sha256_prefix=None,
                    git_revision=None,
                    structural_ok=None,
                    verify_result=None,
                    verify_at=None,
                    status="unknown",
                )
            )
            continue
        manifest_path = child / MANIFEST_FILENAME
        if not manifest_path.is_file():
            invalid.append(f"missing-manifest:{name}")
            items.append(
                BackupListItem(
                    backup_id=name,
                    path=child,
                    size_bytes=None,
                    sha256_prefix=None,
                    git_revision=None,
                    structural_ok=None,
                    verify_result=None,
                    verify_at=None,
                    status="invalid",
                )
            )
            continue
        try:
            manifest = ManifestV1.load(manifest_path)
        except ManifestError as exc:
            invalid.append(f"bad-manifest:{name}:{exc}")
            items.append(
                BackupListItem(
                    backup_id=name,
                    path=child,
                    size_bytes=None,
                    sha256_prefix=None,
                    git_revision=None,
                    structural_ok=None,
                    verify_result=None,
                    verify_at=None,
                    status="invalid",
                )
            )
            continue
        att = latest_attestation(child)
        items.append(
            BackupListItem(
                backup_id=manifest.backup_id,
                path=child,
                size_bytes=manifest.dump_size_bytes,
                sha256_prefix=manifest.sha256[:12],
                git_revision=manifest.git_revision,
                structural_ok=manifest.structural_validation.ok,
                verify_result=(att or {}).get("result"),
                verify_at=(att or {}).get("verified_at_utc"),
                status="valid",
            )
        )
    return ListResult(items=items, invalid_entries=invalid)
