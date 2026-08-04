"""Unit tests for PRD1C2 archive safety, manifest, locking, and deploy-root policy."""

from __future__ import annotations

import io
import os
import stat
import subprocess
import sys
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fetchnow_release.archive import (  # noqa: E402
    ArchiveError,
    _safe_extract_filter,
    detect_gitmodules,
    detect_lfs_pointers,
    extract_archive,
    stream_git_archive,
    verify_archive_commit,
    verify_required_files,
)
from fetchnow_release.c2_constants import LFS_POINTER_PREFIX  # noqa: E402
from fetchnow_release.cli import build_parser  # noqa: E402
from fetchnow_release.deploy_root import DeployRootError, validate_deploy_root  # noqa: E402
from fetchnow_release.git_checks import (  # noqa: E402
    GitCheckError,
    _run_git,
    assert_clean_worktree,
)
from fetchnow_release.image_build import (  # noqa: E402
    ImageBuildError,
    assert_tag_collision_safe,
    _compose_build_env,
)
from fetchnow_release.locking import ReleaseLockError, ReleaseRootLock  # noqa: E402
from fetchnow_release.manifest import (  # noqa: E402
    ImageRecord,
    ManifestError,
    ReleaseManifest,
    parse_manifest,
    validate_manifest,
)
from fetchnow_release.prepare import _remove_path  # noqa: E402
from fetchnow_release.redact import redact  # noqa: E402
from fetchnow_release.revision import RevisionError, validate_full_sha  # noqa: E402


def _make_tar(
    members: list[tuple[str, bytes | None, int]],
    path: Path,
    *,
    symlink: tuple[str, str] | None = None,
    hardlink: tuple[str, str] | None = None,
    fifo: str | None = None,
    sock: str | None = None,
    gitlink: str | None = None,
    pax_comment: str | None = None,
) -> None:
    with tarfile.open(path, mode="w") as tf:
        for name, data, mode in members:
            info = tarfile.TarInfo(name=name)
            info.mode = mode
            if pax_comment:
                info.pax_headers["comment"] = pax_comment
            if data is None:
                info.type = tarfile.DIRTYPE
                tf.addfile(info)
            else:
                info.size = len(data)
                info.type = tarfile.REGTYPE
                tf.addfile(info, io.BytesIO(data))
        if symlink is not None:
            info = tarfile.TarInfo(name=symlink[0])
            info.type = tarfile.SYMTYPE
            info.linkname = symlink[1]
            info.mode = 0o777
            tf.addfile(info)
        if hardlink is not None:
            info = tarfile.TarInfo(name=hardlink[0])
            info.type = tarfile.LNKTYPE
            info.linkname = hardlink[1]
            info.mode = 0o644
            tf.addfile(info)
        if fifo is not None:
            info = tarfile.TarInfo(name=fifo)
            info.type = tarfile.FIFOTYPE
            info.mode = 0o644
            tf.addfile(info)
        if sock is not None:
            info = tarfile.TarInfo(name=sock)
            info.type = getattr(tarfile, "SOCKTYPE", b"S")
            info.mode = 0o644
            tf.addfile(info)
        if gitlink is not None:
            info = tarfile.TarInfo(name=gitlink)
            info.type = tarfile.REGTYPE
            info.mode = 0o160000
            info.size = 20
            tf.addfile(info, io.BytesIO(b"0" * 20))


def test_unsafe_deploy_roots(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(DeployRootError):
        validate_deploy_root(Path("/"), repo_root=repo)
    with pytest.raises(DeployRootError):
        validate_deploy_root(Path.home(), repo_root=repo)
    with pytest.raises((DeployRootError, Exception)):
        validate_deploy_root(repo, repo_root=repo)
    good = tmp_path / "deploy"
    good.mkdir()
    assert validate_deploy_root(good, repo_root=repo) == good.resolve()


def test_symlink_escape_deploy_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "deploy-link"
    link.symlink_to(outside)
    with pytest.raises(DeployRootError):
        validate_deploy_root(link, repo_root=repo)


def test_extract_rejects_absolute_and_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "bad.tar"
    dest = tmp_path / "out"
    _make_tar([("../escape.txt", b"x", 0o644)], archive)
    with pytest.raises(ArchiveError, match="traversal|escape"):
        extract_archive(archive, dest)

    archive2 = tmp_path / "abs.tar"
    with tarfile.open(archive2, mode="w") as tf:
        info = tarfile.TarInfo(name="/etc/passwd")
        data = b"x"
        info.size = len(data)
        info.type = tarfile.REGTYPE
        info.mode = 0o644
        tf.addfile(info, io.BytesIO(data))
    with pytest.raises(ArchiveError, match="absolute|escape|traversal"):
        extract_archive(archive2, tmp_path / "out2")


def test_extract_rejects_control_chars(tmp_path: Path) -> None:
    archive = tmp_path / "ctrl.tar"
    # Tar truncates at embedded NUL; use a non-NUL control character instead.
    with tarfile.open(archive, mode="w") as tf:
        info = tarfile.TarInfo(name="bad\x01name.txt")
        data = b"x"
        info.size = len(data)
        info.type = tarfile.REGTYPE
        info.mode = 0o644
        tf.addfile(info, io.BytesIO(data))
    with pytest.raises(ArchiveError, match="control"):
        extract_archive(archive, tmp_path / "out")


def test_extract_rejects_unsafe_symlink_and_hardlink(tmp_path: Path) -> None:
    archive = tmp_path / "sym.tar"
    dest = tmp_path / "out-sym"
    _make_tar([("ok.txt", b"x", 0o644)], archive, symlink=("bad", "/etc/passwd"))
    with pytest.raises(ArchiveError, match="absolute|unsafe link|link escape"):
        extract_archive(archive, dest)

    archive2 = tmp_path / "lnk.tar"
    dest2 = tmp_path / "out-lnk"
    _make_tar(
        [("ok.txt", b"x", 0o644)],
        archive2,
        hardlink=("alias", "../outside"),
    )
    with pytest.raises(ArchiveError, match="traversal|unsafe link|link escape"):
        extract_archive(archive2, dest2)


def test_extract_rejects_fifo_and_socket(tmp_path: Path) -> None:
    archive = tmp_path / "fifo.tar"
    _make_tar([("ok.txt", b"x", 0o644)], archive, fifo="pipe")
    with pytest.raises(ArchiveError, match="device/FIFO"):
        extract_archive(archive, tmp_path / "out")

    archive2 = tmp_path / "sock.tar"
    _make_tar([("ok.txt", b"x", 0o644)], archive2, sock="sock")
    with pytest.raises(ArchiveError, match="socket|device/FIFO"):
        extract_archive(archive2, tmp_path / "out2")


def test_extract_rejects_gitlink() -> None:
    # ustar round-trip drops high mode bits; exercise the filter directly.
    info = tarfile.TarInfo(name="vendor/lib")
    info.type = tarfile.REGTYPE
    info.mode = 0o160000
    info.size = 20
    with pytest.raises(ArchiveError, match="gitlink"):
        _safe_extract_filter(info, "/tmp/out")


def test_extract_rejects_parent_type_collision(tmp_path: Path) -> None:
    archive = tmp_path / "collide.tar"
    _make_tar(
        [("foo", b"file", 0o644), ("foo/bar", b"nested", 0o644)],
        archive,
    )
    with pytest.raises(ArchiveError, match="type collision|duplicate"):
        extract_archive(archive, tmp_path / "out")


def test_extract_rejects_duplicate_paths(tmp_path: Path) -> None:
    archive = tmp_path / "dup.tar"
    dest = tmp_path / "out"
    _make_tar(
        [("a.txt", b"1", 0o644), ("a.txt", b"2", 0o644)],
        archive,
    )
    with pytest.raises(ArchiveError, match="duplicate"):
        extract_archive(archive, dest)


def test_extract_strips_setuid_preserves_exec(tmp_path: Path) -> None:
    archive = tmp_path / "bits.tar"
    dest = tmp_path / "out"
    _make_tar(
        [
            ("bin/tool", b"#!/bin/sh\n", 0o4755),
            ("readme.txt", b"hi", 0o644),
        ],
        archive,
    )
    extract_archive(archive, dest)
    tool = dest / "bin" / "tool"
    mode = stat.S_IMODE(tool.stat().st_mode)
    assert mode & stat.S_ISUID == 0
    assert mode & 0o111


def test_archive_commit_mismatch(tmp_path: Path) -> None:
    expected = "a" * 40
    other = "b" * 40
    archive = tmp_path / "mis.tar"
    _make_tar([("f.txt", b"x", 0o644)], archive, pax_comment=other)
    with pytest.raises(ArchiveError, match="does not match"):
        verify_archive_commit(archive, expected)


def test_empty_archive_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    out = tmp_path / "empty.tar"

    class FakeProc:
        returncode = 0

        def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    def fake_popen(*_a, **_k):  # noqa: ANN001
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    with pytest.raises(ArchiveError, match="empty"):
        stream_git_archive(repo, "a" * 40, out)


def test_lfs_pointer_detection(tmp_path: Path) -> None:
    src = tmp_path / "source"
    src.mkdir()
    (src / "compose.yaml").write_bytes(LFS_POINTER_PREFIX + b"\noid sha256:abc\n")
    with pytest.raises(ArchiveError, match="LFS"):
        detect_lfs_pointers(src)


def test_unsupported_submodule(tmp_path: Path) -> None:
    src = tmp_path / "source"
    src.mkdir()
    (src / ".gitmodules").write_text("[submodule \"x\"]\n", encoding="utf-8")
    with pytest.raises(ArchiveError, match="submodules"):
        detect_gitmodules(src)


def test_missing_required_files(tmp_path: Path) -> None:
    src = tmp_path / "source"
    src.mkdir()
    with pytest.raises(ArchiveError, match="required source path missing"):
        verify_required_files(src)


def test_dirty_worktree_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        assert args[:2] == ("status", "--porcelain")
        return subprocess.CompletedProcess(
            ["git", *args], 0, "?? untracked.txt\n", ""
        )

    monkeypatch.setattr("fetchnow_release.git_checks._run_git", fake_run)
    with pytest.raises(GitCheckError, match="dirty"):
        assert_clean_worktree(tmp_path)


def test_clean_worktree_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    monkeypatch.setattr("fetchnow_release.git_checks._run_git", fake_run)
    assert_clean_worktree(tmp_path)


def test_cli_prepare_has_no_dirty_bypass() -> None:
    help_text = build_parser().format_help()
    assert "allow_dirty" not in help_text
    assert "skip_preflight" not in help_text
    assert "test_mode" not in help_text
    assert "prepare" in help_text
    assert "verify-release" in help_text


def test_compose_build_env_excludes_secrets() -> None:
    with patch.dict(
        os.environ,
        {
            "PATH": "/bin",
            "POSTGRES_PASSWORD": "super-secret",
            "DATABASE_URL": "postgresql://u:p@h/db",
            "DOCKER_HOST": "unix:///var/run/docker.sock",
        },
        clear=False,
    ):
        env = _compose_build_env("a" * 40)
    assert "POSTGRES_PASSWORD" not in env
    assert "DATABASE_URL" not in env
    assert env["FETCHNOW_RELEASE_REVISION"] == "a" * 40
    assert env.get("DOCKER_HOST") == "unix:///var/run/docker.sock"


def test_manifest_rejects_bad_image_id() -> None:
    rev = "a" * 40
    raw = {
        "schema_version": 1,
        "revision": rev,
        "tree_oid": "b" * 40,
        "created_at_utc": "2026-01-01T00:00:00Z",
        "archive_commit_verified": True,
        "source_repository": "FetchNow",
        "compose_version": "2",
        "docker_version": "24",
        "build_platform": "linux/amd64",
        "contract_hashes": {"compose.yaml": "c" * 64},
        "tool_version": "1.1.0",
        "status": "prepared",
        "images": [
            {
                "service": "api",
                "reference": f"fetchnow-api:{rev}",
                "image_id": "not-an-id",
                "oci_revision": rev,
                "platform": "linux/amd64",
            },
            {
                "service": "web",
                "reference": f"fetchnow-web:{rev}",
                "image_id": "sha256:" + ("d" * 64),
                "oci_revision": rev,
                "platform": "linux/amd64",
            },
            {
                "service": "gateway",
                "reference": f"fetchnow-gateway:{rev}",
                "image_id": "sha256:" + ("e" * 64),
                "oci_revision": rev,
                "platform": "linux/amd64",
            },
        ],
    }
    with pytest.raises(ManifestError, match="malformed"):
        parse_manifest(raw)


def test_manifest_tag_label_mismatch() -> None:
    rev = "a" * 40
    images = (
        ImageRecord(
            "api",
            f"fetchnow-api:{rev}",
            "sha256:" + ("1" * 64),
            "b" * 40,
            "linux/amd64",
        ),
        ImageRecord(
            "web", f"fetchnow-web:{rev}", "sha256:" + ("2" * 64), rev, "linux/amd64"
        ),
        ImageRecord(
            "gateway",
            f"fetchnow-gateway:{rev}",
            "sha256:" + ("3" * 64),
            rev,
            "linux/amd64",
        ),
    )
    man = ReleaseManifest(
        schema_version=1,
        revision=rev,
        tree_oid="c" * 40,
        created_at_utc="2026-01-01T00:00:00Z",
        archive_commit_verified=True,
        source_repository="FetchNow",
        compose_version="2",
        docker_version="24",
        build_platform="linux/amd64",
        images=images,
        contract_hashes={"compose.yaml": "d" * 64},
        tool_version="1.1.0",
        status="prepared",
    )
    with pytest.raises(ManifestError, match="OCI revision"):
        validate_manifest(man)


def test_manifest_unsupported_schema() -> None:
    rev = "a" * 40
    raw = {
        "schema_version": 99,
        "revision": rev,
        "tree_oid": "b" * 40,
        "created_at_utc": "2026-01-01T00:00:00Z",
        "archive_commit_verified": True,
        "source_repository": "FetchNow",
        "compose_version": "2",
        "docker_version": "24",
        "build_platform": "linux/amd64",
        "contract_hashes": {"compose.yaml": "c" * 64},
        "tool_version": "1.1.0",
        "status": "prepared",
        "images": [],
    }
    with pytest.raises(ManifestError, match="schema"):
        parse_manifest(raw)


def test_running_container_tag_collision() -> None:
    rev = "a" * 40
    with (
        patch("fetchnow_release.image_build.image_exists", return_value=True),
        patch(
            "fetchnow_release.image_build.containers_using_image",
            return_value=["fetchnow-staging-api-1"],
        ),
    ):
        with pytest.raises(ImageBuildError, match="refusing to overwrite"):
            assert_tag_collision_safe(rev)


def test_unused_unmanaged_tag_warning() -> None:
    rev = "a" * 40
    with (
        patch("fetchnow_release.image_build.image_exists", return_value=True),
        patch("fetchnow_release.image_build.containers_using_image", return_value=[]),
    ):
        warnings = assert_tag_collision_safe(rev, allow_unused_retag=True)
        assert any("unused unmanaged tag" in w for w in warnings)


def test_incomplete_cleanup(tmp_path: Path) -> None:
    incomplete = tmp_path / ".incomplete-deadbeef"
    incomplete.mkdir()
    (incomplete / "source").mkdir()
    (incomplete / "source" / "f.txt").write_text("x", encoding="utf-8")
    _remove_path(incomplete)
    assert not incomplete.exists()


def test_atomic_rename_same_filesystem(tmp_path: Path) -> None:
    releases = tmp_path / "releases"
    releases.mkdir()
    incomplete = tmp_path / ".incomplete-a"
    incomplete.mkdir()
    (incomplete / "release.json").write_text("{}", encoding="utf-8")
    final = releases / ("a" * 40)
    os.rename(incomplete, final)
    assert final.is_dir()
    assert not incomplete.exists()
    assert (final / "release.json").is_file()


def test_lock_contention(tmp_path: Path) -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    with ReleaseRootLock(deploy, wait=False):
        with pytest.raises(ReleaseLockError, match="locked"):
            with ReleaseRootLock(deploy, wait=False):
                pass


def test_secret_redaction() -> None:
    text = "DATABASE_URL=postgresql://u:secret@host/db POSTGRES_PASSWORD=hunter2"
    out = redact(text)
    assert "hunter2" not in out
    assert "secret@" not in out
    assert "<redacted>" in out


def test_git_disallows_fetch(tmp_path: Path) -> None:
    with pytest.raises(GitCheckError, match="non-read-only"):
        _run_git(tmp_path, "fetch", "origin")


def test_abbreviated_sha_rejected() -> None:
    with pytest.raises(RevisionError):
        validate_full_sha("bfa009d")


def test_full_sha_accepted() -> None:
    sha = "bfa009db0e90dd897e4db350bd9497858573ffae"
    assert validate_full_sha(sha) == sha


def test_subprocess_list_form_no_shell() -> None:
    """Git helpers must invoke argv lists (no shell=True injection surface)."""
    import inspect

    from fetchnow_release import archive, git_checks

    assert "shell=True" not in inspect.getsource(archive._run_git)
    assert "shell=True" not in inspect.getsource(git_checks._run_git)
    assert "shell=True" not in inspect.getsource(archive.stream_git_archive)
