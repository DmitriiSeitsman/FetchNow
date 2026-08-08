"""Count-based retention / prune (default dry-run)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import DEFAULT_KEEP_COUNT, MANIFEST_FILENAME
from .attestation import latest_attestation
from .fs_safety import (
    PathSafetyError,
    assert_direct_child,
    remove_directory_tree,
    validate_backup_root,
)
from .list_backups import list_backups
from .locking import BackupRootLock
from .manifest import ManifestError, ManifestV1
from .retention_hold import RetentionHoldError, active_hold_for_backup


@dataclass(frozen=True)
class PruneDecision:
    backup_id: str
    action: str  # keep | delete | skip
    reason: str


@dataclass(frozen=True)
class PruneResult:
    decisions: list[PruneDecision]
    deleted: list[str]
    dry_run: bool


def select_prune_candidates(
    *,
    backup_root: Path,
    repo_root: Path,
    keep: int = DEFAULT_KEEP_COUNT,
) -> list[PruneDecision]:
    if keep < 1:
        raise ValueError("keep must be >= 1")
    listed = list_backups(backup_root=backup_root, repo_root=repo_root)
    valid = [i for i in listed.items if i.status == "valid"]
    # Sort newest-first by backup_id (UTC timestamp prefix).
    valid_sorted = sorted(valid, key=lambda i: i.backup_id, reverse=True)

    newest_valid = valid_sorted[0].backup_id if valid_sorted else None
    newest_verified = None
    for item in valid_sorted:
        if item.verify_result == "passed":
            newest_verified = item.backup_id
            break

    decisions: list[PruneDecision] = []
    # Non-valid entries are never deletion candidates.
    for item in listed.items:
        if item.status != "valid":
            decisions.append(
                PruneDecision(
                    backup_id=item.backup_id,
                    action="skip",
                    reason=f"status={item.status}",
                )
            )

    deletable_ids = {i.backup_id for i in valid_sorted[keep:]}
    for item in valid_sorted:
        if item.backup_id == newest_valid:
            decisions.append(
                PruneDecision(item.backup_id, "keep", "newest valid backup")
            )
            continue
        if newest_verified and item.backup_id == newest_verified:
            decisions.append(
                PruneDecision(
                    item.backup_id,
                    "keep",
                    "newest successfully restore-verified backup",
                )
            )
            continue
        try:
            held = active_hold_for_backup(backup_root, item.backup_id)
        except RetentionHoldError:
            decisions.append(
                PruneDecision(
                    item.backup_id,
                    "skip",
                    reason="malformed retention hold (fail closed)",
                )
            )
            continue
        if held is not None:
            decisions.append(
                PruneDecision(
                    item.backup_id,
                    "keep",
                    f"active retention hold {held.hold_id}",
                )
            )
            continue
        if item.backup_id in deletable_ids:
            decisions.append(
                PruneDecision(
                    item.backup_id,
                    "delete",
                    f"beyond keep={keep} and not protected",
                )
            )
        else:
            decisions.append(
                PruneDecision(item.backup_id, "keep", f"within keep={keep}")
            )
    return decisions


def prune_backups(
    *,
    backup_root: Path,
    repo_root: Path,
    keep: int = DEFAULT_KEEP_COUNT,
    apply: bool = False,
    wait_lock: bool = False,
) -> PruneResult:
    root = validate_backup_root(backup_root, repo_root=repo_root)
    decisions = select_prune_candidates(
        backup_root=root, repo_root=repo_root, keep=keep
    )
    deleted: list[str] = []
    if not apply:
        return PruneResult(decisions=decisions, deleted=[], dry_run=True)

    errors: list[str] = []
    with BackupRootLock(root, wait=wait_lock):
        # Recompute under lock.
        decisions = select_prune_candidates(
            backup_root=root, repo_root=repo_root, keep=keep
        )
        for decision in decisions:
            if decision.action != "delete":
                continue
            child = root / decision.backup_id
            try:
                path = assert_direct_child(child, root)
                if path.is_symlink() or not path.is_dir():
                    raise PathSafetyError("refusing non-directory or symlink delete")
                # Must still have a valid manifest at delete time.
                ManifestV1.load(path / MANIFEST_FILENAME)
                held = active_hold_for_backup(root, decision.backup_id)
                if held is not None:
                    raise PathSafetyError(
                        f"refusing to delete backup with active hold {held.hold_id}"
                    )
                att = latest_attestation(path)
                # Re-affirm protections.
                if decision.reason.startswith("newest"):
                    raise PathSafetyError("refusing to delete protected backup")
                remove_directory_tree(path)
                deleted.append(decision.backup_id)
                _ = att  # attestation already considered in selection
            except (OSError, PathSafetyError, ManifestError) as exc:
                errors.append(f"{decision.backup_id}: {exc}")
    if errors:
        raise RuntimeError("prune partially failed: " + "; ".join(errors))
    return PruneResult(decisions=decisions, deleted=deleted, dry_run=False)
