"""ArtifactStore publish / validation / orphan sweep tests."""

from __future__ import annotations

import json
import os
import stat
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fetchnow.downloads.artifacts import MIN_ORPHAN_GRACE_SECONDS, ArtifactStore
from fetchnow.downloads.errors import DownloadError, DownloadErrorCode

_AGED = 7200.0


def _store(
    root: Path, *, max_bytes: int = 1024, grace: int = MIN_ORPHAN_GRACE_SECONDS
) -> ArtifactStore:
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return ArtifactStore(
        root=str(root), max_bytes=max_bytes, orphan_grace_seconds=grace
    )


def test_symlink_output_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path / "root")
    job_id = uuid.uuid4()
    workspace = store.create_attempt_workspace(job_id=job_id, attempt=1, fence=1)
    target = workspace.output / "real.mp4"
    target.write_bytes(b"abc")
    link = workspace.output / "artifact.mp4"
    link.symlink_to(target)
    target.unlink()
    # Only symlink remains.
    with pytest.raises(DownloadError) as exc:
        store.publish(
            workspace=workspace,
            expected_container="mp4",
            job_id=job_id,
            format_option_id="fmt_x",
            expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
        )
    assert exc.value.code == DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT


def test_empty_output_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path / "root")
    job_id = uuid.uuid4()
    workspace = store.create_attempt_workspace(job_id=job_id, attempt=1, fence=1)
    (workspace.output / "artifact.mp4").write_bytes(b"")
    with pytest.raises(DownloadError) as exc:
        store.publish(
            workspace=workspace,
            expected_container="mp4",
            job_id=job_id,
            format_option_id="fmt_x",
            expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
        )
    assert exc.value.code == DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT


def test_oversized_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path / "root", max_bytes=8)
    job_id = uuid.uuid4()
    workspace = store.create_attempt_workspace(job_id=job_id, attempt=1, fence=1)
    (workspace.output / "artifact.mp4").write_bytes(b"0123456789")
    with pytest.raises(DownloadError) as exc:
        store.publish(
            workspace=workspace,
            expected_container="mp4",
            job_id=job_id,
            format_option_id="fmt_x",
            expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
        )
    assert exc.value.code == DownloadErrorCode.DOWNLOAD_TOO_LARGE


def test_atomic_publish(tmp_path: Path) -> None:
    store = _store(tmp_path / "root", max_bytes=1024)
    job_id = uuid.uuid4()
    workspace = store.create_attempt_workspace(job_id=job_id, attempt=1, fence=3)
    assert workspace.home.is_dir()
    assert workspace.cache.is_dir()
    assert workspace.video.is_dir()
    assert workspace.audio.is_dir()
    assert workspace.mux.is_dir()
    assert workspace.output.is_dir()
    (workspace.home / "marker").write_text("home-ok", encoding="utf-8")
    (workspace.cache / "marker").write_text("cache-ok", encoding="utf-8")
    (workspace.output / "artifact.mp4").write_bytes(b"payload-bytes")
    published = store.publish(
        workspace=workspace,
        expected_container="mp4",
        job_id=job_id,
        format_option_id="fmt_x",
        expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
    )
    dest = store.root / "published" / str(published.artifact_id)
    artifact = dest / "artifact.mp4"
    manifest = dest / "manifest.json"
    assert dest.is_dir()
    assert artifact.is_file()
    assert manifest.is_file()
    assert published.size_bytes == len(b"payload-bytes")
    assert published.content_type == "video/mp4"
    assert not any(p.name.startswith(".") for p in (store.root / "published").iterdir())
    # Control dirs under the attempt workspace are not treated as artifacts.
    assert (workspace.home / "marker").read_text(encoding="utf-8") == "home-ok"
    assert (workspace.cache / "marker").read_text(encoding="utf-8") == "cache-ok"


def test_hidden_unexpected_file_in_output_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path / "root")
    job_id = uuid.uuid4()
    workspace = store.create_attempt_workspace(job_id=job_id, attempt=1, fence=1)
    (workspace.output / "artifact.mp4").write_bytes(b"ok")
    (workspace.output / ".foo").write_bytes(b"hidden")
    with pytest.raises(DownloadError) as exc:
        store.publish(
            workspace=workspace,
            expected_container="mp4",
            job_id=job_id,
            format_option_id="fmt_x",
            expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
        )
    assert exc.value.code == DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT


def test_nested_subdirectory_in_output_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path / "root")
    job_id = uuid.uuid4()
    workspace = store.create_attempt_workspace(job_id=job_id, attempt=1, fence=1)
    (workspace.output / "artifact.mp4").write_bytes(b"ok")
    nested = workspace.output / "subdir"
    nested.mkdir()
    (nested / "extra.mp4").write_bytes(b"nope")
    with pytest.raises(DownloadError) as exc:
        store.publish(
            workspace=workspace,
            expected_container="mp4",
            job_id=job_id,
            format_option_id="fmt_x",
            expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
        )
    assert exc.value.code == DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT


def test_wrong_extension_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path / "root")
    job_id = uuid.uuid4()
    workspace = store.create_attempt_workspace(job_id=job_id, attempt=1, fence=1)
    (workspace.output / "artifact.webm").write_bytes(b"payload")
    with pytest.raises(DownloadError) as exc:
        store.publish(
            workspace=workspace,
            expected_container="mp4",
            job_id=job_id,
            format_option_id="fmt_x",
            expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
        )
    assert exc.value.code == DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT


def test_group_writable_artifact_root_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o770)
    with pytest.raises(DownloadError) as exc:
        ArtifactStore(
            root=str(root),
            max_bytes=10,
            orphan_grace_seconds=MIN_ORPHAN_GRACE_SECONDS,
        )
    assert exc.value.code == DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE


def test_symlinked_path_component_under_root_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    outside = tmp_path / "outside"
    outside.mkdir()
    job_id = uuid.uuid4()
    (root / str(job_id)).symlink_to(outside)
    store = ArtifactStore(
        root=str(root),
        max_bytes=1024,
        orphan_grace_seconds=MIN_ORPHAN_GRACE_SECONDS,
    )
    with pytest.raises(DownloadError) as exc:
        store.create_attempt_workspace(job_id=job_id, attempt=1, fence=1)
    assert exc.value.code == DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE


def test_orphan_sweep_stays_in_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    outside.mkdir()
    bait = outside / "secret.txt"
    bait.write_text("do-not-delete", encoding="utf-8")

    store = _store(root, max_bytes=1024)
    job_id = uuid.uuid4()
    workspace = store.create_attempt_workspace(job_id=job_id, attempt=1, fence=9)
    (workspace.output / "artifact.mp4").write_bytes(b"x")
    # Age the workspace beyond grace.
    old = time.time() - _AGED
    os.utime(workspace.path, (old, old))

    removed = store.sweep_orphans(active_fences=frozenset(), limit=16)
    assert removed >= 1
    assert bait.read_text(encoding="utf-8") == "do-not-delete"
    assert not workspace.path.exists()
    # Nothing outside root was touched.
    assert list(outside.iterdir()) == [bait]


def test_temp_root_symlink_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    os.chmod(real, 0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(DownloadError) as exc:
        ArtifactStore(
            root=str(link),
            max_bytes=10,
            orphan_grace_seconds=MIN_ORPHAN_GRACE_SECONDS,
        )
    assert exc.value.code == DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE


def test_reconcile_keeps_referenced_ready_artifact_ids(tmp_path: Path) -> None:
    store = _store(tmp_path / "root", max_bytes=1024, grace=MIN_ORPHAN_GRACE_SECONDS)
    job_id = uuid.uuid4()
    workspace = store.create_attempt_workspace(job_id=job_id, attempt=1, fence=1)
    (workspace.output / "artifact.mp4").write_bytes(b"keep-me")
    published = store.publish(
        workspace=workspace,
        expected_container="mp4",
        job_id=job_id,
        format_option_id="fmt_x",
        expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
    )
    store.cleanup_attempt_workspace(workspace, require_gone=True)
    dest = store.root / "published" / str(published.artifact_id)
    old = time.time() - _AGED
    os.utime(dest, (old, old))
    removed = store.reconcile(
        referenced_artifact_ids=frozenset({published.artifact_id}),
        active_attempts=frozenset(),
        limit=16,
    )
    assert removed == 0
    assert dest.is_dir()


def test_reconcile_keeps_active_job_fence_within_grace(tmp_path: Path) -> None:
    store = _store(tmp_path / "root", max_bytes=1024, grace=3600)
    job_id = uuid.uuid4()
    workspace = store.create_attempt_workspace(job_id=job_id, attempt=2, fence=7)
    (workspace.output / "artifact.mp4").write_bytes(b"active")
    published = store.publish(
        workspace=workspace,
        expected_container="mp4",
        job_id=job_id,
        format_option_id="fmt_x",
        expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
    )
    dest = store.root / "published" / str(published.artifact_id)
    # Aged well past grace: only the active (job_id, fence) claim protects it.
    old = time.time() - _AGED
    os.utime(dest, (old, old))
    removed = store.reconcile(
        referenced_artifact_ids=frozenset(),
        active_attempts=frozenset({(job_id, 7)}),
        limit=16,
    )
    assert removed == 0
    assert dest.is_dir()


def test_reconcile_deletes_unreferenced_published_after_grace(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "root", max_bytes=1024, grace=MIN_ORPHAN_GRACE_SECONDS)
    job_id = uuid.uuid4()
    workspace = store.create_attempt_workspace(job_id=job_id, attempt=1, fence=1)
    (workspace.output / "artifact.mp4").write_bytes(b"orphan")
    published = store.publish(
        workspace=workspace,
        expected_container="mp4",
        job_id=job_id,
        format_option_id="fmt_x",
        expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
    )
    dest = store.root / "published" / str(published.artifact_id)
    old = time.time() - _AGED
    os.utime(dest, (old, old))
    removed = store.reconcile(
        referenced_artifact_ids=frozenset(),
        active_attempts=frozenset(),
        limit=16,
    )
    assert removed >= 1
    assert not dest.exists()


def test_reconcile_deletes_partial_after_grace(tmp_path: Path) -> None:
    store = _store(tmp_path / "root", max_bytes=1024, grace=MIN_ORPHAN_GRACE_SECONDS)
    published = store.root / "published"
    published.mkdir(parents=True, exist_ok=True)
    os.chmod(published, 0o700)
    partial_id = uuid.uuid4()
    partial = published / f".partial_{partial_id}"
    partial.mkdir()
    os.chmod(partial, 0o700)
    (partial / "artifact.mp4").write_bytes(b"staging")
    old = time.time() - _AGED
    os.utime(partial, (old, old))
    removed = store.reconcile(
        referenced_artifact_ids=frozenset(),
        active_attempts=frozenset(),
        limit=16,
    )
    assert removed >= 1
    assert not partial.exists()


def test_reconcile_removes_dir_left_after_delete_artifact_failure(
    tmp_path: Path,
) -> None:
    """Simulate TTL delete failure: published dir remains, then reconcile cleans it."""
    store = _store(tmp_path / "root", max_bytes=1024, grace=MIN_ORPHAN_GRACE_SECONDS)
    job_id = uuid.uuid4()
    workspace = store.create_attempt_workspace(job_id=job_id, attempt=1, fence=1)
    (workspace.output / "artifact.mp4").write_bytes(b"ttl-left")
    published = store.publish(
        workspace=workspace,
        expected_container="mp4",
        job_id=job_id,
        format_option_id="fmt_x",
        expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
    )
    dest = store.root / "published" / str(published.artifact_id)
    assert dest.is_dir()
    # Simulate a failed delete_artifact leaving the directory behind while the
    # DB no longer references the artifact id.
    old = time.time() - _AGED
    os.utime(dest, (old, old))
    removed = store.reconcile(
        referenced_artifact_ids=frozenset(),
        active_attempts=frozenset(),
        limit=16,
    )
    assert removed >= 1
    assert not dest.exists()


def test_root_exact_0700_current_uid_accepted(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    os.chmod(root, 0o700)
    store = ArtifactStore(
        root=str(root),
        max_bytes=1024,
        orphan_grace_seconds=MIN_ORPHAN_GRACE_SECONDS,
    )
    assert store.root == root


@pytest.mark.parametrize(
    "suffix",
    ["/safe/../target", "/safe/./target", "/safe//target", "/safe/target/"],
)
def test_non_normalized_root_rejected(tmp_path: Path, suffix: str) -> None:
    # Build a real 0700 target so only the path spelling can fail the check.
    safe = tmp_path / "safe"
    target = safe / "target"
    target.mkdir(parents=True)
    os.chmod(safe, 0o700)
    os.chmod(target, 0o700)
    with pytest.raises(DownloadError) as exc:
        ArtifactStore(
            root=f"{tmp_path}{suffix}",
            max_bytes=1024,
            orphan_grace_seconds=MIN_ORPHAN_GRACE_SECONDS,
        )
    assert exc.value.code == DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE
    assert exc.value.internal_reason == "TEMP_ROOT_NOT_NORMALIZED"


def test_already_normalized_absolute_root_accepted(tmp_path: Path) -> None:
    target = tmp_path / "safe" / "target"
    target.mkdir(parents=True)
    os.chmod(target.parent, 0o700)
    os.chmod(target, 0o700)
    store = ArtifactStore(
        root=str(target),
        max_bytes=1024,
        orphan_grace_seconds=MIN_ORPHAN_GRACE_SECONDS,
    )
    assert store.root == target


def test_root_mode_0755_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    os.chmod(root, 0o755)
    with pytest.raises(DownloadError) as exc:
        ArtifactStore(
            root=str(root),
            max_bytes=1024,
            orphan_grace_seconds=MIN_ORPHAN_GRACE_SECONDS,
        )
    assert exc.value.code == DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE
    # An operator-provided root is never silently repaired.
    assert stat.S_IMODE(root.stat().st_mode) == 0o755


def test_root_owned_by_other_uid_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    os.chmod(root, 0o700)
    monkeypatch.setattr(os, "getuid", lambda: os.stat(root).st_uid + 1)
    with pytest.raises(DownloadError) as exc:
        ArtifactStore(
            root=str(root),
            max_bytes=1024,
            orphan_grace_seconds=MIN_ORPHAN_GRACE_SECONDS,
        )
    assert exc.value.code == DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE


def test_root_with_symlinked_parent_rejected(tmp_path: Path) -> None:
    real_parent = tmp_path / "real_parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked_parent"
    linked_parent.symlink_to(real_parent)
    root = real_parent / "root"
    root.mkdir()
    os.chmod(root, 0o700)
    # Final component is a real 0700 dir; only an interior component is a link.
    with pytest.raises(DownloadError) as exc:
        ArtifactStore(
            root=str(linked_parent / "root"),
            max_bytes=1024,
            orphan_grace_seconds=MIN_ORPHAN_GRACE_SECONDS,
        )
    assert exc.value.code == DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE


def test_orphan_grace_zero_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    os.chmod(root, 0o700)
    with pytest.raises(DownloadError) as exc:
        ArtifactStore(root=str(root), max_bytes=1024, orphan_grace_seconds=0)
    assert exc.value.code == DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE


def test_reconcile_keeps_new_cross_worker_publication(tmp_path: Path) -> None:
    """A publication another worker just made must survive the grace window."""
    store = _store(tmp_path / "root", max_bytes=1024)
    job_id = uuid.uuid4()
    workspace = store.create_attempt_workspace(job_id=job_id, attempt=1, fence=1)
    (workspace.output / "artifact.mp4").write_bytes(b"fresh")
    published = store.publish(
        workspace=workspace,
        expected_container="mp4",
        job_id=job_id,
        format_option_id="fmt_x",
        expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
    )
    store.cleanup_attempt_workspace(workspace, require_gone=True)
    dest = store.root / "published" / str(published.artifact_id)

    # The peer has not yet committed ready, so the DB references nothing.
    removed = store.reconcile(
        referenced_artifact_ids=frozenset(),
        active_attempts=frozenset(),
        limit=16,
    )
    assert removed == 0
    assert (dest / "artifact.mp4").is_file()


def test_reconcile_unlinks_aged_published_symlink_without_following(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secret.mp4"
    target.write_bytes(b"external-bytes")
    target_mode = stat.S_IMODE(target.stat().st_mode)

    store = _store(tmp_path / "root", max_bytes=1024)
    published_dir = store.root / "published"
    published_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(published_dir, 0o700)
    link = published_dir / str(uuid.uuid4())
    link.symlink_to(target)
    old = time.time() - _AGED
    os.utime(link, (old, old), follow_symlinks=False)

    removed = store.reconcile(
        referenced_artifact_ids=frozenset(),
        active_attempts=frozenset(),
        limit=16,
    )

    assert removed >= 1
    # The link itself is gone; its target is untouched.
    assert not link.is_symlink()
    assert not link.exists()
    assert target.read_bytes() == b"external-bytes"
    assert stat.S_IMODE(target.stat().st_mode) == target_mode


def test_reconcile_unlinks_aged_symlinks_in_root_and_job_dirs(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("intact", encoding="utf-8")

    store = _store(tmp_path / "root", max_bytes=1024)
    job_id = uuid.uuid4()
    workspace = store.create_attempt_workspace(job_id=job_id, attempt=1, fence=1)
    job_dir = workspace.path.parent

    root_link = store.root / "stray-link"
    root_link.symlink_to(outside)
    attempt_link = job_dir / "9_9"
    attempt_link.symlink_to(outside)
    old = time.time() - _AGED
    for link in (root_link, attempt_link):
        os.utime(link, (old, old), follow_symlinks=False)
    os.utime(workspace.path, (old, old))

    store.reconcile(
        referenced_artifact_ids=frozenset(),
        active_attempts=frozenset(),
        limit=64,
    )

    assert not root_link.is_symlink()
    assert not attempt_link.is_symlink()
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "intact"


def test_swapped_symlink_is_never_chmodded_opened_or_published(
    tmp_path: Path,
) -> None:
    """An entry swapped for a symlink after listing must not reach the target."""
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "victim.mp4"
    target.write_bytes(b"external-secret")
    os.chmod(target, 0o644)

    store = _store(tmp_path / "root", max_bytes=1024)
    job_id = uuid.uuid4()
    workspace = store.create_attempt_workspace(job_id=job_id, attempt=1, fence=1)
    (workspace.output / "artifact.mp4").write_bytes(b"legit-bytes")

    real_find = ArtifactStore._find_single_regular_file

    def _swap_then_return(
        self: ArtifactStore, ws: object, *, expected_container: str
    ) -> Path:
        found = real_find(self, ws, expected_container=expected_container)
        # Adversary wins the race between listing and opening.
        found.unlink()
        found.symlink_to(target)
        return found

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ArtifactStore, "_find_single_regular_file", _swap_then_return)
        with pytest.raises(DownloadError) as exc:
            store.publish(
                workspace=workspace,
                expected_container="mp4",
                job_id=job_id,
                format_option_id="fmt_x",
                expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
            )

    assert exc.value.code == DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT
    assert target.read_bytes() == b"external-secret"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    published_dir = store.root / "published"
    assert not published_dir.exists() or not any(published_dir.iterdir())


def test_partial_os_write_does_not_truncate_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"a" * 300
    store = _store(tmp_path / "root", max_bytes=4096)
    job_id = uuid.uuid4()
    workspace = store.create_attempt_workspace(job_id=job_id, attempt=1, fence=1)
    (workspace.output / "artifact.mp4").write_bytes(payload)

    real_write = os.write

    def _short_write(fd: int, data: bytes) -> int:
        # Emulate a kernel that accepts only part of each buffer.
        return real_write(fd, bytes(data)[:7])

    monkeypatch.setattr(os, "write", _short_write)
    published = store.publish(
        workspace=workspace,
        expected_container="mp4",
        job_id=job_id,
        format_option_id="fmt_x",
        expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
    )
    monkeypatch.undo()

    dest = store.root / "published" / str(published.artifact_id)
    assert (dest / "artifact.mp4").read_bytes() == payload
    assert published.size_bytes == len(payload)
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["size"] == len(payload)


def test_mux_stream_dir_rejects_symlink_and_fifo(tmp_path: Path) -> None:
    store = _store(tmp_path / "root")
    job_id = uuid.uuid4()
    workspace = store.create_attempt_workspace(job_id=job_id, attempt=1, fence=1)
    target = workspace.video / "real.mp4"
    target.write_bytes(b"abc")
    link = workspace.video / "stream.mp4"
    link.symlink_to(target)
    target.unlink()
    with pytest.raises(DownloadError) as exc:
        store.find_single_regular_file_in(
            workspace.video, allowed_suffixes=frozenset({"mp4"})
        )
    assert exc.value.code == DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT
    fifo = workspace.audio / "stream.m4a"
    os.mkfifo(fifo)
    with pytest.raises(DownloadError) as fifo_exc:
        store.find_single_regular_file_in(
            workspace.audio, allowed_suffixes=frozenset({"m4a"})
        )
    assert fifo_exc.value.code == DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT
    part = workspace.video / "stream.mp4.part"
    (workspace.video / "ok.mp4").write_bytes(b"abc")
    part.write_bytes(b"partial")
    with pytest.raises(DownloadError) as part_exc:
        store.find_single_regular_file_in(
            workspace.video, allowed_suffixes=frozenset({"mp4"})
        )
    assert part_exc.value.code == DownloadErrorCode.DOWNLOAD_INVALID_OUTPUT


def test_stage_muxed_output_moves_without_second_copy(tmp_path: Path) -> None:
    store = _store(tmp_path / "root")
    job_id = uuid.uuid4()
    workspace = store.create_attempt_workspace(job_id=job_id, attempt=1, fence=1)
    payload = b"muxed-artifact!!"
    mux_path = workspace.mux / "artifact.mp4"
    mux_path.write_bytes(payload)
    os.chmod(mux_path, 0o600)
    dest = store.stage_muxed_output(workspace, mux_path, container="mp4")
    assert dest == workspace.output / "artifact.mp4"
    assert dest.read_bytes() == payload
    assert not mux_path.exists()
    assert dest.stat().st_nlink == 1
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
