"""Offline media storage contract tests (kernel findmnt stubbed)."""

from __future__ import annotations

import errno
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from fetchnow_release.media_prerequisites import (  # noqa: E402
    MediaPrerequisiteError,
    check_media_prerequisites,
)
from fetchnow_release.media_storage import (  # noqa: E402
    CALCULATED_CAPACITY_BYTES,
    MEDIA_ROOT_TARGETS,
    MediaStorageError,
    MountIdentity,
    assert_media_storage_ready,
    calculated_capacity_bytes,
    compose_bind_volume_specs,
    disposable_test_storage_contract,
    parse_media_storage_contract,
    peak_disk_breakdown,
)
from fetchnow_release.resource_budget import disposable_test_budget  # noqa: E402


def _layout(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name in ("tmp", "executor", "media-net"):
        (root / name).mkdir(exist_ok=True)


def _identity(
    path: Path, *, uuid: str = "00000000-0000-4000-8000-000000000001"
) -> MountIdentity:
    return MountIdentity(
        target=str(path),
        source="/dev/testdisk",
        fstype="ext4",
        fs_uuid=uuid,
        options="rw",
        size_bytes=CALCULATED_CAPACITY_BYTES,
    )


def _statfs_ok(_path: Path) -> SimpleNamespace:
    # blocks * frsize ~= capacity with spare free (>= 3×MAX operation headroom)
    frsize = 4096
    blocks = CALCULATED_CAPACITY_BYTES // frsize
    bavail = (12 * 1024 * 1024 * 1024) // frsize
    return SimpleNamespace(f_blocks=blocks, f_frsize=frsize, f_bavail=bavail)


def test_peak_capacity_is_deterministic() -> None:
    peak = peak_disk_breakdown()
    assert peak["capacity_bytes"] == calculated_capacity_bytes()
    assert peak["capacity_bytes"] == CALCULATED_CAPACITY_BYTES
    assert peak["max_artifact_bytes"] == 3_221_225_472


def test_unapproved_contract_rejected() -> None:
    raw = disposable_test_storage_contract(host_bind_root="/media")
    raw["approved"] = False
    with pytest.raises(MediaStorageError, match="approved"):
        parse_media_storage_contract(raw)


def test_wrong_capacity_rejected() -> None:
    raw = disposable_test_storage_contract(host_bind_root="/media")
    raw["capacity_bytes"] = 1
    with pytest.raises(MediaStorageError, match="capacity_bytes"):
        parse_media_storage_contract(raw)


def test_ready_happy_path(tmp_path: Path) -> None:
    root = tmp_path / "media"
    _layout(root)
    contract = parse_media_storage_contract(
        disposable_test_storage_contract(host_bind_root=str(root))
    )

    def findmnt(path: Path) -> MountIdentity | None:
        if path == Path("/"):
            return MountIdentity("/", "/dev/root", "ext4", "root-uuid", "rw", None)
        return _identity(root)

    messages = assert_media_storage_ready(contract, findmnt=findmnt, statfs=_statfs_ok)
    assert any("dedicated FS" in msg or "capacity" in msg for msg in messages)


def test_missing_mount_is_root_fs_fallback(tmp_path: Path) -> None:
    root = tmp_path / "media"
    _layout(root)
    contract = parse_media_storage_contract(
        disposable_test_storage_contract(host_bind_root=str(root))
    )

    def findmnt(path: Path) -> MountIdentity | None:
        # Everything looks like root FS.
        return MountIdentity("/", "/dev/root", "ext4", "root-uuid", "rw", None)

    with pytest.raises(MediaStorageError, match="root filesystem"):
        assert_media_storage_ready(contract, findmnt=findmnt, statfs=_statfs_ok)


def test_wrong_identity(tmp_path: Path) -> None:
    root = tmp_path / "media"
    _layout(root)
    contract = parse_media_storage_contract(
        disposable_test_storage_contract(host_bind_root=str(root))
    )

    def findmnt(path: Path) -> MountIdentity | None:
        if path == Path("/"):
            return MountIdentity("/", "/dev/root", "ext4", "root-uuid", "rw", None)
        return _identity(root, uuid="11111111-1111-4111-8111-111111111111")

    with pytest.raises(MediaStorageError, match="UUID"):
        assert_media_storage_ready(contract, findmnt=findmnt, statfs=_statfs_ok)


def test_missing_layout_dir_refuses_auto_create(tmp_path: Path) -> None:
    root = tmp_path / "media"
    root.mkdir()
    (root / "tmp").mkdir()
    # executor + media-net missing
    contract = parse_media_storage_contract(
        disposable_test_storage_contract(host_bind_root=str(root))
    )

    def findmnt(path: Path) -> MountIdentity | None:
        if path == Path("/"):
            return MountIdentity("/", "/dev/root", "ext4", "root-uuid", "rw", None)
        return _identity(root)

    with pytest.raises(MediaStorageError, match="layout.executor"):
        assert_media_storage_ready(contract, findmnt=findmnt, statfs=_statfs_ok)


def test_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "media"
    _layout(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "tmp").rmdir()
    (root / "tmp").symlink_to(outside)
    contract = parse_media_storage_contract(
        disposable_test_storage_contract(host_bind_root=str(root))
    )

    def findmnt(path: Path) -> MountIdentity | None:
        if path == Path("/"):
            return MountIdentity("/", "/dev/root", "ext4", "root-uuid", "rw", None)
        return _identity(root)

    with pytest.raises(MediaStorageError, match="symlink"):
        assert_media_storage_ready(contract, findmnt=findmnt, statfs=_statfs_ok)


def test_insufficient_free(tmp_path: Path) -> None:
    root = tmp_path / "media"
    _layout(root)
    contract = parse_media_storage_contract(
        disposable_test_storage_contract(host_bind_root=str(root))
    )

    def findmnt(path: Path) -> MountIdentity | None:
        if path == Path("/"):
            return MountIdentity("/", "/dev/root", "ext4", "root-uuid", "rw", None)
        return _identity(root)

    def tiny_free(_path: Path) -> SimpleNamespace:
        frsize = 4096
        blocks = CALCULATED_CAPACITY_BYTES // frsize
        return SimpleNamespace(f_blocks=blocks, f_frsize=frsize, f_bavail=1)

    with pytest.raises(MediaStorageError, match="insufficient free"):
        assert_media_storage_ready(contract, findmnt=findmnt, statfs=tiny_free)


def test_bind_volume_specs_point_at_layout(tmp_path: Path) -> None:
    root = tmp_path / "media"
    contract = parse_media_storage_contract(
        disposable_test_storage_contract(host_bind_root=str(root))
    )
    specs = compose_bind_volume_specs(contract)
    assert set(specs) == {"tmp", "executor-work", "media-net-work"}
    assert specs["tmp"]["driver_opts"]["device"] == str(root / "tmp")
    assert MEDIA_ROOT_TARGETS["tmp"] == "/var/lib/fetchnow/tmp"


def test_prerequisites_require_storage_when_requested(tmp_path: Path) -> None:
    with pytest.raises(MediaPrerequisiteError, match="media_storage_contract"):
        check_media_prerequisites(
            budget_raw=disposable_test_budget().to_dict(),
            require_storage=True,
            storage_contract_raw=None,
            skip_landlock=False,
        )


def test_proposal_json_remains_unapproved() -> None:
    proposal = json.loads(
        (ROOT / "docs/operations/sec-08-09-media-storage-contract.json").read_text(
            encoding="utf-8"
        )
    )
    assert proposal["approved"] is False
    assert proposal["activation_contract"]["approved"] is False
    assert (
        proposal["activation_contract"]["capacity_bytes"] == CALCULATED_CAPACITY_BYTES
    )
    budget = json.loads(
        (ROOT / "docs/operations/sec-08-09-resource-budget-proposal.json").read_text(
            encoding="utf-8"
        )
    )
    assert budget["approved"] is False
    assert budget["recommended_budget"]["approved"] is False
    assert budget["recommended_budget"]["shared_ceiling"] is False
    assert (
        budget["recommended_budget"]["aggregate_memory_max_bytes"]
        == (768 + 1152 + 256) * 1024 * 1024
    )


def test_handoff_enospc_maps_to_storage_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sys.path.insert(0, str(ROOT / "backend" / "src"))
    from fetchnow.media_executor.handoff import HandoffError, copy_regular

    src = tmp_path / "src.bin"
    src.write_bytes(b"abc")
    dest = tmp_path / "dest.bin"
    real_open = os.open

    def selective_open(
        path: str | bytes | os.PathLike[str], flags: int, mode: int = 0o777
    ) -> int:
        if flags & os.O_CREAT:
            err = OSError("no space")
            err.errno = errno.ENOSPC
            raise err
        return real_open(path, flags)

    monkeypatch.setattr(os, "open", selective_open)
    with pytest.raises(HandoffError) as exc:
        copy_regular(src, dest, max_bytes=1024)
    assert exc.value.code == "storage_exhausted"
    assert not dest.exists()
