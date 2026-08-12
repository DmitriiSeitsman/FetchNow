"""ArtifactReader fail-closed filesystem tests."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fetchnow.delivery.reader import ArtifactReader
from fetchnow.downloads.artifacts import MIN_ORPHAN_GRACE_SECONDS, ArtifactStore
from fetchnow.downloads.errors import DownloadError, DownloadErrorCode


def _prepare_root(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _publish(
    root: Path,
    *,
    payload: bytes = b"delivery-bytes-payload",
    container: str = "mp4",
) -> tuple[uuid.UUID, uuid.UUID, datetime, int, str]:
    store = ArtifactStore(
        root=str(root),
        max_bytes=1024 * 1024,
        orphan_grace_seconds=MIN_ORPHAN_GRACE_SECONDS,
    )
    job_id = uuid.uuid4()
    workspace = store.create_attempt_workspace(job_id=job_id, attempt=1, fence=3)
    (workspace.output / f"artifact.{container}").write_bytes(payload)
    expires = datetime.now(tz=UTC) + timedelta(hours=1)
    published = store.publish(
        workspace=workspace,
        expected_container=container,
        job_id=job_id,
        format_option_id="fmt_deliv",
        expires_at=expires,
    )
    return job_id, published.artifact_id, expires, 3, published.content_type


def test_happy_path_open(tmp_path: Path) -> None:
    root = _prepare_root(tmp_path / "root")
    job_id, artifact_id, expires, fence, content_type = _publish(root)
    reader = ArtifactReader(str(root))
    with reader.open_published(
        artifact_id=artifact_id,
        download_job_id=job_id,
        format_option_id="fmt_deliv",
        expected_bytes=len(b"delivery-bytes-payload"),
        expected_container="mp4",
        expected_content_type=content_type,
        expected_expires_at=expires,
        expected_fence=fence,
    ) as handle:
        data = os.read(handle.fd, 1024)
    assert data == b"delivery-bytes-payload"


@pytest.mark.parametrize(
    "suffix",
    ["/safe/../target", "/safe/./target", "/safe//target", "/safe/target/"],
)
def test_root_non_normalized_rejected(tmp_path: Path, suffix: str) -> None:
    safe = tmp_path / "safe"
    target = safe / "target"
    target.mkdir(parents=True)
    os.chmod(safe, 0o700)
    os.chmod(target, 0o700)
    with pytest.raises(DownloadError) as exc:
        ArtifactReader(f"{tmp_path}{suffix}")
    assert exc.value.code == DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE


def test_root_0755_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    os.chmod(root, 0o755)
    with pytest.raises(DownloadError):
        ArtifactReader(str(root))


def test_root_wrong_owner_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _prepare_root(tmp_path / "root")
    monkeypatch.setattr(os, "getuid", lambda: os.stat(root).st_uid + 1)
    with pytest.raises(DownloadError):
        ArtifactReader(str(root))


def test_root_symlinked_ancestor_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real)
    root = real / "root"
    root.mkdir()
    os.chmod(root, 0o700)
    with pytest.raises(DownloadError):
        ArtifactReader(str(linked / "root"))


def test_artifact_directory_symlink_rejected(tmp_path: Path) -> None:
    root = _prepare_root(tmp_path / "root")
    job_id, artifact_id, expires, fence, content_type = _publish(root)
    published = root / "published" / str(artifact_id)
    outside = tmp_path / "outside"
    outside.mkdir()
    # Replace published dir with symlink.
    for child in published.iterdir():
        child.unlink()
    published.rmdir()
    published.symlink_to(outside)
    reader = ArtifactReader(str(root))
    with pytest.raises(DownloadError):
        reader.open_published(
            artifact_id=artifact_id,
            download_job_id=job_id,
            format_option_id="fmt_deliv",
            expected_bytes=len(b"delivery-bytes-payload"),
            expected_container="mp4",
            expected_content_type=content_type,
            expected_expires_at=expires,
            expected_fence=fence,
        )


def test_artifact_symlink_rejected_external_untouched(tmp_path: Path) -> None:
    root = _prepare_root(tmp_path / "root")
    job_id, artifact_id, expires, fence, content_type = _publish(root)
    published = root / "published" / str(artifact_id)
    target = tmp_path / "external.mp4"
    target.write_bytes(b"secret-external")
    os.chmod(target, 0o644)
    artifact = published / "artifact.mp4"
    artifact.unlink()
    artifact.symlink_to(target)
    reader = ArtifactReader(str(root))
    with pytest.raises(DownloadError):
        reader.open_published(
            artifact_id=artifact_id,
            download_job_id=job_id,
            format_option_id="fmt_deliv",
            expected_bytes=len(b"delivery-bytes-payload"),
            expected_container="mp4",
            expected_content_type=content_type,
            expected_expires_at=expires,
            expected_fence=fence,
        )
    assert target.read_bytes() == b"secret-external"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_manifest_symlink_rejected(tmp_path: Path) -> None:
    root = _prepare_root(tmp_path / "root")
    job_id, artifact_id, expires, fence, content_type = _publish(root)
    published = root / "published" / str(artifact_id)
    bait = tmp_path / "bait.json"
    bait.write_text("{}", encoding="utf-8")
    manifest = published / "manifest.json"
    manifest.unlink()
    manifest.symlink_to(bait)
    reader = ArtifactReader(str(root))
    with pytest.raises(DownloadError):
        reader.open_published(
            artifact_id=artifact_id,
            download_job_id=job_id,
            format_option_id="fmt_deliv",
            expected_bytes=len(b"delivery-bytes-payload"),
            expected_container="mp4",
            expected_content_type=content_type,
            expected_expires_at=expires,
            expected_fence=fence,
        )


def test_hardlink_rejected(tmp_path: Path) -> None:
    root = _prepare_root(tmp_path / "root")
    job_id, artifact_id, expires, fence, content_type = _publish(root)
    published = root / "published" / str(artifact_id)
    artifact = published / "artifact.mp4"
    link = published / "extra.mp4"
    os.link(artifact, link)
    reader = ArtifactReader(str(root))
    with pytest.raises(DownloadError) as exc:
        reader.open_published(
            artifact_id=artifact_id,
            download_job_id=job_id,
            format_option_id="fmt_deliv",
            expected_bytes=len(b"delivery-bytes-payload"),
            expected_container="mp4",
            expected_content_type=content_type,
            expected_expires_at=expires,
            expected_fence=fence,
        )
    assert exc.value.code == DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE
    link.unlink()


def test_fifo_rejected(tmp_path: Path) -> None:
    root = _prepare_root(tmp_path / "root")
    job_id, artifact_id, expires, fence, content_type = _publish(root)
    published = root / "published" / str(artifact_id)
    artifact = published / "artifact.mp4"
    artifact.unlink()
    os.mkfifo(artifact)
    reader = ArtifactReader(str(root))
    with pytest.raises(DownloadError):
        reader.open_published(
            artifact_id=artifact_id,
            download_job_id=job_id,
            format_option_id="fmt_deliv",
            expected_bytes=len(b"delivery-bytes-payload"),
            expected_container="mp4",
            expected_content_type=content_type,
            expected_expires_at=expires,
            expected_fence=fence,
        )


def test_manifest_unknown_key_rejected(tmp_path: Path) -> None:
    root = _prepare_root(tmp_path / "root")
    job_id, artifact_id, expires, fence, content_type = _publish(root)
    manifest_path = root / "published" / str(artifact_id) / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["extra"] = "nope"
    os.chmod(manifest_path, 0o600)
    flags = os.O_WRONLY | os.O_TRUNC
    fd = os.open(manifest_path, flags)
    try:
        os.write(fd, json.dumps(payload).encode())
    finally:
        os.close(fd)
    os.chmod(manifest_path, 0o600)
    reader = ArtifactReader(str(root))
    with pytest.raises(DownloadError):
        reader.open_published(
            artifact_id=artifact_id,
            download_job_id=job_id,
            format_option_id="fmt_deliv",
            expected_bytes=len(b"delivery-bytes-payload"),
            expected_container="mp4",
            expected_content_type=content_type,
            expected_expires_at=expires,
            expected_fence=fence,
        )


def test_size_mismatch_rejected(tmp_path: Path) -> None:
    root = _prepare_root(tmp_path / "root")
    job_id, artifact_id, expires, fence, content_type = _publish(root)
    reader = ArtifactReader(str(root))
    with pytest.raises(DownloadError):
        reader.open_published(
            artifact_id=artifact_id,
            download_job_id=job_id,
            format_option_id="fmt_deliv",
            expected_bytes=1,
            expected_container="mp4",
            expected_content_type=content_type,
            expected_expires_at=expires,
            expected_fence=fence,
        )


def test_job_id_mismatch_rejected(tmp_path: Path) -> None:
    root = _prepare_root(tmp_path / "root")
    _job_id, artifact_id, expires, fence, content_type = _publish(root)
    reader = ArtifactReader(str(root))
    with pytest.raises(DownloadError):
        reader.open_published(
            artifact_id=artifact_id,
            download_job_id=uuid.uuid4(),
            format_option_id="fmt_deliv",
            expected_bytes=len(b"delivery-bytes-payload"),
            expected_container="mp4",
            expected_content_type=content_type,
            expected_expires_at=expires,
            expected_fence=fence,
        )


def test_swapped_symlink_never_exposes_external(tmp_path: Path) -> None:
    root = _prepare_root(tmp_path / "root")
    job_id, artifact_id, expires, fence, content_type = _publish(root)
    published = root / "published" / str(artifact_id)
    target = tmp_path / "victim.mp4"
    target.write_bytes(b"external-secret")
    os.chmod(target, 0o644)

    real_open = ArtifactReader._open_regular_child

    def _swap_then_open(
        self: ArtifactReader,
        parent_fd: int,
        name: str,
        *,
        require_mode: int,
    ) -> int:
        if name.startswith("artifact."):
            path = published / name
            path.unlink()
            path.symlink_to(target)
        return real_open(self, parent_fd, name, require_mode=require_mode)

    reader = ArtifactReader(str(root))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ArtifactReader, "_open_regular_child", _swap_then_open)
        with pytest.raises(DownloadError):
            reader.open_published(
                artifact_id=artifact_id,
                download_job_id=job_id,
                format_option_id="fmt_deliv",
                expected_bytes=len(b"delivery-bytes-payload"),
                expected_container="mp4",
                expected_content_type=content_type,
                expected_expires_at=expires,
                expected_fence=fence,
            )
    assert target.read_bytes() == b"external-secret"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_reader_has_no_mutating_helpers() -> None:
    banned = {
        "mkdir",
        "makedirs",
        "chmod",
        "rename",
        "unlink",
        "rmtree",
        "reconcile",
        "publish",
        "delete_artifact",
    }
    methods = {name for name in dir(ArtifactReader) if not name.startswith("__")}
    assert banned.isdisjoint(methods)


def _fd_count() -> int:
    """Best-effort open FD count for leak detection (Darwin/Linux)."""
    try:
        return len(os.listdir("/dev/fd"))
    except OSError:
        return len(os.listdir(f"/proc/{os.getpid()}/fd"))


def test_ancestor_symlink_at_construction_rejected(tmp_path: Path) -> None:
    """Symlink ancestor present before ArtifactReader construction."""
    real_parent = tmp_path / "real_parent"
    real_parent.mkdir()
    os.chmod(real_parent, 0o700)
    decoy = tmp_path / "decoy_parent"
    decoy.mkdir()
    os.chmod(decoy, 0o700)
    root = decoy / "root"
    root.mkdir()
    os.chmod(root, 0o700)
    # Configure path goes through a symlink component.
    link_parent = tmp_path / "link_parent"
    link_parent.symlink_to(decoy)
    with pytest.raises(DownloadError) as exc:
        ArtifactReader(str(link_parent / "root"))
    assert exc.value.code == DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE
    del real_parent


def test_ancestor_swap_after_construction_rejected(tmp_path: Path) -> None:
    """Replace an ancestor with a symlink after construction, before open."""
    base = tmp_path / "base"
    mid = base / "mid"
    root = mid / "root"
    root.mkdir(parents=True)
    os.chmod(base, 0o700)
    os.chmod(mid, 0o700)
    os.chmod(root, 0o700)
    _publish(root)
    reader = ArtifactReader(str(root))

    external = tmp_path / "external_root"
    external.mkdir()
    os.chmod(external, 0o700)
    secret = external / "secret.bin"
    secret.write_bytes(b"do-not-read")
    os.chmod(secret, 0o600)
    before = secret.read_bytes()

    # Swap mid for a symlink pointing at external (TOCTOU after construction).
    import shutil

    shutil.rmtree(mid)
    mid.symlink_to(external)

    job_id = uuid.uuid4()
    with pytest.raises(DownloadError) as exc:
        reader.open_published(
            artifact_id=uuid.uuid4(),
            download_job_id=job_id,
            format_option_id="fmt_deliv",
            expected_bytes=12,
            expected_container="mp4",
            expected_content_type="video/mp4",
            expected_expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
            expected_fence=1,
        )
    assert exc.value.code == DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE
    assert secret.read_bytes() == before
    assert secret.stat().st_mode & 0o777 == 0o600


def test_component_swap_during_walk_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a component becomes a symlink mid-walk, O_NOFOLLOW must fail closed."""
    base = tmp_path / "base"
    mid = base / "mid"
    root = mid / "root"
    root.mkdir(parents=True)
    for path in (base, mid, root):
        os.chmod(path, 0o700)

    external = tmp_path / "external"
    external.mkdir()
    os.chmod(external, 0o700)
    target = external / "payload"
    target.write_bytes(b"external-target-bytes")
    os.chmod(target, 0o600)
    before = target.read_bytes()
    before_mode = target.stat().st_mode

    real_open = os.open

    def race_open(path, flags, *args, dir_fd=None, **kwargs):  # type: ignore[no-untyped-def]
        name = path if isinstance(path, str) else ""
        if name == "mid" and mid.exists() and not mid.is_symlink():
            import shutil

            shutil.rmtree(mid)
            mid.symlink_to(external)
        return real_open(path, flags, *args, dir_fd=dir_fd, **kwargs)

    monkeypatch.setattr(os, "open", race_open)

    with pytest.raises(DownloadError) as exc:
        ArtifactReader.open_root_fd(str(root))
    assert exc.value.code == DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE
    assert target.read_bytes() == before
    assert target.stat().st_mode == before_mode

def test_descriptor_walk_fd_stable_across_loops(tmp_path: Path) -> None:
    root = _prepare_root(tmp_path / "root")
    job_id, artifact_id, expires, fence, content_type = _publish(root)
    reader = ArtifactReader(str(root))
    baseline = _fd_count()
    for _ in range(8):
        with reader.open_published(
            artifact_id=artifact_id,
            download_job_id=job_id,
            format_option_id="fmt_deliv",
            expected_bytes=len(b"delivery-bytes-payload"),
            expected_container="mp4",
            expected_content_type=content_type,
            expected_expires_at=expires,
            expected_fence=fence,
        ) as handle:
            assert handle.fd >= 0
        with pytest.raises(DownloadError):
            reader.open_published(
                artifact_id=uuid.uuid4(),
                download_job_id=job_id,
                format_option_id="fmt_deliv",
                expected_bytes=12,
                expected_container="mp4",
                expected_content_type="video/mp4",
                expected_expires_at=expires,
                expected_fence=fence,
            )
    # Allow small OS bookkeeping noise but not a monotonic leak of 8+ FDs.
    assert _fd_count() <= baseline + 2


def test_sha256_grammar_enforced(tmp_path: Path) -> None:
    root = _prepare_root(tmp_path / "root")
    job_id, artifact_id, expires, fence, content_type = _publish(root)
    manifest_path = root / "published" / str(artifact_id) / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["sha256"] = "zz" * 32
    fd = os.open(manifest_path, os.O_WRONLY | os.O_TRUNC)
    try:
        os.write(fd, json.dumps(payload).encode())
    finally:
        os.close(fd)
    os.chmod(manifest_path, 0o600)
    reader = ArtifactReader(str(root))
    with pytest.raises(DownloadError):
        reader.open_published(
            artifact_id=artifact_id,
            download_job_id=job_id,
            format_option_id="fmt_deliv",
            expected_bytes=len(b"delivery-bytes-payload"),
            expected_container="mp4",
            expected_content_type=content_type,
            expected_expires_at=expires,
            expected_fence=fence,
        )


def test_unlink_after_open_still_streams(tmp_path: Path) -> None:
    root = _prepare_root(tmp_path / "root")
    payload = b"x" * 2048
    job_id, artifact_id, expires, fence, content_type = _publish(
        root, payload=payload
    )
    reader = ArtifactReader(str(root))
    handle = reader.open_published(
        artifact_id=artifact_id,
        download_job_id=job_id,
        format_option_id="fmt_deliv",
        expected_bytes=len(payload),
        expected_container="mp4",
        expected_content_type=content_type,
        expected_expires_at=expires,
        expected_fence=fence,
    )
    # Worker TTL cleanup unlinks the published directory after open.
    import shutil

    shutil.rmtree(root / "published" / str(artifact_id))
    data = os.pread(handle.fd, len(payload), 0)
    handle.close()
    assert data == payload
    assert hashlib.sha256(data).hexdigest()
