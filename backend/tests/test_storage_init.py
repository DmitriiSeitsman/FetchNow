"""First-start empty-volume race: delivery cannot mkdir; storage-init can."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fetchnow.delivery.reader import ArtifactReader
from fetchnow.downloads.artifacts import ArtifactStore
from fetchnow.downloads.errors import DownloadError, DownloadErrorCode
from fetchnow.downloads.storage_init import ensure_download_root


def test_empty_volume_delivery_fails_until_storage_init(tmp_path: Path) -> None:
    volume = tmp_path / "tmp"
    volume.mkdir(mode=0o700)
    os.chmod(volume, 0o700)
    root = volume / "downloads"
    assert not root.exists()

    with pytest.raises(DownloadError) as missing:
        ArtifactReader.validate_root(str(root))
    assert missing.value.code == DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE

    created = Path(ensure_download_root(str(root)))
    assert created == root
    st = created.stat()
    assert oct(st.st_mode & 0o777) == "0o700"
    assert st.st_uid == os.getuid()

    validated = ArtifactReader.validate_root(str(root))
    assert validated == root
    worker = ArtifactStore.validate_root(str(root))
    assert worker == root


def test_storage_init_does_not_chmod_existing_operator_root(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    marker = root / "keep"
    marker.write_bytes(b"x")
    os.chmod(marker, 0o600)
    ensure_download_root(str(root))
    st = root.stat()
    assert oct(st.st_mode & 0o777) == "0o700"
    assert marker.read_bytes() == b"x"


def test_storage_init_rejects_symlink_root(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "downloads"
    link.symlink_to(real)
    with pytest.raises(DownloadError) as exc:
        ensure_download_root(str(link))
    assert exc.value.code == DownloadErrorCode.DOWNLOAD_STORAGE_UNAVAILABLE


def test_startup_order_function_contains_no_sleep() -> None:
    import inspect

    from fetchnow.downloads import storage_init

    source = inspect.getsource(storage_init)
    assert "sleep" not in source


def test_worker_or_delivery_either_order_after_init(tmp_path: Path) -> None:
    volume = tmp_path / "tmp"
    volume.mkdir(mode=0o700)
    os.chmod(volume, 0o700)
    root = volume / "downloads"
    ensure_download_root(str(root))
    ArtifactReader.validate_root(str(root))
    ArtifactStore.validate_root(str(root))

    root_b = tmp_path / "tmp-b" / "downloads"
    root_b.parent.mkdir(mode=0o700)
    os.chmod(root_b.parent, 0o700)
    ArtifactStore.validate_root(str(root_b))
    ArtifactReader.validate_root(str(root_b))


def test_storage_init_does_not_load_application_settings() -> None:
    import inspect

    from fetchnow.downloads import storage_init

    source = inspect.getsource(storage_init)
    assert "get_settings" not in source
    assert "fetchnow.core.config" not in source


def test_managed_download_root_from_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from fetchnow.downloads.storage_init import managed_download_root, run

    root = tmp_path / "downloads"
    monkeypatch.setenv("MEDIA_DOWNLOAD_TEMP_ROOT", str(root))
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TEMP_STORAGE_PATH", raising=False)
    assert managed_download_root() == str(root)
    volume = tmp_path / "tmp"
    volume.mkdir(mode=0o700)
    os.chmod(volume, 0o700)
    created = volume / "downloads"
    monkeypatch.setenv("MEDIA_DOWNLOAD_TEMP_ROOT", str(created))
    run()
    assert created.is_dir()
    assert oct(created.stat().st_mode & 0o777) == "0o700"
